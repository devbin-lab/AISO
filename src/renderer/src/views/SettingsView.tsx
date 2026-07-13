import { useEffect, useMemo, useRef, useState } from 'react'
import {
  type AppSettings,
  type ReasoningEffort,
  type ThemeMode,
  type TempPreset,
  TEMP_MODE_OPTIONS,
  RAG_MAX_FILES_OPTIONS
} from '../../../shared/settings'
import type { HealthInfo } from '../../../shared/backend'
import type { UpdateStatus } from '../../../shared/update'
import Select from '../components/Select'
import Segmented from '../components/Segmented'
import Dropdown from '../components/Dropdown'
import { confirmDialog } from '../components/ConfirmDialog'

const ONOFF = [
  { v: true, label: '켜짐' },
  { v: false, label: '꺼짐' }
]

interface Props {
  settings: AppSettings
  health: HealthInfo | null
  onSave: (patch: Partial<AppSettings>) => Promise<void>
  /** 지금 이 화면이 실제로 보이는 탭인지 — 개발자 모드 단축키 감지 범위를 설정 탭에 한정한다.
   *  (뷰는 항상 마운트 상태로 유지되므로 이 플래그 없이는 다른 탭에서도 단축키가 먹힌다.) */
  active: boolean
}

const DEV_UNLOCK_TAPS = 10 // 이 횟수만큼 눌러야 개발자 모드 활성화
const DEV_UNLOCK_WINDOW_MS = 3000 // 이 시간 안에 연타해야 카운트(느리게 누르면 리셋)

const EFFORTS: { v: ReasoningEffort; label: string }[] = [
  { v: 'low', label: 'Low' },
  { v: 'medium', label: 'Medium' },
  { v: 'high', label: 'High' }
]

const THEMES: { v: ThemeMode; label: string }[] = [
  { v: 'dark', label: '다크' },
  { v: 'light', label: '라이트' },
  { v: 'system', label: '시스템' }
]

// 모델 상주 유지(keep_alive) — Ollama 형식 문자열
const KEEP_ALIVE: { v: string; label: string }[] = [
  { v: '5m', label: '5분' },
  { v: '30m', label: '30분' },
  { v: '-1', label: '항상' },
  { v: '0', label: '끔' }
]

// 세부조정 없이 6단계로만 고른다 (4K ~ 128K)
const TOKEN_STEPS = [4096, 8192, 16384, 32768, 65536, 131072]
const TOKEN_LABELS = ['4K', '8K', '16K', '32K', '64K', '128K']

/** 저장된 값이 단계 목록에 없으면(과거 기본값 등) 가장 가까운 단계로 스냅한다. */
function nearestTokenIndex(v: number): number {
  let idx = 0
  let best = Infinity
  TOKEN_STEPS.forEach((s, i) => {
    const d = Math.abs(s - v)
    if (d < best) {
      best = d
      idx = i
    }
  })
  return idx
}

/** 슬라이더 배경 — 채움(오렌지)/미채움(연한 회색)을 값 비율(0~100)로 그린다. */
function rangeFill(pct: number): React.CSSProperties {
  const p = Math.max(0, Math.min(100, pct))
  return { background: `linear-gradient(to right, var(--accent) ${p}%, var(--track) ${p}%)` }
}

function SettingsView({ settings, health, onSave, active }: Props): React.JSX.Element {
  const [form, setForm] = useState<AppSettings>(settings)
  const [saved, setSaved] = useState(false)
  const [manualModel, setManualModel] = useState(false)

  const modelOptions = health?.models ?? []
  const useDropdown = modelOptions.length > 0 && !manualModel

  // 외부 설정이 로드/변경되면 폼 동기화
  useEffect(() => {
    setForm(settings)
  }, [settings])

  const dirty = useMemo(
    () => (Object.keys(form) as (keyof AppSettings)[]).some((k) => form[k] !== settings[k]),
    [form, settings]
  )

  const set = <K extends keyof AppSettings,>(k: K, v: AppSettings[K]): void => {
    setForm((f) => ({ ...f, [k]: v }))
    setSaved(false)
  }

  const submit = async (): Promise<void> => {
    await onSave(form)
    setSaved(true)
    window.setTimeout(() => setSaved(false), 1800)
  }

  // ── 자동 업데이트 (GitHub 릴리스) ──
  const [appVersion, setAppVersion] = useState('')
  const [upd, setUpd] = useState<UpdateStatus>({ state: 'idle' })
  useEffect(() => {
    window.api.updates.version().then(setAppVersion).catch(() => {})
    return window.api.updates.onStatus(setUpd)
  }, [])
  const checkUpdate = (): void => {
    setUpd({ state: 'checking' })
    // dev-disabled·error는 이벤트가 안 올 수 있어 반환값으로 즉시 반영
    void window.api.updates.check().then((s) => {
      if (s.state === 'dev-disabled' || s.state === 'error') setUpd(s)
    })
  }
  // ── 개발자 모드: Ctrl+Shift+F1 10회 연타로 활성화(설정 탭에서만 감지) ──
  const [devToast, setDevToast] = useState<string | null>(null)
  const devModeRef = useRef(settings.devMode) // 키 핸들러 클로저가 최신 값을 보게(리스너 재등록 없이)
  const tapsRef = useRef<number[]>([])
  useEffect(() => {
    devModeRef.current = settings.devMode
  }, [settings.devMode])
  useEffect(() => {
    if (!active) return // 설정 탭이 실제로 보일 때만 감지 — 뷰가 항상 마운트라 필요
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.repeat || e.key !== 'F1' || !e.ctrlKey || !e.shiftKey) return
      e.preventDefault()
      if (devModeRef.current) return // 이미 켜져 있으면 무시
      const now = Date.now()
      const taps = tapsRef.current.filter((t) => now - t < DEV_UNLOCK_WINDOW_MS)
      taps.push(now)
      tapsRef.current = taps
      if (taps.length >= DEV_UNLOCK_TAPS) {
        tapsRef.current = []
        devModeRef.current = true // 라운드트립 끝나기 전 재연타로 다시 트리거되는 것 방지
        void onSave({ devMode: true })
        setDevToast('🔓 개발자 모드가 활성화되었습니다')
        window.setTimeout(() => setDevToast(null), 2500)
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [active, onSave])

  const disableDevMode = (): void => {
    void onSave({ devMode: false, forceOnboarding: false })
  }

  const factoryReset = async (): Promise<void> => {
    const ok = await confirmDialog({
      title: '공장초기화',
      message:
        '설정·대화·토큰 사용량 기록이 모두 삭제되고 앱이 처음 설치 상태로 돌아갑니다.\n(작업 폴더의 파일과 RAG 색인은 그대로 유지됩니다.)\n계속할까요?',
      confirmLabel: '초기화',
      danger: true
    })
    if (!ok) return
    await window.api.factoryReset()
    window.location.reload()
  }

  const updBusy = upd.state === 'checking' || upd.state === 'downloading'
  const updHint = ((): string => {
    switch (upd.state) {
      case 'checking':
        return '릴리스 확인 중…'
      case 'up-to-date':
        return '최신 버전입니다.'
      case 'available':
        return `새 버전 v${upd.version} 이(가) 있습니다.`
      case 'downloading':
        return `다운로드 중… ${upd.percent ?? 0}%`
      case 'downloaded':
        return `v${upd.version} 다운로드 완료 — 재시작하면 설치됩니다.`
      case 'dev-disabled':
        return '개발 모드에선 확인할 수 없습니다 (설치본에서만 동작).'
      case 'error':
        return `오류: ${upd.message ?? '알 수 없음'}`
      default:
        return 'GitHub 릴리스에서 새 버전을 확인합니다.'
    }
  })()

  return (
    <div className="view">
      <header className="view__head">
        <h1>설정</h1>
        <p className="view__desc">AI 엔진과 화면 설정</p>
      </header>

      <div className="settings">
        <section>
          <div className="group__title">엔진</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">모델</div>
                <div className="row__hint">
                  {modelOptions.length > 0
                    ? 'Ollama에 설치된 모델 중 선택'
                    : 'Ollama 연결 후 설치된 모델 목록이 표시됩니다'}
                </div>
              </div>
              <div className="row__control">
                {useDropdown ? (
                  <Select
                    value={form.model}
                    options={modelOptions}
                    onChange={(v) => set('model', v)}
                  />
                ) : (
                  <input
                    className="input"
                    value={form.model}
                    onChange={(e) => set('model', e.target.value)}
                    placeholder="gemma4:12b"
                  />
                )}
                {modelOptions.length > 0 && (
                  <button
                    type="button"
                    className="linklike"
                    onClick={() => setManualModel((m) => !m)}
                  >
                    {useDropdown ? '직접 입력' : '목록에서 선택'}
                  </button>
                )}
              </div>
            </div>
            <div className="row">
              <div>
                <div className="row__label">Ollama 호스트</div>
                <div className="row__hint">FastAPI 사이드카가 통신할 주소</div>
              </div>
              <input
                className="input"
                value={form.ollamaHost}
                onChange={(e) => set('ollamaHost', e.target.value)}
                placeholder="http://localhost:11434"
              />
            </div>
          </div>
        </section>

        <section>
          <div className="group__title">생성</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">추론 강도</div>
                <div className="row__hint">모델 사고(think) 깊이 · Low 빠름 / High 정확</div>
              </div>
              <Segmented
                value={form.reasoningEffort}
                options={EFFORTS}
                onChange={(v) => set('reasoningEffort', v)}
              />
            </div>
            <div className="row">
              <div>
                <div className="row__label">생성 온도</div>
                <div className="row__hint">
                  자동이면 요청 내용을 보고 정리·분류/코딩·일반 온도를 매번 골라줍니다. 커스텀만 직접 조정합니다.
                </div>
              </div>
              <Dropdown
                value={form.tempPreset}
                options={TEMP_MODE_OPTIONS}
                onChange={(v) => set('tempPreset', v as TempPreset)}
                align="right"
                title="생성 온도 모드"
              />
            </div>
            {form.tempPreset === 'custom' && (
              <div className="row">
                <div>
                  <div className="row__label">커스텀 온도</div>
                  <div className="row__hint">0 결정적 · 1 창의적</div>
                </div>
                <div className="temp">
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.05}
                    value={form.tempCustom}
                    onChange={(e) => set('tempCustom', Number(e.target.value))}
                    className="range"
                    style={rangeFill(form.tempCustom * 100)}
                  />
                  <span className="temp__val mono">{form.tempCustom.toFixed(2)}</span>
                </div>
              </div>
            )}
            <div className="row">
              <div>
                <div className="row__label">컨텍스트 길이</div>
                <div className="row__hint">작업 기억(토큰 창) · 클수록 긴 작업 가능, VRAM↑ · 4K ~ 128K</div>
              </div>
              <div className="temp">
                <input
                  type="range"
                  min={0}
                  max={TOKEN_STEPS.length - 1}
                  step={1}
                  value={nearestTokenIndex(form.contextLength)}
                  onChange={(e) => set('contextLength', TOKEN_STEPS[Number(e.target.value)])}
                  className="range range--stepped"
                  style={rangeFill((nearestTokenIndex(form.contextLength) / (TOKEN_STEPS.length - 1)) * 100)}
                  list="token-steps"
                />
                <datalist id="token-steps">
                  {TOKEN_STEPS.map((_, i) => (
                    <option key={i} value={i} />
                  ))}
                </datalist>
                <span className="temp__val mono">
                  {TOKEN_LABELS[nearestTokenIndex(form.contextLength)]}
                </span>
              </div>
            </div>
          </div>
        </section>

        <section>
          <div className="group__title">웹 검색</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">채팅 웹 검색</div>
                <div className="row__hint">
                  채팅에서 모르는 최신 정보·사실을 물으면 <b>자동으로</b> 인터넷을 조사해 답합니다(DuckDuckGo·여러 출처 교차 확인). 끄면 전부 로컬에서만 처리합니다.
                </div>
              </div>
              <Segmented
                value={form.chatWebSearch}
                options={[
                  { v: true, label: '자동' },
                  { v: false, label: '끔' }
                ]}
                onChange={(v) => set('chatWebSearch', v)}
              />
            </div>
          </div>
        </section>

        <section>
          <div className="group__title">RAG · 검색 증강</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">RAG 사용</div>
                <div className="row__hint">에이전트가 색인된 작업 폴더에서 관련 코드·문서를 자동 참고</div>
              </div>
              <Segmented
                value={form.ragEnabled}
                options={[
                  { v: true, label: '사용' },
                  { v: false, label: '끔' }
                ]}
                onChange={(v) => set('ragEnabled', v)}
              />
            </div>
            <div className="row">
              <div>
                <div className="row__label">임베딩 모델</div>
                <div className="row__hint">
                  색인·검색 전용 · 채팅 모델과 독립(바꿔도 재색인 불필요) · 한국어는 bge-m3 권장
                </div>
              </div>
              <input
                className="input"
                value={form.embeddingModel}
                onChange={(e) => set('embeddingModel', e.target.value)}
                placeholder="bge-m3"
              />
            </div>
            <div className="row">
              <div>
                <div className="row__label">색인 범위 (최대 파일 수)</div>
                <div className="row__hint">
                  많을수록 더 많이 참고하지만 색인이 느려집니다 · 폴더가 커서 한계에 닿으면 늘리라고 안내합니다
                </div>
              </div>
              <Dropdown
                value={String(form.ragMaxFiles)}
                options={RAG_MAX_FILES_OPTIONS.map((o) => ({
                  value: String(o.value),
                  label: o.label
                }))}
                onChange={(v) => set('ragMaxFiles', Number(v))}
                align="right"
                title="RAG 색인에 넣을 최대 파일 수"
              />
            </div>
          </div>
        </section>

        <section>
          <div className="group__title">성능</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">모델 상주 유지</div>
                <div className="row__hint">
                  유휴 시 모델 언로드로 인한 콜드 재로드(~5.8초) 방지 · 상주는 VRAM을 계속 점유
                </div>
              </div>
              <Segmented
                value={form.keepAlive}
                options={KEEP_ALIVE}
                onChange={(v) => set('keepAlive', v)}
              />
            </div>
            <div className="row">
              <div>
                <div className="row__label">Ollama 서버 팁</div>
                <div className="row__hint">
                  <b>OLLAMA_NUM_PARALLEL=1</b>(기본) 유지를 권장합니다.
                  <b> OLLAMA_KV_CACHE_TYPE</b>는 gpt-oss에서 미지원이니 gpt-oss 사용 시 설정하지 마세요.
                </div>
              </div>
            </div>
          </div>
        </section>

        <section>
          <div className="group__title">화면</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">테마</div>
                <div className="row__hint">시스템은 OS 설정을 따릅니다</div>
              </div>
              <Segmented value={form.theme} options={THEMES} onChange={(v) => set('theme', v)} />
            </div>
          </div>
        </section>

        <section>
          <div className="group__title">업데이트</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">버전{appVersion && ` · v${appVersion}`}</div>
                <div className="row__hint">{updHint}</div>
              </div>
              <div className="update-actions">
                {upd.state === 'downloaded' ? (
                  <button className="btn" onClick={() => void window.api.updates.install()}>
                    재시작하여 설치
                  </button>
                ) : upd.state === 'available' ? (
                  <button className="btn" onClick={() => void window.api.updates.download()}>
                    v{upd.version} 다운로드
                  </button>
                ) : (
                  <button className="btn btn--ghost2" disabled={updBusy} onClick={checkUpdate}>
                    {upd.state === 'checking' ? '확인 중…' : '업데이트 확인'}
                  </button>
                )}
              </div>
            </div>
            {upd.state === 'downloading' && (
              <div className="row">
                <div className="update-progress">
                  <div
                    className="update-progress__bar"
                    style={{ width: `${upd.percent ?? 0}%` }}
                  />
                </div>
                <span className="temp__val mono">{upd.percent ?? 0}%</span>
              </div>
            )}
          </div>
        </section>

        {form.devMode && (
          <section>
            <div className="group__title">개발자</div>
            <div className="group">
              <div className="row">
                <div>
                  <div className="row__label">온보딩 미리보기</div>
                  <div className="row__hint">
                    모델을 지우지 않고도 홈에서 최초 설치 안내 화면을 강제로 띄웁니다 · 저장 후 홈에서 확인
                  </div>
                </div>
                <Segmented
                  value={form.forceOnboarding}
                  options={ONOFF}
                  onChange={(v) => set('forceOnboarding', v)}
                />
              </div>
              <div className="row">
                <div>
                  <div className="row__label">공장초기화</div>
                  <div className="row__hint">
                    설정·대화·사용량 기록을 모두 지우고 처음 상태로 되돌립니다 (되돌릴 수 없음)
                  </div>
                </div>
                <button className="btn btn--stop" onClick={() => void factoryReset()}>
                  초기화
                </button>
              </div>
              <div className="row">
                <div>
                  <div className="row__label">개발자 모드 끄기</div>
                  <div className="row__hint">
                    다시 켜려면 이 화면에서 Ctrl+Shift+F1을 10회 누르세요
                  </div>
                </div>
                <button className="btn btn--ghost2" onClick={disableDevMode}>
                  끄기
                </button>
              </div>
            </div>
          </section>
        )}

        <div className="actions">
          {saved && <span className="saved">저장됨</span>}
          <button className="btn" disabled={!dirty} onClick={submit}>
            저장
          </button>
        </div>
      </div>
      {devToast && <div className="dev-toast">{devToast}</div>}
    </div>
  )
}

export default SettingsView
