import {
  type AppSettings,
  snapshotLlmSettings,
  resolveTemperature
} from '../../../shared/settings'
import type { AgentEvent, AgentToolCatalog, ApprovalMode } from '../../../shared/agent'
import type { ComfyModelProfile } from '../../../shared/comfy-model'
import { authHeaders } from './backend'

export interface AgentMessage {
  role: 'user' | 'assistant'
  content: string
}

/**
 * Manual ComfyUI selection is intentionally a per-generation choice rather
 * than a model-name hint embedded in the prompt.  The sidecar validates this
 * ID against the registered profile list before it can execute a workflow.
 */
export interface ComfySelectionRequest {
  selectedComfyModelId?: string
}

/** 실제 Python 레지스트리에서 계산한 내장 Agent 도구 목록을 읽는다. */
export async function fetchAgentToolCatalog(port: number): Promise<AgentToolCatalog> {
  const res = await fetch(`http://127.0.0.1:${port}/agent/tools`, {
    headers: authHeaders()
  })
  if (!res.ok) throw new Error(`도구 목록을 불러오지 못했습니다 (HTTP ${res.status})`)
  const catalog = await res.json() as AgentToolCatalog
  if (!Array.isArray(catalog.tools)) throw new Error('도구 목록 응답 형식이 올바르지 않습니다.')
  return catalog
}

/** 에이전트 하네스 스트림(NDJSON) 을 읽어 이벤트 단위로 콜백한다. */
export async function streamAgent(
  port: number,
  settings: AppSettings,
  workspace: string,
  messages: AgentMessage[],
  sessionId: string,
  assistantTurnId: string,
  approvalMode: ApprovalMode,
  comfyProfiles: ComfyModelProfile[],
  onEvent: (e: AgentEvent) => void,
  signal?: AbortSignal,
  comfySelection?: ComfySelectionRequest
): Promise<void> {
  const target = snapshotLlmSettings(settings)
  const nvidiaGrant = target.provider === 'nvidia'
    ? await window.api.nvidia.agent.prepare({ sessionId, assistantTurnId })
    : null
  const lastUserText = [...messages].reverse().find((m) => m.role === 'user')?.content ?? ''
  const comfySelectionMode = settings.comfyModelSelectionMode === 'manual' ? 'manual' : 'auto'
  const selectedComfyModelId =
    comfySelectionMode === 'manual' && typeof comfySelection?.selectedComfyModelId === 'string'
      ? comfySelection.selectedComfyModelId.trim()
      : ''
  const res = await fetch(`http://127.0.0.1:${port}/agent`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      messages,
      provider: target.provider,
      workspace: target.provider === 'nvidia' ? '' : workspace,
      model: target.model,
      assistant_turn_id: assistantTurnId,
      nvidia_grant: nvidiaGrant?.grantId ?? '',
      deployment_mode: target.deploymentMode,
      endpoint: target.endpoint,
      reasoning_effort: settings.reasoningEffort,
      temperature: resolveTemperature(settings, lastUserText),
      context_length: settings.contextLength,
      approval_mode: approvalMode,
      session_id: sessionId,
      ollama_host: settings.ollamaHost,
      rag_enabled: target.provider === 'nvidia' ? false : settings.ragEnabled,
      rag_top_k: settings.ragTopK,
      keep_alive: settings.keepAlive,
      comfy_base_url: target.provider === 'nvidia' ? null : settings.comfyBaseUrl,
      comfy_profiles: target.provider === 'nvidia' ? [] : comfyProfiles,
      comfy_selection_mode: target.provider === 'nvidia' ? 'auto' : comfySelectionMode,
      ...(target.provider !== 'nvidia' && selectedComfyModelId
        ? { selected_comfy_model_id: selectedComfyModelId }
        : {})
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
