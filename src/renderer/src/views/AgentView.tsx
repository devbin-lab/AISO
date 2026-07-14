import { useEffect, useRef, useState } from 'react'
import type { AppSettings, ReasoningEffort } from '../../../shared/settings'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import type { AgentEvent, ApprovalMode, PlanStep } from '../../../shared/agent'
import type { ConversationMeta } from '../../../shared/conversation'
import { TOOL_LABEL, APPROVAL_MODES } from '../../../shared/agent'
import { streamAgent, approveAgent, type AgentMessage } from '../lib/agent'
import { newConversationId, titleFromText } from '../lib/conversations'
import { modelInstalled } from '../lib/ollama'
import ConversationList from '../components/ConversationList'
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

type ToolStatus = 'running' | 'awaiting' | 'done' | 'error' | 'rejected'

type Item =
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string; thinking: string; streaming: boolean }
  | {
      kind: 'tool'
      callId: string
      name: string
      args: Record<string, unknown>
      status: ToolStatus
      output?: string
      screenshot?: string
    }
  | { kind: 'meta'; tokens: number; seconds: number } // 실행 완료 후 토큰·소요시간 요약줄

interface Props {
  settings: AppSettings
  backend: BackendInfo
  health: HealthInfo | null
  onPickWorkspace: () => Promise<void>
  onSaveSettings: (patch: Partial<AppSettings>) => Promise<void>
  convCollapsed: boolean
}

function argPath(args: Record<string, unknown>): string {
  // 툴마다 주요 인자 키가 다르다 (path / command / pattern / query / src) — 카드·승인바에 표시할 값을 고른다
  for (const k of ['path', 'command', 'pattern', 'query', 'src', 'url']) {
    if (typeof args[k] === 'string' && args[k]) return args[k] as string
  }
  return ''
}

function AgentView({
  settings,
  backend,
  health,
  onPickWorkspace,
  onSaveSettings,
  convCollapsed
}: Props): React.JSX.Element {
  const [items, setItems] = useState<Item[]>([])
  const [input, setInput] = useState('')
  const [running, setRunning] = useState(false)
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>('read')
  const [note, setNote] = useState<string | null>(null)
  const [plan, setPlan] = useState<PlanStep[]>([])
  const [sidebarOpen, setSidebarOpen] = useState(false) // 우측 사이드바(계획+미리보기) 전체 토글
  const [showPreview, setShowPreview] = useState(true) // 사이드바 안 미리보기 하위 패널만
  const [previewPath, setPreviewPath] = useState<string | null>(null)
  const [previewReload, setPreviewReload] = useState(0)
  const [rag, setRag] = useState<RagStatus | null>(null)
  const [indexing, setIndexing] = useState(false)
  const [indexProg, setIndexProg] = useState<{ done: number; total: number } | null>(null)
  const [ragNote, setRagNote] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0) // 현재 실행 경과(초) — '멈춘 것처럼 보임' 방지용 활동 표시
  const [liveTokens, setLiveTokens] = useState(0) // 이번 실행 누적 토큰(실시간 표시)
  const runStartRef = useRef(0)
  const runTokensRef = useRef(0) // 완료 시 사용량 기록용(마지막 usage.total)

  // 작업 폴더가 정해지면 사이드카에 미리보기 루트를 알려준다 (/f 정적 서빙용)
  useEffect(() => {
    if (backend.state === 'ready' && backend.port && settings.workspace) {
      fetch(`http://127.0.0.1:${backend.port}/preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ workspace: settings.workspace })
      }).catch(() => {})
    }
  }, [backend.state, backend.port, settings.workspace])

  // RAG 색인 상태 로드 (작업 폴더·백엔드 준비 시)
  useEffect(() => {
    if (backend.state === 'ready' && backend.port && settings.workspace.trim()) {
      ragStatus(backend.port, settings.workspace).then(setRag).catch(() => setRag(null))
    } else {
      setRag(null)
    }
  }, [backend.state, backend.port, settings.workspace])

  const previewUrl =
    backend.port && previewPath
      ? `http://127.0.0.1:${backend.port}/f/${previewPath.split('/').map(encodeURIComponent).join('/')}`
      : ''
  const abortRef = useRef<AbortController | null>(null)
  const sessionRef = useRef<string>('')
  const historyRef = useRef<AgentMessage[]>([])
  const finalTextRef = useRef<string>('') // 이번 run의 마지막 assistant 답변 (툴콜 이후 초기화)
  const autoIndexedRef = useRef<string>('') // 자동 색인을 이미 시도한 워크스페이스 (중복 방지)
  const scrollRef = useRef<HTMLDivElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)

  // ── 대화방 (에이전트 작업 세션) ──
  const [convId, setConvId] = useState<string | null>(null)
  const [convTitle, setConvTitle] = useState('새 대화')
  const [convList, setConvList] = useState<ConversationMeta[]>([])
  const convIdRef = useRef<string | null>(null)

  const refreshConvs = (): void => {
    window.api.conversations.list('agent').then(setConvList).catch(() => {})
  }
  useEffect(() => refreshConvs(), [])

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
      .then(refreshConvs)
      .catch(() => {})
  }, [items, plan, running, convTitle])

  const newConv = (): void => {
    if (running) return
    convIdRef.current = null
    setConvId(null)
    setConvTitle('새 대화')
    setItems([])
    setPlan([])
    historyRef.current = []
    setNote(null)
    // 새 대화는 작업 폴더 미선택 상태로 시작 — 작업 폴더는 대화(작업 세션)마다 따로 고른다
    if (settings.workspace) void onSaveSettings({ workspace: '' })
  }
  const selectConv = async (id: string): Promise<void> => {
    if (running || id === convId) return
    const c = await window.api.conversations.get(id)
    if (!c) return
    const d = (c.data ?? {}) as { items?: Item[]; history?: AgentMessage[]; plan?: PlanStep[]; workspace?: string }
    convIdRef.current = id
    setConvId(id)
    setConvTitle(c.title)
    setItems(d.items ?? [])
    setPlan(d.plan ?? [])
    historyRef.current = d.history ?? []
    setNote(null)
    // 작업 세션마다 작업 폴더가 다를 수 있으니 저장된 폴더로 복원
    if (d.workspace && d.workspace !== settings.workspace) void onSaveSettings({ workspace: d.workspace })
  }
  const renameConv = async (id: string, title: string): Promise<void> => {
    const c = await window.api.conversations.get(id)
    if (!c) return
    await window.api.conversations.save({ id, kind: 'agent', title, data: c.data })
    if (convIdRef.current === id) setConvTitle(title)
    refreshConvs()
  }
  const pinConv = (id: string, pinned: boolean): void => {
    window.api.conversations.setPinned(id, pinned).then(refreshConvs).catch(() => {})
  }
  const deleteConv = async (id: string): Promise<void> => {
    await window.api.conversations.remove(id)
    if (convIdRef.current === id) newConv()
    refreshConvs()
  }

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
  const ready = backendReady && hasWorkspace && ollamaOk
  const canSend = ready && !running && input.trim().length > 0

  // ---- 이벤트 → 타임라인 리듀서 (순수: 기존 객체를 변형하지 않는다) ----
  const reduce = (ev: AgentEvent): void => {
    // 마지막 assistant 답변 추적 (setItems 밖에서 1회만 실행 → StrictMode 이중호출 영향 없음)
    if (ev.type === 'content') finalTextRef.current += ev.text
    else if (ev.type === 'tool_call') {
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
    } else if (ev.type === 'notice') {
      setNote(ev.text)
      return
    } else if (ev.type === 'plan') {
      setPlan(ev.steps)
      return
    } else if (ev.type === 'rag_reindex') {
      // 에이전트가 파일을 바꾼 뒤 색인 자동 최신화 — 칩을 실시간 반영
      if (ev.state === 'start') setIndexing(true)
      else {
        setIndexing(false)
        if (typeof ev.count === 'number') {
          setRag((prev) => ({
            indexed: ev.count! > 0,
            count: ev.count!,
            files: ev.files,
            embed_model: prev?.embed_model,
            dim: prev?.dim
          }))
        }
      }
      return
    } else if (ev.type === 'screenshot') {
      setPreviewReload((n) => n + 1) // 파일이 방금 실행됨 → 미리보기 새로고침
    }

    if (ev.type === 'error') {
      setItems((prev) => [
        ...prev.map((i) => (i.kind === 'assistant' ? { ...i, streaming: false } : i)),
        { kind: 'assistant', text: `⚠ ${ev.error}`, thinking: '', streaming: false }
      ])
      return
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
          next.push({ kind: 'tool', callId: ev.id, name: ev.name, args: ev.args, status: 'running' })
        }
      } else if (ev.type === 'approval_request') {
        return next.map((i) =>
          i.kind === 'tool' && i.callId === ev.id ? { ...i, status: 'awaiting' as const } : i
        )
      } else if (ev.type === 'tool_result') {
        return next.map((i) =>
          i.kind === 'tool' && i.callId === ev.id
            ? {
                ...i,
                status: ev.ok ? ('done' as const) : ev.rejected ? ('rejected' as const) : ('error' as const),
                output: ev.output
              }
            : i
        )
      } else if (ev.type === 'screenshot') {
        return next.map((i) =>
          i.kind === 'tool' && i.callId === ev.id ? { ...i, screenshot: ev.data } : i
        )
      } else if (ev.type === 'done') {
        if (openAsst) next[li] = { ...openAsst, streaming: false }
      }
      return next
    })
  }

  const decide = async (callId: string, approved: boolean): Promise<void> => {
    if (backend.port == null) return
    setItems((prev) =>
      prev.map((i) =>
        i.kind === 'tool' && i.callId === callId ? { ...i, status: approved ? 'running' : 'rejected' } : i
      )
    )
    try {
      await approveAgent(backend.port, sessionRef.current, callId, approved)
    } catch (err) {
      console.error('승인 전송 실패:', err)
    }
  }

  const send = async (): Promise<void> => {
    const text = input.trim()
    if (!text || !ready || running) return
    // 새 세션의 첫 지시 → 대화 id·제목 부여 (이후 저장 이펙트가 영속화)
    if (!convIdRef.current) {
      const id = newConversationId()
      convIdRef.current = id
      setConvId(id)
      setConvTitle(titleFromText(text))
    }
    setInput('')
    if (taRef.current) taRef.current.style.height = 'auto'

    historyRef.current = [...historyRef.current, { role: 'user', content: text }]
    finalTextRef.current = ''
    setNote(null)
    setPlan([])
    setItems((prev) => [...prev, { kind: 'user', text }])
    setRunning(true)
    setLiveTokens(0)
    runTokensRef.current = 0
    const ac = new AbortController()
    abortRef.current = ac
    sessionRef.current =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : String(Math.random())

    try {
      await streamAgent(
        backend.port!,
        settings,
        settings.workspace,
        historyRef.current,
        sessionRef.current,
        approvalMode,
        reduce,
        ac.signal
      )
    } catch (err) {
      if ((err as Error).name !== 'AbortError') reduce({ type: 'error', error: (err as Error).message })
    } finally {
      setRunning(false)
      abortRef.current = null
      // 이번 실행에서 쓴 토큰을 사용량 통계에 기록 (홈의 일/주/월 집계)
      if (runTokensRef.current > 0) {
        void window.api.usage.record(runTokensRef.current)
        // 이번 답변 아래에 토큰·소요시간 요약줄을 남긴다
        const secs = (Date.now() - runStartRef.current) / 1000
        setItems((prev) => [...prev, { kind: 'meta', tokens: runTokensRef.current, seconds: secs }])
      }
      // 이번 run의 마지막 assistant 답변을 대화 히스토리에 반영 (ref 기반 → 중복 없음)
      if (finalTextRef.current.trim()) {
        historyRef.current = [...historyRef.current, { role: 'assistant', content: finalTextRef.current }]
      }
      // 재색인은 백엔드에서 백그라운드로 돌므로, 잠시 뒤 색인 칩 수치를 갱신한다
      if (backend.port && settings.workspace.trim()) {
        const port = backend.port
        window.setTimeout(() => {
          ragStatus(port, settings.workspace).then(setRag).catch(() => {})
        }, 1500)
      }
    }
  }

  const stop = (): void => {
    abortRef.current?.abort()
    // 승인 대기 중인 툴은 거부 처리 → 백엔드 홀딩 해제
    items.forEach((i) => {
      if (i.kind === 'tool' && i.status === 'awaiting') void decide(i.callId, false)
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
              : e.truncated
                ? `폴더가 커서 일부만 RAG에 넣었습니다 (발견 ${e.total_found ?? e.files}개 중 ${e.files}개 파일). 더 넣으려면 설정 → RAG의 '색인 범위(최대 파일 수)'를 늘리세요.`
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
    if (!settings.ragEnabled || !backendReady || !hasWorkspace || indexing) return
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
    backendReady,
    hasWorkspace,
    indexing,
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

  const modelOptions: DropdownOption[] =
    health?.models && health.models.length
      ? health.models.map((m) => ({ value: m, label: m }))
      : settings.model
        ? [{ value: settings.model, label: settings.model }]
        : []

  let notice: { text: string; kind: 'err' | 'warn' } | null = null
  if (backend.state !== 'ready') notice = { text: '백엔드 엔진 준비 중…', kind: 'warn' }
  else if (!ollamaOk) notice = { text: 'Ollama에 연결할 수 없습니다 — Ollama 앱을 실행하세요', kind: 'err' }
  else if (!hasWorkspace) notice = { text: '작업 폴더를 먼저 선택하세요 — 모든 파일 작업은 이 폴더 안으로 제한됩니다', kind: 'warn' }

  return (
    <div className="convshell">
      {!convCollapsed && (
        <ConversationList
          items={convList}
          activeId={convId}
          onSelect={selectConv}
          onNew={newConv}
          onRename={renameConv}
          onPin={pinConv}
          onDelete={deleteConv}
        />
      )}
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
            if (it.kind === 'user') return <div key={i} className="msg msg--user"><div className="msg__bubble">{it.text}</div></div>
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
          <button className="btn btn--sm" onClick={() => void decide(awaiting.callId, true)}>
            승인
          </button>
          <button className="btn btn--sm btn--ghost2" onClick={() => void decide(awaiting.callId, false)}>
            거부
          </button>
        </div>
      )}

      {ragNote && !awaiting && <div className="notice notice--warn">{ragNote}</div>}
      {note && !awaiting && <div className="notice notice--warn">{note}</div>}
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

      {/* 입력창 위: 작업 폴더 + RAG (왼쪽 정렬) */}
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

      <div className="composer">
        <textarea
          ref={taRef}
          className="input composer__ta"
          rows={1}
          value={input}
          disabled={!ready || running}
          placeholder={ready ? '무엇을 할까요? · Enter 전송' : '작업 폴더·엔진 준비 후 사용 가능'}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          onInput={autoGrow}
        />
        {running ? (
          <button className="btn btn--stop" onClick={stop}>중지</button>
        ) : (
          <button className="btn" disabled={!canSend} onClick={() => void send()}>실행</button>
        )}
      </div>

      <div className="composer-tools">
        <Dropdown
          value={approvalMode}
          options={APPROVAL_OPTIONS}
          onChange={(v) => setApprovalMode(v as ApprovalMode)}
          align="left"
          title="승인 모드 — 수동(전부 승인) · 읽기(쓰기·삭제만 승인) · 자동(승인 없음)"
        />

        <div className="composer-tools__right">
          <Dropdown
            value={settings.model}
            options={modelOptions}
            onChange={(v) => void onSaveSettings({ model: v })}
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
              className="iconbtn"
              data-tip="새 대화 시작"
              aria-label="새 대화 시작"
              onClick={newConv}
              disabled={running}
            >
              <TrashIcon />
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
                  // 최상위 네비게이션은 차단 — CSP가 못 막는 window.open 외부 유출 경로를 닫는다.
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
