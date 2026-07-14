import { type AppSettings, resolveTemperature } from '../../../shared/settings'
import { authHeaders } from './backend'

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
  const res = await fetch(`http://127.0.0.1:${port}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      messages,
      model: settings.model,
      reasoning_effort: settings.reasoningEffort,
      temperature: resolveTemperature(settings, lastUserText),
      context_length: settings.contextLength,
      ollama_host: settings.ollamaHost,
      keep_alive: settings.keepAlive,
      research: settings.chatWebSearch
    }),
    signal
  })
  if (!res.ok || !res.body) {
    throw new Error(`백엔드 오류 (HTTP ${res.status})`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx).trim()
      buf = buf.slice(idx + 1)
      if (!line) continue
      try {
        onChunk(JSON.parse(line) as ChatChunk)
      } catch {
        /* 불완전한 JSON 라인 무시 */
      }
    }
  }
}
