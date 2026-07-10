import { useEffect, useRef, useState } from 'react'
import type { AppSettings } from '../../../shared/settings'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import { modelInstalled, pullModel, type PullEvent } from '../lib/ollama'

const OLLAMA_DOWNLOAD = 'https://ollama.com/download'

interface Props {
  settings: AppSettings
  backend: BackendInfo
  health: HealthInfo | null
}

type StepState = 'done' | 'todo' | 'pending'

/** 처음 설치 온보딩 — Ollama·채팅 모델·임베딩 모델이 준비됐는지 감지해 설치를 유도한다.
 *  임베딩 모델(RAG 필수)은 앱에서 원클릭 다운로드, 채팅 모델은 명령어 안내만, Ollama는 링크 안내.
 *  모든 준비가 끝나면 아무것도 렌더하지 않는다. */
function SetupCard({ settings, backend, health }: Props): React.JSX.Element | null {
  // 파생값 먼저(널 안전) — 아래 훅들이 참조하고, 훅 뒤에서 조기 반환한다.
  const ollamaOk = health?.ollama === true
  const chatModel = settings.model
  const embedModel = settings.embeddingModel
  const ragOn = settings.ragEnabled
  const models = health?.models ?? []
  const chatInstalled = ollamaOk && modelInstalled(models, chatModel)
  const embedInstalled = ollamaOk && modelInstalled(models, embedModel)

  const [pulling, setPulling] = useState(false)
  const [pct, setPct] = useState<number | null>(null)
  const [pullStatus, setPullStatus] = useState('')
  const [pullErr, setPullErr] = useState<string | null>(null)
  const [justInstalled, setJustInstalled] = useState(false)
  const [copied, setCopied] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => () => abortRef.current?.abort(), [])
  // 임베딩 대상 모델이 바뀌면 낙관 상태·진행 표시 초기화 — 다른(미설치) 모델로 바꿨을 때 '설치'가 다시 뜨도록
  useEffect(() => {
    setJustInstalled(false)
    setPct(null)
    setPullErr(null)
    setPullStatus('')
  }, [embedModel])
  // 실제 설치가 확인되면 낙관 래치 해제 — 이후 모델이 지워지면 다시 '설치'가 뜨도록(스테일 마스킹 방지)
  useEffect(() => {
    if (embedInstalled) setJustInstalled(false)
  }, [embedInstalled])

  if (!health) return null
  const allReady = ollamaOk && chatInstalled && (!ragOn || embedInstalled)
  if (allReady) return null

  const ollamaState: StepState = ollamaOk ? 'done' : 'todo'
  const chatState: StepState = !ollamaOk ? 'pending' : chatInstalled ? 'done' : 'todo'
  const embedDone = embedInstalled || justInstalled
  const embedState: StepState = !ollamaOk ? 'pending' : embedDone ? 'done' : 'todo'

  const chatCmd = `ollama pull ${chatModel}`
  const copyCmd = async (cmd: string): Promise<void> => {
    try {
      await navigator.clipboard.writeText(cmd)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      /* 클립보드 불가 — 무시 */
    }
  }

  const startPull = async (): Promise<void> => {
    if (backend.port == null || pulling) return
    setPulling(true)
    setPullErr(null)
    setPct(null)
    setPullStatus('준비 중…')
    const ac = new AbortController()
    abortRef.current = ac
    try {
      await pullModel(
        backend.port,
        embedModel,
        settings.ollamaHost,
        (e: PullEvent) => {
          if (e.type === 'progress') {
            setPullStatus(e.status || '다운로드 중…')
            if (typeof e.total === 'number' && e.total > 0 && typeof e.completed === 'number') {
              setPct(Math.min(100, Math.round((e.completed / e.total) * 100)))
            }
          } else if (e.type === 'done') {
            setPct(100)
            setPullStatus('완료')
            setJustInstalled(true)
          } else if (e.type === 'error') {
            setPullErr(e.error)
          }
        },
        ac.signal
      )
    } catch (err) {
      if ((err as Error).name !== 'AbortError') setPullErr((err as Error).message)
    } finally {
      setPulling(false)
      abortRef.current = null
    }
  }

  return (
    <section className="setup">
      <div className="setup__head">
        <span className="setup__title">시작 준비</span>
        <span className="setup__sub">Ollama와 모델이 준비되면 바로 사용할 수 있어요</span>
      </div>
      <ol className="setup__steps">
        <li className={`setup-step setup-step--${ollamaState}`}>
          <span className="setup-step__mark">{ollamaState === 'done' ? '✓' : '1'}</span>
          <div className="setup-step__body">
            <div className="setup-step__label">Ollama 실행</div>
            {ollamaState === 'done' ? (
              <div className="setup-step__hint">로컬 Ollama에 연결됨</div>
            ) : (
              <>
                <div className="setup-step__hint">
                  Ollama가 실행되고 있지 않습니다. 설치 후 실행하면 자동으로 연결됩니다.
                </div>
                <a className="setup-btn" href={OLLAMA_DOWNLOAD} target="_blank" rel="noreferrer">
                  Ollama 다운로드 ↗
                </a>
              </>
            )}
          </div>
        </li>

        <li className={`setup-step setup-step--${chatState}`}>
          <span className="setup-step__mark">{chatState === 'done' ? '✓' : '2'}</span>
          <div className="setup-step__body">
            <div className="setup-step__label">
              채팅 모델 <span className="mono setup-step__model">{chatModel}</span>
            </div>
            {chatState === 'done' ? (
              <div className="setup-step__hint">설치됨</div>
            ) : chatState === 'pending' ? (
              <div className="setup-step__hint">Ollama 실행 후 확인됩니다</div>
            ) : (
              <>
                <div className="setup-step__hint">
                  원하는 모델은 설정 탭에서 고를 수 있어요. 터미널에서 아래 명령으로 받습니다.
                </div>
                <div className="setup-cmd">
                  <code className="mono">{chatCmd}</code>
                  <button className="setup-btn setup-btn--ghost" onClick={() => void copyCmd(chatCmd)}>
                    {copied ? '복사됨' : '복사'}
                  </button>
                </div>
              </>
            )}
          </div>
        </li>

        {ragOn && (
          <li className={`setup-step setup-step--${embedState}`}>
            <span className="setup-step__mark">{embedState === 'done' ? '✓' : '3'}</span>
            <div className="setup-step__body">
              <div className="setup-step__label">
                임베딩 모델 <span className="mono setup-step__model">{embedModel}</span>
                <span className="setup-step__tag">RAG</span>
              </div>
              {embedState === 'done' ? (
                <div className="setup-step__hint">
                  {embedInstalled ? '설치됨 — RAG 검색 사용 가능' : '설치 완료 — 반영 중…'}
                </div>
              ) : embedState === 'pending' ? (
                <div className="setup-step__hint">Ollama 실행 후 설치할 수 있어요</div>
              ) : pulling ? (
                <div className="setup-pull">
                  <div className="setup-pull__bar">
                    <span style={{ width: `${pct ?? 0}%` }} />
                  </div>
                  <div className="setup-pull__meta mono">
                    {pullStatus}
                    {pct != null ? ` · ${pct}%` : ''}
                  </div>
                </div>
              ) : (
                <>
                  <div className="setup-step__hint">
                    RAG(의미 검색)에 필요한 모델입니다. 앱에서 바로 받을 수 있어요.
                  </div>
                  {pullErr && <div className="setup-step__err">{pullErr}</div>}
                  <button className="setup-btn" onClick={() => void startPull()}>
                    설치 (약 1.2GB)
                  </button>
                </>
              )}
            </div>
          </li>
        )}
      </ol>
    </section>
  )
}

export default SetupCard
