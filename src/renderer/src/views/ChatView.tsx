import { useEffect, useRef, useState } from 'react'
import {
  type AppSettings,
  type ReasoningEffort,
  type TempPreset,
  TEMP_PRESET_META
} from '../../../shared/settings'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import { streamChat, type ChatPayloadMessage } from '../lib/chat'
import { ChatIcon } from '../components/icons'
import Dropdown, { type DropdownOption } from '../components/Dropdown'

const EFFORT_OPTIONS: DropdownOption[] = [
  { value: 'low', label: '낮음', hint: '빠름' },
  { value: 'medium', label: '중간', hint: '균형' },
  { value: 'high', label: '높음', hint: '정확' }
]
const TEMP_OPTIONS: DropdownOption[] = TEMP_PRESET_META.map((p) => ({
  value: p.id,
  label: p.label,
  hint: p.hint
}))

interface ChatMsg {
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  error?: string
  streaming?: boolean
}

interface Props {
  settings: AppSettings
  backend: BackendInfo
  health: HealthInfo | null
  onSaveSettings: (patch: Partial<AppSettings>) => Promise<void>
}

function ChatView({ settings, backend, health, onSaveSettings }: Props): React.JSX.Element {
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)

  // 새 청크마다 하단으로 스크롤
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  const backendReady = backend.state === 'ready' && backend.port != null
  const ollamaOk = health?.ollama === true
  const modelInstalled = !health || health.models.includes(settings.model)
  const canSend = backendReady && ollamaOk && !streaming && input.trim().length > 0

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
    setInput('')
    if (taRef.current) taRef.current.style.height = 'auto'

    const history: ChatPayloadMessage[] = [
      ...messages
        .filter((m) => !m.error)
        .map((m) => ({ role: m.role, content: m.content })),
      { role: 'user' as const, content: text }
    ]
    setMessages((prev) => [
      ...prev,
      { role: 'user', content: text },
      { role: 'assistant', content: '', streaming: true }
    ])
    setStreaming(true)
    setNote(null)
    const ac = new AbortController()
    abortRef.current = ac

    try {
      await streamChat(backend.port!, settings, history, (c) => {
        if (c.type === 'thinking' && c.text) {
          updateLast((m) => ({ ...m, thinking: (m.thinking ?? '') + c.text }))
        } else if (c.type === 'content' && c.text) {
          updateLast((m) => ({ ...m, content: m.content + c.text }))
        } else if (c.type === 'notice' && c.text) {
          setNote(c.text)
        } else if (c.type === 'error') {
          updateLast((m) => ({ ...m, error: c.error ?? '알 수 없는 오류', streaming: false }))
        } else if (c.type === 'done') {
          updateLast((m) => ({ ...m, streaming: false }))
        }
      }, ac.signal)
      updateLast((m) => (m.streaming ? { ...m, streaming: false } : m))
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        updateLast((m) => ({ ...m, streaming: false }))
      } else {
        updateLast((m) => ({ ...m, error: (err as Error).message, streaming: false }))
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
    health?.models && health.models.length
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
  } else if (backendReady && health && !health.ollama) {
    notice = { text: 'Ollama에 연결할 수 없습니다 — Ollama 앱을 실행하세요', kind: 'err' }
  } else if (backendReady && ollamaOk && !modelInstalled) {
    notice = { text: `모델 '${settings.model}'이 설치되어 있지 않습니다 — 터미널에서: ollama pull ${settings.model}`, kind: 'warn' }
  }

  return (
    <div className="view view--chat">
      <header className="view__head">
        <h1>채팅</h1>
        <p className="view__desc">로컬 모델과의 대화</p>
      </header>

      <div className="chat-scroll" ref={scrollRef}>
        {messages.length === 0 ? (
          <div className="empty empty--borderless">
            <div className="empty__icon">
              <ChatIcon size={18} />
            </div>
            <div className="empty__title">무엇이든 물어보세요</div>
            <div className="empty__desc">
              모든 대화는 로컬에서 처리됩니다 · 사고 과정은 접힌 상태로 표시됩니다
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
                  {m.content && <div className="msg__body">{m.content}</div>}
                  {m.streaming && !m.content && !m.thinking && (
                    <div className="msg__body msg__body--pending">응답 대기 중…</div>
                  )}
                  {m.streaming && (m.content || m.thinking) && <span className="caret" />}
                  {m.error && <div className="msg__error">{m.error}</div>}
                </>
              ) : (
                <div className="msg__bubble">{m.content}</div>
              )}
            </div>
          ))
        )}
      </div>

      {note && <div className="notice notice--warn">{note}</div>}
      {notice && <div className={`notice notice--${notice.kind}`}>{notice.text}</div>}

      <div className="composer">
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
          <Dropdown
            value={settings.tempPreset}
            options={TEMP_OPTIONS}
            onChange={(v) => void onSaveSettings({ tempPreset: v as TempPreset })}
            align="right"
            title="생성 온도 — 작업 유형별 프리셋 (설정에서 값 조정)"
          />
        </div>
      </div>
    </div>
  )
}

export default ChatView
