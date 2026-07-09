import { useEffect, useMemo, useState } from 'react'
import {
  type AppSettings,
  type ReasoningEffort,
  type ThemeMode,
  TEMP_PRESET_META
} from '../../../shared/settings'
import type { HealthInfo } from '../../../shared/backend'
import Select from '../components/Select'

interface Props {
  settings: AppSettings
  health: HealthInfo | null
  onSave: (patch: Partial<AppSettings>) => Promise<void>
}

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

function SettingsView({ settings, health, onSave }: Props): React.JSX.Element {
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
              <div className="seg">
                {EFFORTS.map((o) => (
                  <button
                    key={o.v}
                    type="button"
                    className={`seg__opt ${form.reasoningEffort === o.v ? 'seg__opt--on' : ''}`}
                    onClick={() => set('reasoningEffort', o.v)}
                  >
                    {o.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="row row--stack">
              <div>
                <div className="row__label">생성 온도 (작업별 프리셋)</div>
                <div className="row__hint">
                  0 결정적 · 1 창의적 — 정리는 낮게, 창작은 높게. 활성 프리셋은 대화창 하단에서 선택합니다.
                </div>
              </div>
              <div className="temp-presets">
                {TEMP_PRESET_META.map((p) => (
                  <div
                    className={`temp-preset ${form.tempPreset === p.id ? 'temp-preset--active' : ''}`}
                    key={p.id}
                  >
                    <button
                      type="button"
                      className="temp-preset__name"
                      title={`활성 프리셋으로 지정 · ${p.hint}`}
                      onClick={() => set('tempPreset', p.id)}
                    >
                      {form.tempPreset === p.id ? '● ' : '○ '}
                      {p.label}
                    </button>
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      value={form[p.field]}
                      onChange={(e) => set(p.field, Number(e.target.value))}
                      className="range"
                    />
                    <span className="temp__val mono">{form[p.field].toFixed(2)}</span>
                  </div>
                ))}
              </div>
            </div>
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
          <div className="group__title">RAG · 검색 증강</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">RAG 사용</div>
                <div className="row__hint">에이전트가 색인된 작업 폴더에서 관련 코드·문서를 자동 참고</div>
              </div>
              <div className="seg">
                {[
                  { v: true, label: '사용' },
                  { v: false, label: '끔' }
                ].map((o) => (
                  <button
                    key={String(o.v)}
                    type="button"
                    className={`seg__opt ${form.ragEnabled === o.v ? 'seg__opt--on' : ''}`}
                    onClick={() => set('ragEnabled', o.v)}
                  >
                    {o.label}
                  </button>
                ))}
              </div>
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
              <div className="seg">
                {KEEP_ALIVE.map((o) => (
                  <button
                    key={o.v}
                    type="button"
                    className={`seg__opt ${form.keepAlive === o.v ? 'seg__opt--on' : ''}`}
                    onClick={() => set('keepAlive', o.v)}
                  >
                    {o.label}
                  </button>
                ))}
              </div>
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
              <div className="seg">
                {THEMES.map((o) => (
                  <button
                    key={o.v}
                    type="button"
                    className={`seg__opt ${form.theme === o.v ? 'seg__opt--on' : ''}`}
                    onClick={() => set('theme', o.v)}
                  >
                    {o.label}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </section>

        <div className="actions">
          {saved && <span className="saved">저장됨</span>}
          <button className="btn" disabled={!dirty} onClick={submit}>
            저장
          </button>
        </div>
      </div>
    </div>
  )
}

export default SettingsView
