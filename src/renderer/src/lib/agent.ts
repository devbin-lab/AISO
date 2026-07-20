import { type AppSettings, resolveTemperature } from '../../../shared/settings'
import type { AgentEvent, ApprovalMode } from '../../../shared/agent'
import type { ComfyModelProfile } from '../../../shared/comfy-model'
import { authHeaders } from './backend'

export interface AgentMessage {
  role: 'user' | 'assistant'
  content: string
}

/** 에이전트 하네스 스트림(NDJSON) 을 읽어 이벤트 단위로 콜백한다. */
export async function streamAgent(
  port: number,
  settings: AppSettings,
  workspace: string,
  messages: AgentMessage[],
  sessionId: string,
  approvalMode: ApprovalMode,
  comfyProfiles: ComfyModelProfile[],
  onEvent: (e: AgentEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const lastUserText = [...messages].reverse().find((m) => m.role === 'user')?.content ?? ''
  const res = await fetch(`http://127.0.0.1:${port}/agent`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      messages,
      workspace,
      model: settings.model,
      reasoning_effort: settings.reasoningEffort,
      temperature: resolveTemperature(settings, lastUserText),
      context_length: settings.contextLength,
      approval_mode: approvalMode,
      session_id: sessionId,
      ollama_host: settings.ollamaHost,
      rag_enabled: settings.ragEnabled,
      rag_top_k: settings.ragTopK,
      keep_alive: settings.keepAlive,
      comfy_base_url: settings.comfyBaseUrl,
      comfy_profiles: comfyProfiles
    }),
    signal
  })
  if (!res.ok || !res.body) throw new Error(`에이전트 백엔드 오류 (HTTP ${res.status})`)

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
        onEvent(JSON.parse(line) as AgentEvent)
      } catch {
        /* 불완전 라인 무시 */
      }
    }
  }
}

/** 파괴적 툴 승인/거부 신호를 백엔드로 보낸다. */
export async function approveAgent(
  port: number,
  sessionId: string,
  callId: string,
  approved: boolean
): Promise<void> {
  await fetch(`http://127.0.0.1:${port}/agent/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ session_id: sessionId, call_id: callId, approved })
  })
}
