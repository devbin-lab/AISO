import { useEffect, useRef, useState } from 'react'
import type { AppSettings, ReasoningEffort } from '../../../shared/settings'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import { streamChat, type ChatPayloadMessage } from '../lib/chat'
import { newConversationId, titleFromText } from '../lib/conversations'
import { modelInstalled } from '../lib/ollama'
import { ChatIcon, SearchIcon, GlobeIcon } from '../components/icons'
import { TOOL_LABEL } from '../../../shared/agent'
import Markdown from '../components/Markdown'
import type { ConversationRequest } from '../components/Sidebar'
import Dropdown, { type DropdownOption } from '../components/Dropdown'
import AttachmentPicker from '../components/AttachmentPicker'
import type { AttachmentRef } from '../../../shared/attachments'

const EFFORT_OPTIONS: DropdownOption[] = [
  { value: 'low', label: '낮음', hint: '빠름' },
  { value: 'medium', label: '중간', hint: '균형' },
  { value: 'high', label: '높음', hint: '정확' }
]

// 웹 검색(리서치) 모드에서 모델이 부른 조사 도구 1건의 진행 상태
interface ToolAct {
  id: string
  name: string
  arg: string // 검색어(query) 또는 URL — 카드에 표시
  status: 'running' | 'done' | 'error' | 'stopped'
  output?: string
}

// 스트림이 중단(중지/오류)될 때 아직 'running'인 도구 카드를 마무리한다 — 영구 '검색 중' 방지.
const finalizeTools = (tools: ToolAct[] | undefined, to: 'stopped' | 'error'): ToolAct[] | undefined =>
  tools?.map((t) => (t.status === 'running' ? { ...t, status: to } : t))

interface ChatMsg {
  attachments?: AttachmentRef[]
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  error?: string
  incomplete?: boolean
  streaming?: boolean
  tools?: ToolAct[] // 웹 검색 모드의 도구 활동(검색·문서 가져오기)
  tokens?: number // 이 답변에 쓴 토큰(프롬프트+생성)
  seconds?: number // 생성 소요 시간(초)
}

// 도구 인자에서 카드에 보여줄 주요 값(검색어/URL)을 고른다
const toolArg = (args?: Record<string, unknown>): string => {
  for (const k of ['query', 'url']) {
    if (args && typeof args[k] === 'string' && args[k]) return args[k] as string
  }
  return ''
}

const fmtTokens = (n: number): string => n.toLocaleString('en-US')
const fmtDuration = (s: number): string =>
  s < 60 ? `${s.toFixed(1)}초` : `${Math.floor(s / 60)}분 ${Math.round(s % 60)}초`

interface Props {
  settings: AppSettings
  backend: BackendInfo
  health: HealthInfo | null
  onSaveSettings: (patch: Partial<AppSettings>) => Promise<boolean>
  conversationRequest?: ConversationRequest | null
  onConversationActive?: (id: string | null) => void
}

function ChatView({
  settings,
  backend,
  health,
  onSaveSettings,
  conversationRequest,
  onConversationActive = () => {}
}: Props): React.JSX.Element {
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [input, setInput] = useState('')
  const [attachments, setAttachments] = useState<AttachmentRef[]>([])
  const [streaming, setStreaming] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)

  // ── 대화방 ──
  const [convId, setConvId] = useState<string | null>(null)
  const [convTitle, setConvTitle] = useState('새 대화')
  const convIdRef = useRef<string | null>(null) // send() 클로저에서 최신 id 참조

  // 스트리밍이 끝나면(메시지 확정 시) 활성 대화를 저장 — 목록/제목/시각 갱신
  useEffect(() => {
    const id = convIdRef.current
    if (!id || streaming || messages.length === 0) return
    window.api.conversations
      .save({ id, kind: 'chat', title: convTitle, data: messages })
      .then(() => window.dispatchEvent(new Event('aiso:conversations-changed')))
      .catch(() => {})
  }, [messages, streaming, convTitle])

  const newConv = (announce = true): void => {
    if (streaming) return
    convIdRef.current = null
    setConvId(null)
    setConvTitle('새 대화')
    setMessages([])
    setNote(null)
    if (announce) onConversationActive(null)
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
    if (streaming || id === convId) return
    const c = await window.api.conversations.get(id)
    if (!c) return
    convIdRef.current = id
    setConvId(id)
    setConvTitle(c.title)
    setMessages((c.data as ChatMsg[]) ?? [])
    setNote(null)
    if (announce) onConversationActive(id)
  }
  const consumedRequestRef = useRef(0)
  useEffect(() => {
    if (conversationRequest?.kind !== 'chat' || conversationRequest.nonce === consumedRequestRef.current) return
    consumedRequestRef.current = conversationRequest.nonce
    if (conversationRequest.id) void selectConv(conversationRequest.id, false)
    else newConv(false)
  }, [conversationRequest])

  // 새 청크마다 하단으로 스크롤
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  const backendReady = backend.state === 'ready' && backend.port != null
  const ollamaOk = health?.ollama === true
  const modelReady = !health || modelInstalled(health.models, settings.model)
  const nvidiaSelected = settings.activeLlmProvider === 'nvidia'
  const providerReady = nvidiaSelected
    ? settings.nvidiaModel.trim().length > 0
    : ollamaOk && modelReady
  const canSend = backendReady && providerReady && !streaming && input.trim().length > 0

  const updateLast = (fn: (m: ChatMsg) => ChatMsg): void => {
    setMessages((prev) => {
      if (prev.length === 0) return prev
      const next = prev.slice()
      next[next.length - 1] = fn(next[next.length - 1])
      return next
    })
  }

  const send = async (): Promise<void> => {
    const text = input.trim()
    if (!text || !backendReady || streaming) return
    // 새 대화의 첫 메시지 → 대화 id·제목 부여 (이후 저장 이펙트가 영속화)
    if (!convIdRef.current) {
      const id = newConversationId()
      convIdRef.current = id
      setConvId(id)
      setConvTitle(titleFromText(text))
      onConversationActive(id)
    }
    setInput('')
    if (taRef.current) taRef.current.style.height = 'auto'

    const pendingAttachments = attachments
    const history: ChatPayloadMessage[] = [
      // 오류 메시지, 그리고 내용이 빈 어시스턴트 턴(조기 중지·리셋 후 중단 등)은 요청 히스토리에서 제외
      ...messages
        .filter((m) => !m.error && !m.incomplete && (m.role === 'user' || m.content.trim() !== ''))
        .map((m) => ({ role: m.role, content: m.content, attachments: m.attachments })),
      { role: 'user' as const, content: text, attachments: pendingAttachments }
    ]
    setMessages((prev) => [
      ...prev,
      { role: 'user', content: text, attachments: pendingAttachments },
      { role: 'assistant', content: '', streaming: true }
    ])
    setStreaming(true)
    setAttachments([])
    setNote(null)
    const ac = new AbortController()
    abortRef.current = ac
    const startedAt = Date.now()
    let runTokens = 0 // 웹 검색(멀티턴) 시 usage 이벤트로 누적되는 생성 토큰
    let pendingReset = false // reset_content 예약 — 다음 스트림 토큰이 올 때 원자적으로 교체

    try {
      await streamChat(backend.port!, settings, history, (c) => {
        if (c.type === 'thinking' && c.text) {
          const txt = c.text
          if (pendingReset) {
            pendingReset = false
            setNote(null) // 재작성 답이 시작되면 '교차확인 중…' 진행 문구를 지운다
            updateLast((m) => ({ ...m, content: '', thinking: txt }))
          } else {
            updateLast((m) => ({ ...m, thinking: (m.thinking ?? '') + txt }))
          }
        } else if (c.type === 'content' && c.text) {
          const txt = c.text
          if (pendingReset) {
            pendingReset = false
            setNote(null) // 재작성 답이 시작되면 '교차확인 중…' 진행 문구를 지운다
            updateLast((m) => ({ ...m, content: txt, thinking: '' }))
          } else {
            updateLast((m) => ({ ...m, content: m.content + txt }))
          }
        } else if (c.type === 'reset_content') {
          // 웹 검색 모드: 스니펫만 보고 쓴 임시 답을 원문 검증 후 답으로 '교체 예약'한다.
          // 실제 지우기는 다음 스트림 토큰이 올 때(원자적 교체) — 그 전에 중지/오류가 나도
          // 임시답이 남아, 빈 어시스턴트 턴이 저장·재전송되는 일이 없다.
          pendingReset = true
        } else if (c.type === 'notice' && c.text) {
          setNote(c.text)
        } else if (c.type === 'tool_call') {
          // 웹 검색 모드: 모델이 검색/문서 가져오기를 시작 → 카드 추가
          const act: ToolAct = {
            id: c.id ?? '',
            name: c.name ?? '',
            arg: toolArg(c.args),
            status: 'running'
          }
          updateLast((m) => ({ ...m, tools: [...(m.tools ?? []), act] }))
        } else if (c.type === 'tool_result') {
          updateLast((m) => ({
            ...m,
            tools: (m.tools ?? []).map((t) =>
              t.id === c.id ? { ...t, status: c.ok ? 'done' : 'error', output: c.output } : t
            )
          }))
        } else if (c.type === 'usage') {
          runTokens = c.total ?? runTokens
          updateLast((m) => ({ ...m, tokens: runTokens }))
        } else if (c.type === 'error') {
          updateLast((m) => ({ ...m, error: c.error ?? '알 수 없는 오류', incomplete: true, streaming: false }))
        } else if (c.type === 'incomplete' || c.type === 'cancelled') {
          updateLast((m) => ({
            ...m,
            error: c.error ?? '응답이 불완전하게 종료되어 다음 요청에 포함하지 않습니다.',
            incomplete: true,
            streaming: false
          }))
        } else if (c.type === 'done') {
          // 순수 채팅은 done에 eval_count, 웹 검색은 usage로 누적 → 둘 중 큰 값을 쓴다
          const used = Math.max(c.eval_count ?? 0, runTokens)
          const secs = (Date.now() - startedAt) / 1000
          updateLast((m) => ({
            ...m,
            streaming: false,
            tokens: used > 0 ? used : undefined,
            seconds: secs
          }))
          if (used > 0) void window.api.usage.record(used)
        }
      }, ac.signal)
      updateLast((m) => (m.streaming ? { ...m, streaming: false } : m))
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        updateLast((m) => ({
          ...m,
          error: '응답을 중지했습니다. 부분 응답은 다음 요청에 포함하지 않습니다.',
          incomplete: true,
          streaming: false,
          tools: finalizeTools(m.tools, 'stopped')
        }))
      } else {
        updateLast((m) => ({
          ...m,
          error: (err as Error).message,
          incomplete: true,
          streaming: false,
          tools: finalizeTools(m.tools, 'error')
        }))
      }
    } finally {
      setStreaming(false)
      abortRef.current = null
    }
  }

  const stop = (): void => {
    abortRef.current?.abort()
  }

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

  const modelOptions: DropdownOption[] =
    nvidiaSelected
      ? settings.nvidiaModel
        ? [{ value: settings.nvidiaModel, label: settings.nvidiaModel }]
        : []
      : health?.models && health.models.length
      ? health.models.map((m) => ({ value: m, label: m }))
      : settings.model
        ? [{ value: settings.model, label: settings.model }]
        : []

  // 상태 안내 배너
  let notice: { text: string; kind: 'err' | 'warn' } | null = null
  if (backend.state === 'error') {
    notice = { text: `백엔드 엔진 오류 — Python 환경(python/.venv)을 확인하세요${backend.detail ? ` · ${backend.detail}` : ''}`, kind: 'err' }
  } else if (backend.state === 'starting') {
    notice = { text: '백엔드 엔진 시작 중…', kind: 'warn' }
  } else if (nvidiaSelected && !settings.nvidiaModel.trim()) {
    notice = { text: '설정에서 NVIDIA 모델명을 입력하세요.', kind: 'err' }
  } else if (!nvidiaSelected && backendReady && health && !health.ollama) {
    notice = { text: 'Ollama에 연결할 수 없습니다 — Ollama 앱을 실행하세요', kind: 'err' }
  } else if (!nvidiaSelected && backendReady && ollamaOk && !modelReady) {
    notice = { text: `모델 '${settings.model}'이 설치되어 있지 않습니다 — 터미널에서: ollama pull ${settings.model}`, kind: 'warn' }
  }

  return (
    <div className="convshell">
      <div className="view view--chat">
        <header className="view__head">
          <h1>채팅</h1>
          <p className="view__desc">{nvidiaSelected ? 'NVIDIA 일반 채팅' : '로컬 모델과의 대화'}</p>
        </header>

      <div className="chat-scroll" ref={scrollRef}>
        {messages.length === 0 ? (
          <div className="empty empty--borderless">
            <div className="empty__icon">
              <ChatIcon size={18} />
            </div>
            <div className="empty__title">무엇이든 물어보세요</div>
            <div className="empty__desc">
              {nvidiaSelected
                ? settings.chatWebSearch
                  ? '대화와 조사 도구 결과가 선택한 NVIDIA 서비스로 전송됩니다'
                  : '프롬프트와 대화 문맥이 선택한 NVIDIA 서비스로 전송됩니다'
                : settings.chatWebSearch
                ? '필요할 때 자동으로 인터넷을 조사해 답합니다 · 검색이 불필요하면 로컬에서 처리 (설정에서 끌 수 있어요)'
                : '모든 대화는 로컬에서 처리됩니다 · 사고 과정은 접힌 상태로 표시됩니다'}
            </div>
          </div>
        ) : (
          messages.map((m, i) => (
            <div key={i} className={`msg msg--${m.role}`}>
              {m.role === 'assistant' ? (
                <>
                  {m.thinking && (
                    <details className="think">
                      <summary>사고 과정</summary>
                      <div className="think__body">{m.thinking}</div>
                    </details>
                  )}
                  {m.tools && m.tools.length > 0 && (
                    <div className="chat-tools">
                      {m.tools.map((t) => (
                        <div key={t.id} className={`tool tool--${t.status}`}>
                          <div className="tool__head">
                            {t.name === 'web_search' ? <SearchIcon /> : <GlobeIcon />}
                            <span className="tool__name">{TOOL_LABEL[t.name] ?? t.name}</span>
                            {t.arg && <span className="tool__path mono">{t.arg}</span>}
                            <span className={`tool__badge tool__badge--${t.status}`}>
                              {t.status === 'running'
                                ? t.name === 'web_search'
                                  ? '검색 중'
                                  : '읽는 중'
                                : t.status === 'done'
                                  ? '완료'
                                  : t.status === 'stopped'
                                    ? '중지됨'
                                    : '오류'}
                            </span>
                          </div>
                          {t.status !== 'running' && t.output && (
                            <details className="tool__more">
                              <summary>{t.status === 'error' ? '오류 상세' : '결과 보기'}</summary>
                              <pre className="tool__output mono">{t.output.slice(0, 2000)}</pre>
                            </details>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                  {m.content && <Markdown text={m.content} />}
                  {m.streaming && !m.content && !m.thinking && !m.tools?.length && (
                    <div className="msg__body msg__body--pending">응답 대기 중…</div>
                  )}
                  {m.streaming && (m.content || m.thinking) && <span className="caret" />}
                  {m.error && <div className="msg__error">{m.error}</div>}
                  {!m.streaming && m.seconds != null && (m.content || m.tokens) && (
                    <div className="run-meta mono">
                      {m.tokens != null && `${fmtTokens(m.tokens)} 토큰 · `}
                      {fmtDuration(m.seconds)}
                    </div>
                  )}
                </>
              ) : (
                <div className="msg__bubble">
                  {m.content}
                  {m.attachments && m.attachments.length > 0 && (
                    <div className="msg__attachments">첨부: {m.attachments.map((item) => item.name).join(', ')}</div>
                  )}
                </div>
              )}
            </div>
          ))
        )}
      </div>

      {note && <div className="notice notice--warn">{note}</div>}
      {notice && <div className={`notice notice--${notice.kind}`}>{notice.text}</div>}

      <div className="composer">
        <AttachmentPicker value={attachments} onChange={setAttachments} disabled={!backendReady || streaming} />
        <textarea
          ref={taRef}
          className="input composer__ta"
          rows={1}
          value={input}
          disabled={!backendReady || streaming}
          placeholder={
            backendReady
              ? '메시지 입력 · Enter 전송 · Shift+Enter 줄바꿈'
              : '백엔드 연결 대기 중…'
          }
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          onInput={autoGrow}
        />
        {streaming ? (
          <button className="btn btn--stop" onClick={stop}>
            중지
          </button>
        ) : (
          <button className="btn" disabled={!canSend} onClick={() => void send()}>
            전송
          </button>
        )}
      </div>

      <div className="composer-tools">
        <div className="composer-tools__right">
          <Dropdown
            value={nvidiaSelected ? settings.nvidiaModel : settings.model}
            options={modelOptions}
            onChange={(v) => void onSaveSettings(nvidiaSelected ? { nvidiaModel: v } : { model: v })}
            mono
            align="right"
            title="모델"
            placeholder="모델"
          />
          {!nvidiaSelected && <Dropdown
            value={settings.reasoningEffort}
            options={EFFORT_OPTIONS}
            onChange={(v) => void onSaveSettings({ reasoningEffort: v as ReasoningEffort })}
            align="right"
            title="추론 성능"
          />}
        </div>
      </div>
      </div>
    </div>
  )
}

export default ChatView
