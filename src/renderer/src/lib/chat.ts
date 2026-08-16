import {
  type AppSettings,
  resolveTemperature,
  snapshotLlmSettings
} from '../../../shared/settings'
import { authHeaders } from './backend'
import type { AttachmentRef } from '../../../shared/attachments'

export interface ChatChunk {
  type:
    | 'thinking'
    | 'content'
    | 'done'
    | 'error'
    | 'notice'
    | 'tool_call'
    | 'tool_result'
    | 'usage'
    | 'reset_content'
    | 'incomplete'
    | 'cancelled'
    | 'tool_calls'
  text?: string
  error?: string
  eval_count?: number
  total_duration?: number
  // 웹 검색(리서치) 모드 전용 이벤트 필드
  id?: string
  name?: string
  args?: Record<string, unknown>
  ok?: boolean
  output?: string
  total?: number // 누적 생성 토큰(usage)
}

export interface ChatPayloadMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  attachments?: AttachmentRef[]
}

/** FastAPI 사이드카의 NDJSON 스트림을 읽어 청크 단위로 콜백한다. */
export async function streamChat(
  port: number,
  settings: AppSettings,
  messages: ChatPayloadMessage[],
  onChunk: (c: ChatChunk) => void,
  signal?: AbortSignal
): Promise<void> {
  const lastUserText = [...messages].reverse().find((m) => m.role === 'user')?.content ?? ''
  const snapshot = snapshotLlmSettings(settings)
  let nvidiaResearchGrant = ''
  if (snapshot.provider === 'nvidia') {
    if (!snapshot.model.trim()) throw new Error('설정에서 NVIDIA 모델명을 입력해 주세요.')
    if (settings.chatWebSearch) {
      nvidiaResearchGrant = (await window.api.nvidia.research.prepare({
        deploymentMode: snapshot.deploymentMode!,
        endpoint: snapshot.endpoint,
        model: snapshot.model
      })).grantId
    } else {
      await window.api.nvidia.execution.prepare({
        deploymentMode: snapshot.deploymentMode!,
        endpoint: snapshot.endpoint
      })
    }
  }
  const res = await fetch(`http://127.0.0.1:${port}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      messages: messages.map((message) => ({
        role: message.role,
        content: message.content,
        attachments: message.attachments?.map((attachment) => attachment.id) ?? []
      })),
      provider: snapshot.provider,
      deployment_mode: snapshot.deploymentMode,
      endpoint: snapshot.endpoint,
      model: snapshot.model,
      reasoning_effort: settings.reasoningEffort,
      temperature: resolveTemperature(settings, lastUserText),
      context_length: settings.contextLength,
      ollama_host: snapshot.provider === 'ollama' ? snapshot.endpoint : undefined,
      keep_alive: settings.keepAlive,
      research: settings.chatWebSearch,
      nvidia_research_grant: nvidiaResearchGrant
    }),
    signal
  })
  if (!res.ok || !res.body) {
    throw new Error(`백엔드 오류 (HTTP ${res.status})`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let terminal = false
  const processLine = (lineValue: string): void => {
    const line = lineValue.trim()
    if (!line) return
    let chunk: ChatChunk
    try {
      chunk = JSON.parse(line) as ChatChunk
    } catch {
      throw new Error('백엔드 응답 스트림 형식이 올바르지 않습니다.')
    }
    onChunk(chunk)
    if (['done', 'error', 'incomplete', 'cancelled'].includes(chunk.type)) terminal = true
  }
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx)
      buf = buf.slice(idx + 1)
      processLine(line)
    }
  }
  buf += decoder.decode()
  if (buf.trim()) processLine(buf)
  if (!terminal) throw new Error('백엔드 응답이 완료 표식 없이 종료되었습니다.')
}
