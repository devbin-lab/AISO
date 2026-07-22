import { useEffect, useRef, useState } from 'react'
import type { AppSettings } from '../../../shared/settings'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import { modelInstalled, pullModel, type PullEvent } from '../lib/ollama'
import Select from './Select'

const OLLAMA_DOWNLOAD = 'https://ollama.com/download'

// 추천 채팅 모델 — Ollama 레지스트리 검색 API가 로컬에 없어 큐레이션 목록으로 제공.
const RECOMMENDED_MODELS: { tag: string; label: string; hint: string }[] = [
  { tag: 'gemma4:12b', label: 'Gemma4 12B', hint: '기본 추천 · 균형' },
  { tag: 'gpt-oss:20b', label: 'GPT-OSS 20B', hint: '고성능 · VRAM 여유 필요' },
  { tag: 'deepseek-r1:14b', label: 'DeepSeek-R1 14B', hint: '추론 특화' },
  { tag: 'qwen3.5:9b', label: 'Qwen3.5 9B', hint: '다국어 · 경량' },
  { tag: 'exaone3.5:7.8b', label: 'EXAONE 3.5 7.8B', hint: '한국어 특화 · LG' }
]

interface Props {
  settings: AppSettings
  backend: BackendInfo
  health: HealthInfo | null
  onSaveSettings: (patch: Partial<AppSettings>) => Promise<boolean>
}

type StepState = 'done' | 'todo' | 'pending'

/** 같은 모델인지 태그(:latest 등) 무시하고 비교. */
const sameModel = (a: string, b: string): boolean => modelInstalled([a], b)

/** 처음 설치 온보딩 — Ollama·채팅 모델·임베딩 모델 준비를 감지해 설치를 유도한다.
 *  채팅·임베딩 모델 모두 앱에서 원클릭 다운로드(/ollama/pull), Ollama는 링크 안내.
 *  모든 준비가 끝나면 아무것도 렌더하지 않는다. */
function SetupCard({ settings, backend, health, onSaveSettings }: Props): React.JSX.Element | null {
  const preview = settings.forceOnboarding === true // 개발용: 실제 상태와 무관하게 온보딩 강제 표시
  const ollamaOk = health?.ollama === true
  const chatModel = settings.model
  const embedModel = settings.embeddingModel
  const ragOn = settings.ragEnabled
  const models = health?.models ?? []

  const [active, setActive] = useState<string | null>(null) // 지금 내려받는 모델 태그(한 번에 하나)
  const [pct, setPct] = useState<number | null>(null)
  const [status, setStatus] = useState('')
  const [errs, setErrs] = useState<Record<string, string>>({})
  const [done, setDone] = useState<string[]>([]) // 방금 설치 완료(낙관) — health 확인 전까지 표시
  // 추천 모델 드롭다운 선택 — 현재 모델이 추천 목록에 있으면 그걸 기본값으로
  const [picked, setPicked] = useState<string>(
    () => RECOMMENDED_MODELS.find((m) => sameModel(settings.model, m.tag))?.tag ?? RECOMMENDED_MODELS[0].tag
  )
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => () => abortRef.current?.abort(), [])
  // health가 실제 설치를 확인하면 낙관 목록에서 제거(중복 방지) — 확인 전 항목만 낙관 유지
  useEffect(() => {
    setDone((d) => {
      const next = d.filter((t) => !modelInstalled(models, t))
      return next.length === d.length ? d : next
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [health])

  // preview에선 전부 미설치(첫 설치 모습)로 보여준다.
  const isInstalled = (tag: string): boolean =>
    !preview && ollamaOk && (modelInstalled(models, tag) || done.includes(tag))

  const chatInstalled = isInstalled(chatModel)
  const embedInstalled = isInstalled(embedModel)
  const allReady = ollamaOk && chatInstalled && (!ragOn || embedInstalled)

  if (!health && !preview) return null
  if (allReady && !preview) return null

  /** 모델을 내려받는다. 성공하면 true. (한 번에 하나만 — active로 잠금)
   *  preview(온보딩 미리보기)에선 실제 다운로드·모델 변경을 하지 않는다(화면만 보는 용도). */
  const pull = async (model: string): Promise<boolean> => {
    if (preview || active || backend.port == null) return false
    setActive(model)
    setPct(null)
    setStatus('준비 중…')
    setErrs((e) => {
      const n = { ...e }
      delete n[model]
      return n
    })
    const ac = new AbortController()
    abortRef.current = ac
    let ok = false
    try {
      await pullModel(
        backend.port,
        model,
        settings.ollamaHost,
        (e: PullEvent) => {
          if (e.type === 'progress') {
            setStatus(e.status || '다운로드 중…')
            if (typeof e.total === 'number' && e.total > 0 && typeof e.completed === 'number') {
              setPct(Math.min(100, Math.round((e.completed / e.total) * 100)))
            }
          } else if (e.type === 'done') {
            ok = true
            setPct(100)
            setStatus('완료')
            setDone((d) => (d.includes(model) ? d : [...d, model]))
          } else if (e.type === 'error') {
            ok = false
            setErrs((x) => ({ ...x, [model]: e.error }))
          }
        },
        ac.signal
      )
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        setErrs((x) => ({ ...x, [model]: (err as Error).message }))
      }
    } finally {
      setActive(null)
    }
    return ok
  }

  /** 채팅 모델: 설치 후 성공하면 활성 모델로 선택. */
  const installChat = async (model: string): Promise<void> => {
    const ok = await pull(model)
    if (ok) void onSaveSettings({ model })
  }

  // 드롭다운에서 고른 추천 모델의 메타·상태
  const pickedMeta = RECOMMENDED_MODELS.find((m) => m.tag === picked)
  const pickedInstalled = isInstalled(picked)
  const pickedSelected = !preview && sameModel(chatModel, picked)

  const ollamaState: StepState = preview ? 'todo' : ollamaOk ? 'done' : 'todo'
  const chatState: StepState = preview ? 'todo' : !ollamaOk ? 'pending' : chatInstalled ? 'done' : 'todo'
  const embedState: StepState = preview
    ? 'todo'
    : !ollamaOk
      ? 'pending'
      : embedInstalled
        ? 'done'
        : 'todo'

  return (
    <section className="setup">
      <div className="setup__head">
        <span className="setup__title">시작 준비</span>
        {preview && <span className="setup-step__tag">미리보기</span>}
        <span className="setup__sub">Ollama와 모델이 준비되면 바로 사용할 수 있어요</span>
      </div>
      <ol className="setup__steps">
        {/* 1. Ollama */}
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

        {/* 2. 채팅 모델 — 추천 목록에서 원클릭 설치 */}
        <li className={`setup-step setup-step--${chatState}`}>
          <span className="setup-step__mark">{chatState === 'done' ? '✓' : '2'}</span>
          <div className="setup-step__body">
            <div className="setup-step__label">채팅 모델</div>
            {chatState === 'done' ? (
              <div className="setup-step__hint">
                사용 중: <span className="mono">{chatModel}</span>
              </div>
            ) : chatState === 'pending' ? (
              <div className="setup-step__hint">Ollama 실행 후 설치할 수 있어요</div>
            ) : (
              <>
                <div className="setup-step__hint">
                  추천 모델을 골라 받으면 바로 사용할 수 있어요. (다른 모델은 설정에서 선택)
                </div>
                <div className="setup-pick">
                  <Select
                    value={picked}
                    options={RECOMMENDED_MODELS.map((m) => m.tag)}
                    onChange={setPicked}
                    disabled={!!active}
                    status={(tag) => (isInstalled(tag) ? 'on' : 'off')}
                  />
                  <div className="setup-pick__action">
                    {active === picked ? (
                      <div className="setup-pull setup-pull--inline">
                        <div className="setup-pull__bar">
                          <span style={{ width: `${pct ?? 0}%` }} />
                        </div>
                        <div className="setup-pull__meta mono">
                          {status}
                          {pct != null ? ` · ${pct}%` : ''}
                        </div>
                      </div>
                    ) : pickedInstalled ? (
                      pickedSelected ? (
                        <span className="setup-model__ok">✓ 사용 중</span>
                      ) : (
                        <button
                          className="setup-btn setup-btn--ghost"
                          disabled={!!active}
                          onClick={() => void onSaveSettings({ model: picked })}
                        >
                          사용
                        </button>
                      )
                    ) : (
                      <button
                        className="setup-btn"
                        disabled={!!active}
                        onClick={() => void installChat(picked)}
                      >
                        설치
                      </button>
                    )}
                  </div>
                </div>
                {pickedMeta && (
                  <div className="setup-pick__desc">
                    {pickedMeta.label} · {pickedMeta.hint}
                  </div>
                )}
                {errs[picked] && <div className="setup-step__err">{errs[picked]}</div>}
              </>
            )}
          </div>
        </li>

        {/* 3. 임베딩 모델 (RAG) — 원클릭 설치 */}
        {ragOn && (
          <li className={`setup-step setup-step--${embedState}`}>
            <span className="setup-step__mark">{embedState === 'done' ? '✓' : '3'}</span>
            <div className="setup-step__body">
              <div className="setup-step__label">
                임베딩 모델 <span className="mono setup-step__model">{embedModel}</span>
                <span className="setup-step__tag">RAG</span>
              </div>
              {embedState === 'done' ? (
                <div className="setup-step__hint">설치됨 — RAG 검색 사용 가능</div>
              ) : embedState === 'pending' ? (
                <div className="setup-step__hint">Ollama 실행 후 설치할 수 있어요</div>
              ) : active === embedModel ? (
                <div className="setup-pull">
                  <div className="setup-pull__bar">
                    <span style={{ width: `${pct ?? 0}%` }} />
                  </div>
                  <div className="setup-pull__meta mono">
                    {status}
                    {pct != null ? ` · ${pct}%` : ''}
                  </div>
                </div>
              ) : (
                <>
                  <div className="setup-step__hint">
                    RAG(의미 검색)에 필요한 모델입니다. 앱에서 바로 받을 수 있어요.
                  </div>
                  {errs[embedModel] && <div className="setup-step__err">{errs[embedModel]}</div>}
                  <button
                    className="setup-btn"
                    disabled={!!active}
                    onClick={() => void pull(embedModel)}
                  >
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
