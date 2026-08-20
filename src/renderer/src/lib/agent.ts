import {
  type AppSettings,
  snapshotLlmSettings,
  resolveTemperature
} from '../../../shared/settings'
import type { AgentEvent, AgentToolCatalog, ApprovalMode } from '../../../shared/agent'
import type { ComfyModelProfile } from '../../../shared/comfy-model'
import { authHeaders } from './backend'
import type { AttachmentRef } from '../../../shared/attachments'

export interface AgentMessage {
  role: 'user' | 'assistant' | 'tool'
  content: string
  attachments?: AttachmentRef[]
  /**
   * 이 assistant 턴이 실제로 호출한 도구들. 각 항목의 id에 대응하는 `role: 'tool'`
   * 메시지가 바로 뒤에 와야 한다 — 짝이 깨지면 OpenAI 호환 공급자가 요청 전체를
   * 거부한다. 서버도 같은 불변식을 다시 강제하지만, 여기서부터 지켜서 보낸다.
   */
  toolCalls?: AgentToolCallRecord[]
  /** 이 결과가 어느 호출에 대한 것인지. `role: 'tool'`에만 쓴다. */
  toolCallId?: string
}

export interface AgentToolCallRecord {
  id: string
  type: 'function'
  function: { name: string; arguments: Record<string, unknown> }
}

/**
 * Manual ComfyUI selection is intentionally a per-generation choice rather
 * than a model-name hint embedded in the prompt.  The sidecar validates this
 * ID against the registered profile list before it can execute a workflow.
 */
export interface ComfySelectionRequest {
  selectedComfyModelId?: string
  /** Main-issued, one-use grant. Renderer can carry it but cannot mint it. */
  nvidiaGrantId?: string
}

const AGENT_APPROVAL_TIMEOUT_MS = 10_000

function isApprovalTimeout(error: unknown): boolean {
  return error instanceof Error && (error.name === 'TimeoutError' || error.name === 'AbortError')
}

function approvalTimeoutError(): Error {
  return new Error('승인 응답 시간이 초과되었습니다. 백엔드 상태를 확인한 뒤 다시 시도해 주세요.')
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
  comfySelection?: ComfySelectionRequest,
  /** Proven only by a persisted/rendered image result card, never by model prose. */
  imageContextVerified = false,
  /** Client-side conversation correlation only; never writes to My DB. */
  conversationId = ''
): Promise<void> {
  const target = snapshotLlmSettings(settings)
  const nvidiaGrantId = target.provider === 'nvidia'
    ? comfySelection?.nvidiaGrantId?.trim() ?? ''
    : ''
  if (target.provider === 'nvidia' && !nvidiaGrantId) {
    throw new Error('NVIDIA Agent 실행 승인이 준비되지 않았습니다.')
  }
  const lastUserText = [...messages].reverse().find((m) => m.role === 'user')?.content ?? ''
  const enabledTools = settings.agentToolPolicy[target.provider]
  const enabledToolSet = new Set(enabledTools)
  const comfySelectionMode = settings.comfyModelSelectionMode === 'manual' ? 'manual' : 'auto'
  const selectedComfyModelId =
    comfySelectionMode === 'manual' && typeof comfySelection?.selectedComfyModelId === 'string'
      ? comfySelection.selectedComfyModelId.trim()
      : ''
  const res = await fetch(`http://127.0.0.1:${port}/agent`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      messages: messages.map((message) => ({
        role: message.role,
        content: message.content,
        attachments: message.attachments?.map((attachment) => attachment.id) ?? [],
        ...(message.toolCalls ? { tool_calls: message.toolCalls } : {}),
        ...(message.toolCallId ? { tool_call_id: message.toolCallId } : {})
      })),
      provider: target.provider,
      workspace: target.provider === 'nvidia' ? '' : workspace,
      model: target.model,
      assistant_turn_id: assistantTurnId,
      image_context_verified: imageContextVerified,
      nvidia_grant: nvidiaGrantId,
      deployment_mode: target.deploymentMode,
      endpoint: target.endpoint,
      reasoning_effort: settings.reasoningEffort,
      temperature: resolveTemperature(settings, lastUserText),
      context_length: settings.contextLength,
      approval_mode: approvalMode,
      session_id: sessionId,
      conversation_id: conversationId,
      ollama_host: settings.ollamaHost,
      rag_enabled: target.provider === 'nvidia'
        ? false
        : settings.ragEnabled && enabledToolSet.has('search_docs'),
      rag_top_k: settings.ragTopK,
      keep_alive: settings.keepAlive,
      comfy_base_url: target.provider === 'nvidia' || !enabledToolSet.has('generate_image')
        ? null
        : settings.comfyBaseUrl,
      comfy_profiles: target.provider === 'nvidia' || !enabledToolSet.has('generate_image')
        ? []
        : comfyProfiles,
      comfy_selection_mode: target.provider === 'nvidia' ? 'auto' : comfySelectionMode,
      ...(target.provider !== 'nvidia' && selectedComfyModelId
        ? { selected_comfy_model_id: selectedComfyModelId }
        : {}),
      ...(target.provider === 'ollama' ? { enabled_tools: enabledTools } : {})
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
  let response: Response
  try {
    response = await fetch(`http://127.0.0.1:${port}/agent/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ session_id: sessionId, call_id: callId, approved }),
      signal: AbortSignal.timeout(AGENT_APPROVAL_TIMEOUT_MS)
    })
  } catch (error) {
    if (isApprovalTimeout(error)) throw approvalTimeoutError()
    throw error
  }
  let result: unknown
  try {
    result = await response.json()
  } catch (error) {
    if (isApprovalTimeout(error)) throw approvalTimeoutError()
    throw new Error(
      response.ok
        ? '승인 응답 형식이 올바르지 않습니다.'
        : `승인 요청을 처리하지 못했습니다 (HTTP ${response.status})`
    )
  }
  if (!response.ok) {
    throw new Error(`승인 요청을 처리하지 못했습니다 (HTTP ${response.status})`)
  }
  if (!result || typeof result !== 'object' || (result as Record<string, unknown>).ok !== true) {
    throw new Error('승인 요청이 만료되었거나 현재 작업과 일치하지 않습니다. 다시 시도해 주세요.')
  }
}
