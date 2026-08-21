import { useCallback, useEffect, useRef, useState } from 'react'
import {
  snapshotLlmSettings,
  type AppSettings,
  type ReasoningEffort
} from '../../../shared/settings'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import type {
  AgentEvent,
  ApprovalMode,
  ComfyGeneratedImage,
  PlanStep
} from '../../../shared/agent'
import { TOOL_LABEL, APPROVAL_MODES } from '../../../shared/agent'
import {
  streamAgent,
  approveAgent,
  AUTO_CONTINUE_PROMPT,
  MAX_AUTO_CONTINUES,
  type AgentMessage
} from '../lib/agent'
import { newConversationId, titleFromText } from '../lib/conversations'
import { modelInstalled } from '../lib/ollama'
import type { ConversationRequest } from '../components/Sidebar'
import Markdown from '../components/Markdown'
import { ragStatus, ragIndex, type RagStatus } from '../lib/rag'
import { authHeaders } from '../lib/backend'
import {
  AgentIcon,
  FolderIcon,
  FileIcon,
  RefreshIcon,
  GlobeIcon,
  CodeIcon,
  PanelRightIcon,
  TrashIcon,
  CloseIcon,
  DatabaseIcon,
  SearchIcon,
  TerminalIcon
} from '../components/icons'
import Dropdown, { type DropdownOption } from '../components/Dropdown'
import GeneratedImage from '../components/GeneratedImage'
import { ensureComfyReadyForAgent, looksLikeImageGenerationRequest } from '../lib/comfy'
import { getComfyAgentReadiness, type ComfyModelProfile } from '../../../shared/comfy-model'
import AttachmentPicker from '../components/AttachmentPicker'
import type { AttachmentRef } from '../../../shared/attachments'

const EFFORT_OPTIONS: DropdownOption[] = [
  { value: 'low', label: '낮음', hint: '빠름' },
  { value: 'medium', label: '중간', hint: '균형' },
  { value: 'high', label: '높음', hint: '정확' }
]
const APPROVAL_OPTIONS: DropdownOption[] = APPROVAL_MODES.map((m) => ({
  value: m.v,
  label: m.label,
  hint: m.hint
}))

/**
 * 자동 모드에서는 Agent 자동 선택 허용 모델만, 수동 모드에서는 등록·검증이 완료된
 * 모든 모델을 후보로 노출한다. `agentEnabled`는 자동 선택 허용 플래그일 뿐 모델 자체의
 * 수동 실행 가능 여부를 뜻하지 않는다.
 */
async function listComfyProfilesForAgent(manualSelection: boolean): Promise<ComfyModelProfile[]> {
  try {
    const registry = await window.api.comfy.models.list()
    return registry.profiles.filter(
      (profile) => getComfyAgentReadiness(profile).ready && (manualSelection || profile.agentEnabled)
    )
  } catch {
    return []
  }
}

type ToolStatus = 'running' | 'awaiting' | 'done' | 'error' | 'rejected' | 'expired'

type Item =
  | { kind: 'user'; text: string; attachments?: AttachmentRef[] }
  | { kind: 'assistant'; text: string; thinking: string; streaming: boolean }
  | {
      kind: 'tool'
      callId: string
      approvalId: string
      providerToolCallId: string
      assistantTurnId: string
      name: string
      args: Record<string, unknown>
      status: ToolStatus
      output?: string
      screenshot?: string
    }
  | { kind: 'image'; image: ComfyGeneratedImage }
  | { kind: 'meta'; tokens: number; seconds: number } // 실행 완료 후 토큰·소요시간 요약줄

interface Props {
  active: boolean
  settings: AppSettings
  backend: BackendInfo
  health: HealthInfo | null
  onPickWorkspace: () => Promise<void>
  onSaveSettings: (patch: Partial<AppSettings>) => Promise<boolean>
  conversationRequest?: ConversationRequest | null
  onConversationActive?: (id: string | null) => void
  /** App-level Agent mode only: conversations must begin from a registered project. */
  requireProjectStart?: boolean
  convCollapsed?: boolean
}

/** 런 경계를 넘겨 이어갈 도구 실행 기록 수. 넘길수록 근거는 늘지만 요청이 커진다. */
const CARRIED_TOOL_RESULTS = 12

/**
 * 타임라인에서 최근 도구 실행을 뽑아 모델이 읽을 수 있는 형태로 조립한다.
 *
 * 예전에는 실행이 끝나면 마지막 assistant 문장 하나만 다음 실행으로 넘어갔다.
 * "여기까지 한 내용은 유지됩니다"라고 안내하면서 정작 도구 결과는 전부 사라져,
 * '계속해줘'가 백지에서 다시 시작했다. 결과 본문은 이미 타임라인에 있으므로
 * 새로 저장할 것은 없고, 모델에게 보내는 형태로 바꿔주기만 하면 된다.
 *
 * 짝을 반드시 맞춘다 — 결과가 없는 호출(승인 대기·실행 중·중단)은 제외한다.
 * 호출만 있고 결과가 없으면 OpenAI 호환 공급자가 요청 전체를 거부한다.
 */
function carriedToolHistory(items: Item[]): AgentMessage[] {
  const completed = items.filter(
    (item): item is Extract<Item, { kind: 'tool' }> =>
      item.kind === 'tool' && typeof item.output === 'string' && item.output.length > 0
  )
  const recent = completed.slice(-CARRIED_TOOL_RESULTS)
  if (recent.length === 0) return []

  const messages: AgentMessage[] = []
  let index = 0
  while (index < recent.length) {
    const turnId = recent[index].assistantTurnId
    const group: typeof recent = []
    while (index < recent.length && recent[index].assistantTurnId === turnId) {
      group.push(recent[index])
      index += 1
    }
    messages.push({
      role: 'assistant',
      content: '',
      toolCalls: group.map((tool) => ({
        id: tool.providerToolCallId,
        type: 'function' as const,
        function: { name: tool.name, arguments: tool.args }
      }))
    })
    for (const tool of group) {
      messages.push({ role: 'tool', content: tool.output ?? '', toolCallId: tool.providerToolCallId })
    }
  }
  return messages
}

function argPath(args: Record<string, unknown>): string {
  // 툴마다 주요 인자 키가 다르다 (path / command / pattern / query / src) — 카드·승인바에 표시할 값을 고른다
  for (const k of ['path', 'command', 'pattern', 'query', 'src', 'url']) {
    if (typeof args[k] === 'string' && args[k]) return args[k] as string
  }
  return ''
}

function AgentView({
  active,
  settings,
  backend,
  health,
  onPickWorkspace,
  onSaveSettings,
  conversationRequest,
  onConversationActive = () => {},
  requireProjectStart = false
}: Props): React.JSX.Element {
  const [items, setItems] = useState<Item[]>([])
  const [input, setInput] = useState('')
  const [attachments, setAttachments] = useState<AttachmentRef[]>([])
  const [running, setRunning] = useState(false)
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>('read')
  const [note, setNote] = useState<string | null>(null)
  const [transientNote, setTransientNote] = useState<string | null>(null)
  const [plan, setPlan] = useState<PlanStep[]>([])
  const [sidebarOpen, setSidebarOpen] = useState(false) // 우측 사이드바(계획+미리보기) 전체 토글
  const [showPreview, setShowPreview] = useState(true) // 사이드바 안 미리보기 하위 패널만
  const [previewPath, setPreviewPath] = useState<string | null>(null)
  const [previewReload, setPreviewReload] = useState(0)
  const [rag, setRag] = useState<RagStatus | null>(null)
  const [indexing, setIndexing] = useState(false)
  const [indexProg, setIndexProg] = useState<{ done: number; total: number } | null>(null)
  const [ragNote, setRagNote] = useState<string | null>(null)
  const [manualComfyProfiles, setManualComfyProfiles] = useState<ComfyModelProfile[]>([])
  const [manualComfyProfileId, setManualComfyProfileId] = useState('')
  const [manualComfyLoading, setManualComfyLoading] = useState(false)
  const [elapsed, setElapsed] = useState(0) // 현재 실행 경과(초) — '멈춘 것처럼 보임' 방지용 활동 표시
  const [liveTokens, setLiveTokens] = useState(0) // 이번 실행 누적 토큰(실시간 표시)
  const [nvidiaAgentCapability, setNvidiaAgentCapability] = useState<
    'idle' | 'loading' | 'supported' | 'blocked'
  >('idle')
  const [approvalSubmitting, setApprovalSubmitting] = useState<string | null>(null)
  const [approvalError, setApprovalError] = useState<string | null>(null)
  const approvalSubmittingRef = useRef<string | null>(null)
  const runStartRef = useRef(0)
  const runTokensRef = useRef(0) // 완료 시 사용량 기록용(마지막 usage.total)

  // 작업 폴더가 정해지면 사이드카에 미리보기 루트를 알려준다 (/f 정적 서빙용)
  useEffect(() => {
    if (
      active &&
      settings.activeLlmProvider !== 'nvidia' &&
      backend.state === 'ready' && backend.port && settings.workspace
    ) {
      fetch(`http://127.0.0.1:${backend.port}/preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ workspace: settings.workspace })
      }).catch(() => {})
    }
  }, [active, backend.state, backend.port, settings.activeLlmProvider, settings.workspace])

  // Metadata-only local cache lookup. Starting Agent never probes NVIDIA implicitly;
  // Main remains the authoritative gate and revalidates again before minting a grant.
  useEffect(() => {
    if (!active) return
    if (settings.activeLlmProvider !== 'nvidia') {
      setNvidiaAgentCapability('idle')
      return
    }
    let cancelled = false
    setNvidiaAgentCapability('loading')
    try {
      const target = snapshotLlmSettings(settings)
      if (!target.model) {
        setNvidiaAgentCapability('blocked')
        return
      }
      void window.api.nvidia.capabilities.status({
        deploymentMode: target.deploymentMode!,
        endpoint: target.endpoint,
        model: target.model
      }).then((snapshot) => {
        if (!cancelled) {
          setNvidiaAgentCapability(
            snapshot?.capabilities.tools === 'supported' ? 'supported' : 'blocked'
          )
        }
      }).catch(() => {
        if (!cancelled) setNvidiaAgentCapability('blocked')
      })
    } catch {
      setNvidiaAgentCapability('blocked')
    }
    return () => { cancelled = true }
  }, [
    active,
    settings.activeLlmProvider,
    settings.nvidiaDeploymentMode,
    settings.nvidiaModel,
    settings.nvidiaNimEndpoint
  ])

  // RAG 색인 상태 로드 (작업 폴더·백엔드 준비 시)
  useEffect(() => {
    if (
      active &&
      backend.state === 'ready' &&
      backend.port &&
      settings.workspace.trim()
    ) {
      ragStatus(backend.port, settings.workspace).then(setRag).catch(() => setRag(null))
    } else {
      setRag(null)
    }
  }, [active, backend.state, backend.port, settings.activeLlmProvider, settings.workspace])

  const previewUrl =
    backend.port && previewPath
      ? `http://127.0.0.1:${backend.port}/f/${previewPath.split('/').map(encodeURIComponent).join('/')}`
      : ''
  const abortRef = useRef<AbortController | null>(null)
  const sendGuardRef = useRef(false)
  const sessionRef = useRef<string>('')
  const historyRef = useRef<AgentMessage[]>([])
  const finalTextRef = useRef<string>('') // 이번 run의 마지막 assistant 답변 (툴콜 이후 초기화)
  // 하네스가 관측한 실행 사실 요약. 안전 한도로 멈춘 런에서만 온다. finalText와 달리
  // 툴콜마다 초기화하지 않는다 — 런 전체의 기록이기 때문이다.
  const runSummaryRef = useRef<string>('')
  // 이번 런이 안전 한도로 멈췄는지. 자동 이어가기 판단에만 쓴다.
  const runLimitRef = useRef<string>('')
  // 한 사용자 요청에서 자동으로 이어간 횟수. 무한 연장을 막는 유일한 장치다.
  const autoContinuesRef = useRef(0)
  // NDJSON events can arrive after a newer run starts.  Keep the active execution
  // identity outside React state so a stale image_result cannot mutate this timeline.
  const activeAssistantTurnIdRef = useRef<string>('')
  const autoIndexedRef = useRef<string>('') // 자동 색인을 이미 시도한 워크스페이스 (중복 방지)
  const scrollRef = useRef<HTMLDivElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)

  const newNvidiaSession = useCallback((): string => {
    const sessionId = typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `${Date.now()}.${Math.random()}`
    sessionRef.current = sessionId
    return sessionId
  }, [])

  // ── 대화방 (에이전트 작업 세션) ──
  const [convId, setConvId] = useState<string | null>(null)
  const [convTitle, setConvTitle] = useState('새 대화')
  const convIdRef = useRef<string | null>(null)

  const resetNvidiaSessionForConversation = (): void => {
    if (!nvidiaSelected) return
    const previousSessionId = sessionRef.current
    sessionRef.current = ''
    if (previousSessionId) {
      void window.api.nvidia.agent.finish({ sessionId: previousSessionId }).catch(() => {})
    }
  }

  const refreshManualComfyProfiles = useCallback(async (): Promise<void> => {
    setManualComfyLoading(true)
    try {
      const profiles = await listComfyProfilesForAgent(true)
      setManualComfyProfiles(profiles)
      // 수동 모드에서는 임의의 첫 모델을 고르지 않는다. 사용자가 명시적으로 선택해야 한다.
      setManualComfyProfileId((current) =>
        profiles.some((profile) => profile.id === current) ? current : ''
      )
    } finally {
      setManualComfyLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!active) return
    if (settings.comfyModelSelectionMode === 'manual') {
      void refreshManualComfyProfiles()
      return
    }
    setManualComfyProfiles([])
    setManualComfyProfileId('')
  }, [
    active,
    settings.comfyModelSelectionMode,
    refreshManualComfyProfiles
  ])

  // 실행이 끝나면 세션 저장 (스크린샷은 용량 커서 제외 — 재실행 시 재생성됨)
  useEffect(() => {
    const id = convIdRef.current
    if (!id || running || items.length === 0) return
    const lean = items.map((i) => (i.kind === 'tool' && i.screenshot ? { ...i, screenshot: undefined } : i))
    window.api.conversations
      .save({
        id,
        kind: 'agent',
        title: convTitle,
        data: { items: lean, history: historyRef.current, plan, workspace: settings.workspace }
      })
      .then(() => window.dispatchEvent(new Event('aiso:conversations-changed')))
      .catch(() => {})
  }, [items, plan, running, convTitle])

  const newConv = (announce = true): void => {
    if (running) return
    resetNvidiaSessionForConversation()
    convIdRef.current = null
    setConvId(null)
    setConvTitle('새 대화')
    setItems([])
    setAttachments([])
    setPlan([])
    historyRef.current = []
    setNote(null)
    setTransientNote(null)
    if (announce) onConversationActive(null)
    // 새 대화는 작업 폴더 미선택 상태로 시작 — 작업 폴더는 대화(작업 세션)마다 따로 고른다
    if (settings.workspace) void onSaveSettings({ workspace: '' })
  }
  useEffect(() => {
    const onRenamed = (event: Event): void => {
      const detail = (event as CustomEvent<{ id?: unknown; title?: unknown }>).detail
      if (detail?.id === convIdRef.current && typeof detail.title === 'string') {
        setConvTitle(detail.title)
      }
    }
    window.addEventListener('aiso:conversation-renamed', onRenamed)
    return () => window.removeEventListener('aiso:conversation-renamed', onRenamed)
  }, [])
  const selectConv = async (id: string, announce = true): Promise<void> => {
    if (running || id === convId) return
    const c = await window.api.conversations.get(id)
    if (!c) return
    resetNvidiaSessionForConversation()
    const d = (c.data ?? {}) as { items?: Item[]; history?: AgentMessage[]; plan?: PlanStep[]; workspace?: string }
    convIdRef.current = id
    setConvId(id)
    setConvTitle(c.title)
    setItems(d.items ?? [])
    setPlan(d.plan ?? [])
    historyRef.current = d.history ?? []
    setNote(null)
    setTransientNote(null)
    if (announce) onConversationActive(id)
    // 작업 세션마다 작업 폴더가 다를 수 있으니 저장된 폴더로 복원
    if (d.workspace && d.workspace !== settings.workspace) void onSaveSettings({ workspace: d.workspace })
  }
  const consumedRequestRef = useRef(0)
  useEffect(() => {
    if (conversationRequest?.kind !== 'agent' || conversationRequest.nonce === consumedRequestRef.current) return
    consumedRequestRef.current = conversationRequest.nonce
    if (conversationRequest.id) void selectConv(conversationRequest.id, false)
    else newConv(false)
  }, [conversationRequest])

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [items])

  // 실행 중엔 경과 시간을 계속 갱신 — 모델이 조용히 생성(생각)만 하는 구간에도
  // 시간이 흐르는 게 보여서 '멈춘 것처럼' 보이지 않는다.
  useEffect(() => {
    if (!running) return
    runStartRef.current = Date.now()
    setElapsed(0)
    const t = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - runStartRef.current) / 1000))
    }, 500)
    return () => window.clearInterval(t)
  }, [running])

  const backendReady = backend.state === 'ready' && backend.port != null
  const hasWorkspace = settings.workspace.trim().length > 0
  const ollamaOk = health?.ollama === true
  const nvidiaSelected = settings.activeLlmProvider === 'nvidia'
  // 작업 폴더는 선택 사항 — 없어도 웹 조사·스킬 제작은 가능(백엔드가 로컬 도구를 잠근다).
  const ready = backendReady && (
    nvidiaSelected ? nvidiaAgentCapability === 'supported' : ollamaOk
  )
  const canSend =
    ready && !running && input.trim().length > 0 && (!requireProjectStart || convIdRef.current != null)

  // ---- 이벤트 → 타임라인 리듀서 (순수: 기존 객체를 변형하지 않는다) ----
  const reduce = (ev: AgentEvent): void => {
    // Check before scheduling React state work. The stream can finish and clear
    // the active ref before a functional setItems updater is eventually run.
    if (ev.type === 'image_result' && ev.assistantTurnId !== activeAssistantTurnIdRef.current) {
      return
    }
    // 마지막 assistant 답변 추적 (setItems 밖에서 1회만 실행 → StrictMode 이중호출 영향 없음)
    if (ev.type === 'content') finalTextRef.current += ev.text
    else if (ev.type === 'tool_call') {
      // A backend notice describes only the transition into a tool call.  It
      // must not survive once that call actually starts.
      setTransientNote(null)
      finalTextRef.current = ''
      // 방금 다룬 HTML을 우측 미리보기 대상으로 추적
      const p = typeof ev.args.path === 'string' ? ev.args.path : ''
      if (p && /\.html?$/i.test(p) && ['write_file', 'run_web', 'edit_file'].includes(ev.name)) {
        setPreviewPath(p.replace(/\\/g, '/'))
      }
    } else if (ev.type === 'usage') {
      runTokensRef.current = ev.total
      setLiveTokens(ev.total)
      return
    } else if (ev.type === 'run_summary') {
      runSummaryRef.current = ev.text
      return
    } else if (ev.type === 'run_limit') {
      // 한국어 안내문을 파싱하지 않는다 — 문구를 고칠 때마다 이어가기가 깨진다.
      runLimitRef.current = ev.reason
      return
    } else if (ev.type === 'notice') {
      if (ev.transient) {
        setTransientNote(ev.text)
      } else {
        setTransientNote(null)
        setNote(ev.text)
      }
      return
    } else if (ev.type === 'plan') {
      setPlan(ev.steps)
      return
    } else if (ev.type === 'screenshot') {
      setPreviewReload((n) => n + 1) // 파일이 방금 실행됨 → 미리보기 새로고침
    }

    if (ev.type === 'error') {
      setTransientNote(null)
      setItems((prev) => [
        ...prev.map((i) => (i.kind === 'assistant' ? { ...i, streaming: false } : i)),
        { kind: 'assistant', text: `⚠ ${ev.error}`, thinking: '', streaming: false }
      ])
      return
    }

    // Keep run-scoped guidance out of the next idle state even when a terminal
    // event and the timeline update are batched by React.
    if (ev.type === 'tool_result' || ev.type === 'image_result' || ev.type === 'done') {
      setTransientNote(null)
    }

    setItems((prev) => {
      const next = prev.slice()
      const li = next.length - 1
      const last = next[li]
      const openAsst = last && last.kind === 'assistant' && last.streaming ? last : null

      if (ev.type === 'thinking') {
        if (openAsst) next[li] = { ...openAsst, thinking: openAsst.thinking + ev.text }
        else next.push({ kind: 'assistant', text: '', thinking: ev.text, streaming: true })
      } else if (ev.type === 'content') {
        if (openAsst) next[li] = { ...openAsst, text: openAsst.text + ev.text }
        else next.push({ kind: 'assistant', text: ev.text, thinking: '', streaming: true })
      } else if (ev.type === 'tool_call') {
        if (openAsst) next[li] = { ...openAsst, streaming: false }
        // update_plan은 카드로 그리지 않고 계획 패널에서 표시
        if (ev.name !== 'update_plan') {
          next.push({
            kind: 'tool',
            callId: ev.executionId,
            approvalId: ev.approvalId,
            providerToolCallId: ev.providerToolCallId,
            assistantTurnId: ev.assistantTurnId,
            name: ev.name,
            args: ev.args,
            status: 'running'
          })
        }
      } else if (ev.type === 'approval_request') {
        setApprovalError(null)
        return next.map((i) =>
          i.kind === 'tool' && i.callId === ev.executionId
            ? { ...i, approvalId: ev.approvalId, status: 'awaiting' as const }
            : i
        )
      } else if (ev.type === 'tool_result') {
        setApprovalSubmitting((current) => current === ev.executionId ? null : current)
        setApprovalError(null)
        return next.map((i) =>
          i.kind === 'tool' && i.callId === ev.executionId
            ? {
                ...i,
                status: ev.ok
                  ? ('done' as const)
                  : ev.expired
                    ? ('expired' as const)
                    : ev.rejected
                      ? ('rejected' as const)
                      : ('error' as const),
                output: ev.output
              }
            : i
        )
      } else if (ev.type === 'screenshot') {
        return next.map((i) =>
          i.kind === 'tool' && i.callId === ev.id ? { ...i, screenshot: ev.data } : i
        )
      } else if (ev.type === 'image_result') {
        next.push({ kind: 'image', image: ev.image })
      } else if (ev.type === 'done') {
        if (openAsst) next[li] = { ...openAsst, streaming: false }
      }
      return next
    })
  }

  const decide = async (callId: string, approvalId: string, approved: boolean): Promise<void> => {
    if (backend.port == null || approvalSubmittingRef.current === callId) return
    approvalSubmittingRef.current = callId
    setApprovalSubmitting(callId)
    setApprovalError(null)
    try {
      await approveAgent(backend.port, sessionRef.current, approvalId, approved)
      setItems((prev) =>
        prev.map((i) =>
          i.kind === 'tool' && i.callId === callId && i.status === 'awaiting'
            ? { ...i, status: approved ? 'running' : 'rejected' }
            : i
        )
      )
    } catch (err) {
      console.error('승인 전송 실패:', err)
      setApprovalError((err as Error).message)
    } finally {
      if (approvalSubmittingRef.current === callId) approvalSubmittingRef.current = null
      setApprovalSubmitting((current) => current === callId ? null : current)
    }
  }

  /**
   * @param continueWith 사용자가 입력하지 않은 자동 이어가기 지시. 주면 입력창을
   *   읽지도 비우지도 않는다.
   */
  const send = async (continueWith?: string): Promise<void> => {
    const text = (continueWith ?? input).trim()
    if (!text || !ready || running || sendGuardRef.current) return
    if (requireProjectStart && !convIdRef.current) {
      setNote('왼쪽 사이드바에서 프로젝트를 만들고 “프로젝트 시작”을 눌러 대화를 시작해 주세요.')
      return
    }
    sendGuardRef.current = true
    const ac = new AbortController()
    abortRef.current = ac
    if (nvidiaSelected) {
      if (!sessionRef.current) newNvidiaSession()
    } else {
      sessionRef.current =
        typeof crypto !== 'undefined' && 'randomUUID' in crypto
          ? crypto.randomUUID()
          : String(Math.random())
    }
    const assistantTurnId =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : `${Date.now()}.${Math.random()}`
    let executionStarted = false
    runLimitRef.current = ''
    if (continueWith === undefined) autoContinuesRef.current = 0

    try {
      // A completion-looking assistant sentence is not evidence that ComfyUI
      // produced an image.  Only a real, rendered image timeline item unlocks
      // contextual correction intent for this request.
      const imageContextVerified = items.some((item) => item.kind === 'image')
      const previousAssistant = imageContextVerified
        ? [...historyRef.current]
          .reverse()
          .find((message) => message.role === 'assistant')?.content ?? ''
        : ''
      const imageRequested = looksLikeImageGenerationRequest(
        text,
        previousAssistant,
        imageContextVerified
      )
      const manualSelection = settings.comfyModelSelectionMode === 'manual'
      const availableComfyProfiles = await listComfyProfilesForAgent(manualSelection)
      const selectedComfyProfile = availableComfyProfiles.find(
        (profile) => profile.id === manualComfyProfileId
      )
      if (manualSelection && imageRequested && !selectedComfyProfile) {
        throw new Error(
          availableComfyProfiles.length === 0
            ? '수동 선택에 사용할 준비된 ComfyUI 모델이 없습니다. 설정에서 모델과 워크플로 연결 상태를 확인하세요.'
            : '수동 선택 모드입니다. 입력창 아래에서 이미지 생성 모델을 선택한 뒤 다시 실행하세요.'
        )
      }
      const comfyProfiles = manualSelection
        ? selectedComfyProfile ? [selectedComfyProfile] : []
        : availableComfyProfiles
      const selectedComfyModelId = manualSelection ? selectedComfyProfile?.id ?? null : null

      if (comfyProfiles.length > 0 && imageRequested) {
        setNote('ComfyUI 실행 상태를 확인하고 있습니다…')
        await ensureComfyReadyForAgent(
          backend.port!,
          settings.comfyBaseUrl,
          settings.comfyInstallPath,
          ac.signal
        )
        setNote(null)
      }
      const nvidiaGrantId = nvidiaSelected
          ? (await window.api.nvidia.agent.prepare({
              sessionId: sessionRef.current,
              assistantTurnId,
              approvalMode,
              ...(selectedComfyModelId ? { selectedComfyModelId } : {})
            })).grantId
        : undefined

      // Main의 exact approval/credential/capability 재검증이 성공한 뒤에만 대화를 변경한다.
      if (!convIdRef.current) {
        const id = newConversationId()
        convIdRef.current = id
        setConvId(id)
        setConvTitle(titleFromText(text))
        onConversationActive(id)
      }
      if (continueWith === undefined) setInput('')
      if (taRef.current) taRef.current.style.height = 'auto'
      const pendingAttachments = attachments
      runSummaryRef.current = ''
      historyRef.current = [...historyRef.current, { role: 'user', content: text, attachments: pendingAttachments }]
      finalTextRef.current = ''
      setNote(null)
      setTransientNote(null)
      setPlan([])
      setItems((prev) => [...prev, { kind: 'user', text, attachments: pendingAttachments }])
      setRunning(true)
      setAttachments([])
      setLiveTokens(0)
      runTokensRef.current = 0
      activeAssistantTurnIdRef.current = assistantTurnId
      executionStarted = true

      await streamAgent(
        backend.port!,
        settings,
        settings.workspace,
        [...historyRef.current.slice(0, -1), ...carriedToolHistory(items), ...historyRef.current.slice(-1)],
        sessionRef.current,
        assistantTurnId,
        approvalMode,
        comfyProfiles,
        reduce,
        ac.signal,
        {
          selectedComfyModelId: selectedComfyModelId ?? undefined,
          nvidiaGrantId
        },
        imageContextVerified,
        convIdRef.current ?? ''
      )
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        if (executionStarted) {
          setTransientNote(null)
          reduce({ type: 'error', error: (err as Error).message })
        } else {
          setNote((err as Error).message)
        }
      }
    } finally {
      // Network termination, user cancellation, and malformed streams may end
      // without a terminal SSE event.  Never leave a run-scoped notice behind.
      if (executionStarted) setTransientNote(null)
      if (activeAssistantTurnIdRef.current === assistantTurnId) {
        activeAssistantTurnIdRef.current = ''
      }
      setRunning(false)
      sendGuardRef.current = false
      abortRef.current = null
      // 이번 실행에서 쓴 토큰을 사용량 통계에 기록 (홈의 일/주/월 집계)
      if (executionStarted && runTokensRef.current > 0) {
        void window.api.usage.record(runTokensRef.current)
        // 이번 답변 아래에 토큰·소요시간 요약줄을 남긴다
        const secs = (Date.now() - runStartRef.current) / 1000
        setItems((prev) => [...prev, { kind: 'meta', tokens: runTokensRef.current, seconds: secs }])
      }
      // 이번 run의 마지막 assistant 답변을 대화 히스토리에 반영 (ref 기반 → 중복 없음)
      // 안전 한도로 멈춘 경우 하네스의 실행 사실 요약을 함께 남긴다. 예전에는
      // "여기까지 한 내용은 유지됩니다"라고 안내하면서 정작 아무것도 넘기지 않아,
      // '계속해줘'가 백지에서 다시 시작했다.
      if (executionStarted) {
        const carried = [finalTextRef.current.trim(), runSummaryRef.current.trim()]
          .filter(Boolean)
          .join(`\n\n`)
        if (carried) {
          historyRef.current = [...historyRef.current, { role: 'assistant', content: carried }]
        }
      }
      // 재색인은 백엔드에서 백그라운드로 돌므로, 잠시 뒤 색인 칩 수치를 갱신한다
      if (backend.port && settings.workspace.trim()) {
        const port = backend.port
        window.setTimeout(() => {
          ragStatus(port, settings.workspace).then(setRag).catch(() => {})
        }, 1500)
      }
      if (nvidiaSelected) {
        const completedSessionId = sessionRef.current
        await window.api.nvidia.agent.finish({ sessionId: completedSessionId }).catch(() => {})
        sessionRef.current = ''
      }
    }

    // 안전 한도로 멈췄고 사용자가 켜 뒀다면 스스로 이어간다.
    // 이 시점에는 이번 런의 실행 요약과 최근 도구 결과가 이미 historyRef 에 실려 있어
    // 백지에서 다시 시작하지 않는다 — 그게 이 기능이 성립하는 이유다.
    if (
      settings.autoContinueOnLimit &&
      runLimitRef.current &&
      executionStarted &&
      !ac.signal.aborted &&          // 사용자가 정지를 눌렀으면 이어가지 않는다
      autoContinuesRef.current < MAX_AUTO_CONTINUES
    ) {
      autoContinuesRef.current += 1
      const nth = autoContinuesRef.current
      setNote(`안전 한도에서 멈춰 이어서 진행합니다 (${nth}/${MAX_AUTO_CONTINUES}). 중지하려면 정지를 누르세요.`)
      // 다음 런이 시작되기 전에 정리 상태가 반영되도록 한 틱 넘긴다.
      window.setTimeout(() => { void send(AUTO_CONTINUE_PROMPT) }, 0)
    } else if (settings.autoContinueOnLimit && runLimitRef.current && autoContinuesRef.current >= MAX_AUTO_CONTINUES) {
      setNote(
        `자동 이어가기 ${MAX_AUTO_CONTINUES}회를 모두 썼습니다. 계속하려면 '계속해줘'라고 해주세요 — ` +
        '요청을 더 작게 나누면 한도에 덜 걸립니다.'
      )
    }
  }

  const stop = (): void => {
    abortRef.current?.abort()
    setTransientNote(null)
    // 승인 대기 중인 툴은 거부 처리 → 백엔드 홀딩 해제
    items.forEach((i) => {
      if (i.kind === 'tool' && i.status === 'awaiting') {
        void decide(i.callId, i.approvalId, false)
      }
    })
  }

  const awaiting = items.find((i) => i.kind === 'tool' && i.status === 'awaiting') as
    | Extract<Item, { kind: 'tool' }>
    | undefined

  // 실행 중 활동 표시기용 — 현재 어떤 단계인지(툴 실행 중 / 생각 중)
  const runningTool = running
    ? ([...items].reverse().find((i) => i.kind === 'tool' && i.status === 'running') as
        | Extract<Item, { kind: 'tool' }>
        | undefined)
    : undefined
  const activityLabel = runningTool
    ? `${TOOL_LABEL[runningTool.name] ?? runningTool.name} 실행 중`
    : '생각 중'
  const fmtElapsed = (s: number): string =>
    s < 60 ? `${s}초` : `${Math.floor(s / 60)}분 ${s % 60}초`
  const fmtTokens = (n: number): string => n.toLocaleString('en-US')
  const fmtDuration = (s: number): string =>
    s < 60 ? `${s.toFixed(1)}초` : `${Math.floor(s / 60)}분 ${Math.round(s % 60)}초`


  // 작업 폴더를 임베딩 색인 (진행 상황 표시). 임베딩 모델은 채팅 모델과 무관.
  const runIndex = async (): Promise<void> => {
    if (!backendReady || !hasWorkspace || indexing) return
    autoIndexedRef.current = settings.workspace // 이 워크스페이스는 (자동/수동) 색인 시도함 → 자동 재트리거 방지
    setIndexing(true)
    setRagNote(null)
    setIndexProg({ done: 0, total: 0 })
    try {
      await ragIndex(backend.port!, settings, settings.workspace, (e) => {
        if (e.type === 'progress') setIndexProg({ done: e.done, total: e.total })
        else if (e.type === 'done') {
          setIndexProg(null)
          setRag({
            indexed: e.count > 0,
            count: e.count,
            files: e.files,
            embed_model: e.embed_model,
            dim: e.dim
          })
          setRagNote(
            e.count === 0
              ? 'RAG로 참고할 코드·문서를 찾지 못했습니다 (작업 폴더에 텍스트 파일이 있는지 확인하세요).'
              : e.file_limit_reached
                ? `폴더가 커서 일부만 RAG에 넣었습니다 (최소 ${e.total_found ?? e.files}개 중 ${e.files}개 파일). 더 넣으려면 설정 → RAG의 '색인 범위(최대 파일 수)'를 늘리세요.`
                : e.truncated
                  ? '문서 또는 청크 상한으로 일부 내용만 RAG에 넣었습니다. 필요한 문서는 짧게 나누거나 색인 범위를 조정하세요.'
                  : null
          )
        } else if (e.type === 'error') setRagNote(e.error)
      })
    } catch (err) {
      setRagNote((err as Error).message)
    } finally {
      setIndexing(false)
      setIndexProg(null)
      if (backendReady && hasWorkspace) {
        ragStatus(backend.port!, settings.workspace).then(setRag).catch(() => {})
      }
    }
  }

  // 색인이 없으면 자동으로 한 번 색인한다 (매번 수동 클릭 불필요). 이후 파일 변경은 백엔드가
  // 백그라운드로 재색인하므로 최초 1회만 트리거하면 된다.
  useEffect(() => {
    if (!active || !settings.ragEnabled || !backendReady || !hasWorkspace || indexing) return
    if (rag == null || rag.indexed) return // 상태 미확인 or 이미 색인됨
    if (autoIndexedRef.current === settings.workspace) return // 이 워크스페이스는 이미 시도함
    // 임베딩 모델이 설치돼 있을 때만 (health 로드 대기 + 미설치면 조용히 대기 — 수동 버튼으로 처리)
    // 태그 무시 비교: 'bge-m3' vs 'bge-m3:latest'
    if (!health || !modelInstalled(health.models, settings.embeddingModel)) return
    void runIndex()
    // runIndex는 매 렌더 재생성되므로 deps에서 제외(안정적 트리거 조건만 관찰)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    rag,
    active,
    backendReady,
    hasWorkspace,
    indexing,
    nvidiaSelected,
    settings.ragEnabled,
    settings.workspace,
    settings.embeddingModel,
    health
  ])

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void send()
    }
  }
  const autoGrow = (e: React.FormEvent<HTMLTextAreaElement>): void => {
    const ta = e.currentTarget
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(ta.scrollHeight, 120)}px`
  }

  const wsName = hasWorkspace ? settings.workspace.replace(/\\/g, '/').split('/').filter(Boolean).pop() : null

  const modelOptions: DropdownOption[] = nvidiaSelected
    ? settings.nvidiaModel
      ? [{ value: settings.nvidiaModel, label: settings.nvidiaModel }]
      : []
    : health?.models && health.models.length
      ? health.models.map((m) => ({ value: m, label: m }))
      : settings.model
        ? [{ value: settings.model, label: settings.model }]
        : []
  const manualComfyOptions: DropdownOption[] = manualComfyProfiles.map((profile) => ({
    value: profile.id,
    label: profile.name,
    hint: profile.tags.slice(0, 3).join(', ') || '준비된 ComfyUI 모델'
  }))

  let notice: { text: string; kind: 'err' | 'warn' } | null = null
  if (backend.state !== 'ready') notice = { text: '백엔드 엔진 준비 중…', kind: 'warn' }
  else if (nvidiaSelected && nvidiaAgentCapability === 'loading') {
    notice = { text: '저장된 NVIDIA 도구 기능 확인 상태를 읽고 있습니다…', kind: 'warn' }
  } else if (nvidiaSelected && nvidiaAgentCapability !== 'supported') {
    notice = {
      text: 'NVIDIA Agent를 사용하려면 설정에서 현재 모델의 도구 기능을 검사해 tools=supported 상태를 확인하세요.',
      kind: 'warn'
    }
  } else if (!ollamaOk && !nvidiaSelected) notice = { text: 'Ollama에 연결할 수 없습니다 — Ollama 앱을 실행하세요', kind: 'err' }

  return (
    <div className="convshell">
      <div className="agent">
      <section className="agent__main view view--chat">
      <header className="view__head view__head--row">
        <div>
          <h1>에이전트</h1>
          <p className="view__desc">로컬 파일을 직접 읽고 편집·생성·삭제하는 하네스</p>
        </div>
        <button
          className={`iconbtn ${sidebarOpen ? 'iconbtn--on' : ''}`}
          data-tip={sidebarOpen ? '사이드바 끄기' : '사이드바 켜기'}
          aria-label="사이드바 토글"
          onClick={() => setSidebarOpen((v) => !v)}
        >
          <PanelRightIcon />
        </button>
      </header>

      <div className="chat-scroll" ref={scrollRef}>
        {items.length === 0 ? (
          <div className="empty empty--borderless">
            <div className="empty__icon">
              <AgentIcon size={18} />
            </div>
            <div className="empty__title">파일 작업을 맡겨보세요</div>
            <div className="empty__desc">
              “README 만들어줘”, “utils.py에서 버그 찾아 고쳐줘” 처럼 지시하면
              <br />
              작업 폴더 안에서 파일을 읽고 편집합니다 · 쓰기 작업은 승인 후 실행됩니다
            </div>
          </div>
        ) : (
          items.map((it, i) => {
            if (it.kind === 'user') return (
              <div key={i} className="msg msg--user">
                <div className="msg__bubble">
                  {it.text}
                  {it.attachments && it.attachments.length > 0 && (
                    <div className="msg__attachments">첨부: {it.attachments.map((item) => item.name).join(', ')}</div>
                  )}
                </div>
              </div>
            )
            if (it.kind === 'assistant') {
              return (
                <div key={i} className="msg msg--assistant">
                  {it.thinking && (
                    <details className="think">
                      <summary>사고 과정</summary>
                      <div className="think__body">{it.thinking}</div>
                    </details>
                  )}
                  {it.text && <Markdown text={it.text} />}
                  {it.streaming && !it.text && !it.thinking && (
                    <div className="msg__body msg__body--pending">생각 중…</div>
                  )}
                  {it.streaming && (it.text || it.thinking) && <span className="caret" />}
                </div>
              )
            }
            if (it.kind === 'meta')
              return (
                <div key={i} className="run-meta mono">
                  {fmtTokens(it.tokens)} 토큰 · {fmtDuration(it.seconds)}
                </div>
              )
            if (it.kind === 'image')
              return <GeneratedImage key={i} image={it.image} backendPort={backend.port} />
            // tool
            const path = argPath(it.args)
            const ToolIcon =
              it.name === 'run_web' || it.name === 'web_fetch'
                ? GlobeIcon
                : it.name === 'run_code'
                  ? CodeIcon
                  : it.name === 'run_command'
                    ? TerminalIcon
                    : it.name === 'grep' || it.name === 'glob' || it.name === 'search_docs' || it.name === 'web_search'
                      ? SearchIcon
                      : it.name === 'list_dir' || it.name === 'list_tree'
                        ? FolderIcon
                        : FileIcon
            return (
              <div key={i} className={`tool tool--${it.status}`}>
                <div className="tool__head">
                  <ToolIcon />
                  <span className="tool__name">{TOOL_LABEL[it.name] ?? it.name}</span>
                  {path && <span className="tool__path mono">{path}</span>}
                  <span className={`tool__badge tool__badge--${it.status}`}>
                    {it.status === 'running' &&
                      (it.name === 'run_web'
                        ? '브라우저 검증 중'
                        : it.name === 'run_code'
                          ? '코드 검증 중'
                          : it.name === 'run_command'
                            ? '명령 실행 중'
                            : '실행 중')}
                    {it.status === 'awaiting' && '승인 대기'}
                    {it.status === 'done' && '완료'}
                    {it.status === 'error' && '오류'}
                    {it.status === 'rejected' && '거부됨'}
                    {/* 무응답을 '거부됨'으로 보여 주면 자리를 비웠던 사용자가 자기가 거부했다고 오해한다. */}
                    {it.status === 'expired' && '응답 없음'}
                  </span>
                </div>

                {it.name === 'run_web' && (
                  <div className="tool__caption">
                    {it.status === 'running'
                      ? '헤드리스 브라우저를 띄워 실행·검증 중…'
                      : '검증용 브라우저는 실행 후 자동 종료됩니다 (백그라운드에 남지 않음).'}
                  </div>
                )}

                {it.name === 'write_file' && typeof it.args.content === 'string' && (
                  <pre className="tool__preview mono">{(it.args.content as string).slice(0, 800)}</pre>
                )}
                {it.name === 'edit_file' && (
                  <pre className="tool__preview mono">
                    <span className="diff-del">- {String(it.args.old_string ?? '').slice(0, 300)}</span>
                    {'\n'}
                    <span className="diff-add">+ {String(it.args.new_string ?? '').slice(0, 300)}</span>
                  </pre>
                )}

                {it.status === 'awaiting' && (
                  <div className="tool__approve">
                    <span className="tool__approve-q">아래 승인 바에서 승인/거부하세요</span>
                  </div>
                )}
                {it.output && it.status !== 'awaiting' && (
                  <pre className="tool__output mono">{it.output.slice(0, 1200)}</pre>
                )}
                {it.screenshot && (
                  <img
                    className="tool__shot"
                    src={`data:image/png;base64,${it.screenshot}`}
                    alt="실행 화면"
                  />
                )}
              </div>
            )
          })
        )}
      </div>

      {awaiting && (
        <div className="approve-bar">
          <span className="approve-bar__mark">승인 필요</span>
          <span className="approve-bar__desc">
            <b>{TOOL_LABEL[awaiting.name] ?? awaiting.name}</b>
            {argPath(awaiting.args) && <span className="mono"> · {argPath(awaiting.args)}</span>}
          </span>
          {approvalError && <span className="approve-bar__error" role="alert">{approvalError}</span>}
          <button
            className="btn btn--sm"
            disabled={approvalSubmitting === awaiting.callId}
            onClick={() => void decide(awaiting.callId, awaiting.approvalId, true)}
          >
            {approvalSubmitting === awaiting.callId ? '처리 중…' : '승인'}
          </button>
          <button
            className="btn btn--sm btn--ghost2"
            disabled={approvalSubmitting === awaiting.callId}
            onClick={() => void decide(awaiting.callId, awaiting.approvalId, false)}
          >
            거부
          </button>
        </div>
      )}

      {ragNote && !awaiting && <div className="notice notice--warn">{ragNote}</div>}
      {note && !awaiting && <div className="notice notice--warn">{note}</div>}
      {transientNote && running && !awaiting && <div className="notice notice--warn">{transientNote}</div>}
      {notice && !awaiting && !note && !ragNote && (
        <div className={`notice notice--${notice.kind}`}>{notice.text}</div>
      )}

      {running && !awaiting && (
        <div className="activity" aria-live="polite">
          <span className="activity__dots">
            <i />
            <i />
            <i />
          </span>
          <span className="activity__label">{activityLabel}</span>
          {liveTokens > 0 && (
            <span className="activity__tokens mono">· {fmtTokens(liveTokens)} 토큰</span>
          )}
          <span className="activity__time mono">· {fmtElapsed(elapsed)}</span>
        </div>
      )}

      <div className="composer-head">
        <button
          className="ws-pick"
          onClick={() => void onPickWorkspace()}
          data-tip={settings.workspace || '작업 폴더 선택'}
        >
          <FolderIcon />
          <span className="mono">{wsName ?? '작업 폴더 선택'}</span>
        </button>
        {hasWorkspace && (
          <button
            className={`ws-pick rag-chip ${rag?.indexed ? 'rag-chip--on' : ''}`}
            onClick={() => void runIndex()}
            disabled={indexing || !backendReady}
            data-tip={
              rag?.indexed
                ? `RAG 활성 · 조각 ${rag.count}개 (${rag.embed_model}) · 클릭하면 다시 만듭니다`
                : 'RAG(검색 증강) — 작업 폴더를 의미 검색용으로 준비해 에이전트가 관련 코드·문서를 자동 참고 (자동 실행)'
            }
          >
            <DatabaseIcon />
            <span>
              {indexing
                ? indexProg && indexProg.total
                  ? `RAG ${indexProg.done}/${indexProg.total}`
                  : 'RAG 준비 중…'
                : rag?.indexed
                  ? `RAG ${rag.count}`
                  : 'RAG'}
            </span>
          </button>
        )}
      </div>

      <div className="agent-composer">
        <textarea
          ref={taRef}
          className="agent-composer__ta"
          rows={1}
          value={input}
          disabled={!ready || running}
          placeholder={
            ready
              ? '무엇이든 요청하세요'
              : '엔진 준비 후 사용 가능'
          }
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          onInput={autoGrow}
        />
        <div className="agent-composer__footer">
          <AttachmentPicker value={attachments} onChange={setAttachments} disabled={!ready || running} />

          <Dropdown
            value={approvalMode}
            options={APPROVAL_OPTIONS}
            onChange={(v) => setApprovalMode(v as ApprovalMode)}
            align="left"
            title="승인 모드 — 수동(모든 작업 확인) · 읽기(생성·변경 확인) · 자동(승인 없이 실행)"
          />

          {settings.comfyModelSelectionMode === 'manual' && (
            <div className="agent-composer__image-model">
            <Dropdown
              value={manualComfyProfileId}
              options={manualComfyOptions}
              onChange={setManualComfyProfileId}
              align="left"
              disabled={running || manualComfyLoading}
              title="수동 선택 모드의 이미지 생성 모델"
              placeholder={manualComfyLoading ? '모델 목록 불러오는 중…' : '모델 선택'}
            />
            <button
              className="iconbtn"
              type="button"
              data-tip="이미지 모델 목록 새로고침"
              aria-label="이미지 모델 목록 새로고침"
              onClick={() => void refreshManualComfyProfiles()}
              disabled={running || manualComfyLoading}
              >
                <RefreshIcon />
              </button>
            </div>
          )}

          <div className="agent-composer__controls">
            <Dropdown
              value={nvidiaSelected ? settings.nvidiaModel : settings.model}
              options={modelOptions}
              onChange={(v) => void onSaveSettings(nvidiaSelected ? { nvidiaModel: v } : { model: v })}
              mono
              align="right"
              title="모델"
              placeholder="모델"
            />
            <Dropdown
              value={settings.reasoningEffort}
              options={EFFORT_OPTIONS}
              onChange={(v) => void onSaveSettings({ reasoningEffort: v as ReasoningEffort })}
              align="right"
              title="추론 성능"
            />
          {items.length > 0 && (
            <button
              className="iconbtn agent-composer__new"
              data-tip="새 대화 시작"
              aria-label="새 대화 시작"
              onClick={() => newConv()}
              disabled={running}
            >
              <TrashIcon />
            </button>
          )}
          </div>

          {running ? (
            <button className="agent-composer__send agent-composer__send--stop" onClick={stop}>중지</button>
          ) : (
            <button
              className="agent-composer__send"
              aria-label="실행"
              title="실행"
              disabled={!canSend}
              onClick={() => void send()}
            >
              <span aria-hidden="true">↑</span>
            </button>
          )}
        </div>
      </div>
      </section>

      {sidebarOpen && (plan.length > 0 || showPreview) && (
        <aside className="agent__side">
          {plan.length > 0 && (
            <div className="side-plan">
              <div className="plan__title">
                작업 계획
                <span className="plan__count">
                  {plan.filter((s) => s.status === 'completed').length}/{plan.length}
                </span>
              </div>
              <div className="side-plan__list">
                {plan.map((s, i) => (
                  <div key={i} className={`plan__step plan__step--${s.status}`}>
                    <span className="plan__mark">
                      {s.status === 'completed' ? '✓' : s.status === 'in_progress' ? '▸' : '○'}
                    </span>
                    <span className="plan__text">{s.content}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {showPreview && (
            <div className="side-preview">
              <div className="preview__bar">
                <span className="preview__label">미리보기</span>
                <span className="preview__file mono">{previewPath ?? '—'}</span>
                <button
                  className="iconbtn"
                  data-tip="새로고침"
                  aria-label="미리보기 새로고침"
                  disabled={!previewUrl}
                  onClick={() => setPreviewReload((n) => n + 1)}
                >
                  <RefreshIcon />
                </button>
                <button
                  className="iconbtn"
                  data-tip="미리보기 닫기"
                  aria-label="미리보기 닫기"
                  onClick={() => setShowPreview(false)}
                >
                  <CloseIcon />
                </button>
              </div>
              <div className="preview__body">
                {previewUrl ? (
                  // sandbox: 스크립트·자체 오리진 자산·폼은 허용(미리보기 정상)하되 팝업(window.open)과
                  // 최상위 네비게이션은 차단 — 사이드카가 /f/ 응답에 붙이는 PREVIEW_CSP가
                  // 못 막는 window.open 외부 유출 경로를 닫는다(CSP에는 window.open을 막는
                  // 지시어가 없다 — navigate-to는 스펙에서 폐기됐다).
                  // (iframe은 앱과 cross-origin이라 allow-same-origin+allow-scripts로도 샌드박스 자기해제 불가.)
                  <iframe
                    key={previewReload}
                    className="preview__frame"
                    src={previewUrl}
                    title="미리보기"
                    sandbox="allow-scripts allow-same-origin allow-forms"
                  />
                ) : (
                  <div className="preview__empty">
                    아직 미리볼 HTML이 없습니다.
                    <br />
                    에이전트가 웹 파일을 만들면 여기서 바로 확인·플레이할 수 있어요.
                  </div>
                )}
              </div>
            </div>
          )}
        </aside>
      )}
      </div>
    </div>
  )
}

export default AgentView
