import { Fragment, useEffect, useState } from 'react'
import type { AppSettings } from '../../../shared/settings'
import type { PingResult } from '../../../shared/ipc'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import type { UsageSummary } from '../../../shared/usage'
import { RefreshIcon } from '../components/icons'
import SetupCard from '../components/SetupCard'

const fmtTokens = (n: number): string =>
  n >= 1_000_000
    ? `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`
    : n >= 10_000
      ? `${(n / 1000).toFixed(0)}K`
      : n.toLocaleString('en-US')

interface Props {
  settings: AppSettings
  backend: BackendInfo
  health: HealthInfo | null
  onSaveSettings: (patch: Partial<AppSettings>) => Promise<boolean>
}

const BACKEND_LABEL: Record<BackendInfo['state'], string> = {
  starting: '시작 중',
  ready: '실행 중',
  error: '오류',
  stopped: '중지됨'
}

type Conn = 'checking' | 'ok' | 'error'

const STEPS: { n: string; label: string; state: 'done' | 'active' | '' }[] = [
  { n: '01', label: '셋업', state: 'done' },
  { n: '02', label: 'UI', state: 'done' },
  { n: '03', label: '로컬 채팅', state: 'done' },
  { n: '04', label: '하네스', state: 'done' },
  { n: '05', label: '자가검증', state: 'done' },
  { n: '06', label: 'RAG', state: 'done' }
]

function HomeView({ settings, backend, health, onSaveSettings }: Props): React.JSX.Element {
  const [ping, setPing] = useState<PingResult | null>(null)
  const [conn, setConn] = useState<Conn>('checking')
  const [usage, setUsage] = useState<UsageSummary | null>(null)

  const loadUsage = (): void => {
    window.api?.usage
      ?.summary()
      .then(setUsage)
      .catch(() => {})
  }

  const refresh = async (): Promise<void> => {
    setConn('checking')
    loadUsage()
    try {
      if (!window.api?.ping) throw new Error('preload API 없음')
      setPing(await window.api.ping())
      setConn('ok')
    } catch {
      setConn('error')
    }
  }

  useEffect(() => {
    refresh()
    // 홈은 항상 마운트 상태(숨김) — 실행 후 돌아오면 최신 수치가 보이게 주기적 갱신
    const t = window.setInterval(loadUsage, 20_000)
    return () => window.clearInterval(t)
  }, [])

  const connText = conn === 'ok' ? 'IPC 정상' : conn === 'checking' ? '확인 중' : '연결 안 됨'
  const daily = usage?.daily ?? []
  const maxTokens = Math.max(1, ...daily.map((d) => d.tokens))

  return (
    <div className="view">
      <header className="view__head">
        <h1>홈</h1>
        <p className="view__desc">로컬에서 도는, 스스로 검증하는 코딩·개발 크리에이터 도구</p>
      </header>

      <SetupCard
        settings={settings}
        backend={backend}
        health={health}
        onSaveSettings={onSaveSettings}
      />

      <div className="grid">
        <section className="panel">
          <div className="panel__head">
            <span className="panel__title">시스템</span>
            <button className="iconbtn" data-tip="새로고침" aria-label="새로고침" onClick={refresh}>
              <RefreshIcon />
            </button>
          </div>
          <div className={`state state--${conn}`}>
            <span className="state__dot" />
            {connText}
          </div>
          {ping && (
            <ul className="kv">
              <li>
                <span>Electron</span>
                <b className="mono">{ping.versions.electron}</b>
              </li>
              <li>
                <span>Chromium</span>
                <b className="mono">{ping.versions.chrome}</b>
              </li>
              <li>
                <span>Node</span>
                <b className="mono">{ping.versions.node}</b>
              </li>
              <li>
                <span>V8</span>
                <b className="mono">{ping.versions.v8}</b>
              </li>
            </ul>
          )}
        </section>

        <section className="panel">
          <div className="panel__head">
            <span className="panel__title">AI 엔진</span>
          </div>
          <ul className="kv">
            <li>
              <span>백엔드 (FastAPI)</span>
              <b>{BACKEND_LABEL[backend.state]}</b>
            </li>
            <li>
              <span>Ollama</span>
              <b>{health == null ? '—' : health.ollama ? '연결됨' : '미연결'}</b>
            </li>
            <li>
              <span>모델</span>
              <b className="mono">{settings.model}</b>
            </li>
            <li>
              <span>설치된 모델</span>
              <b>{health ? `${health.models.length}개` : '—'}</b>
            </li>
            <li>
              <span>추론 강도</span>
              <b>{settings.reasoningEffort}</b>
            </li>
          </ul>
        </section>

        <section className="panel panel--wide">
          <div className="panel__head">
            <span className="panel__title">토큰 사용량</span>
            <button className="iconbtn" data-tip="새로고침" aria-label="새로고침" onClick={loadUsage}>
              <RefreshIcon />
            </button>
          </div>
          <div className="usage-tiles">
            <div className="usage-tile">
              <span className="usage-tile__num mono">{fmtTokens(usage?.today ?? 0)}</span>
              <span className="usage-tile__label">오늘</span>
            </div>
            <div className="usage-tile">
              <span className="usage-tile__num mono">{fmtTokens(usage?.week ?? 0)}</span>
              <span className="usage-tile__label">최근 7일</span>
            </div>
            <div className="usage-tile">
              <span className="usage-tile__num mono">{fmtTokens(usage?.month ?? 0)}</span>
              <span className="usage-tile__label">최근 30일</span>
            </div>
          </div>
          <div className="usage-chart" role="img" aria-label="최근 30일 일별 토큰 사용량">
            {daily.map((d, i) => {
              const pct = (d.tokens / maxTokens) * 100
              const h = d.tokens > 0 ? Math.max(6, pct) : 2
              const isToday = i === daily.length - 1
              return (
                <span
                  key={d.day}
                  className={`usage-bar ${isToday ? 'usage-bar--today' : ''} ${d.tokens === 0 ? 'usage-bar--empty' : ''}`}
                  style={{ height: `${h}%` }}
                  data-tip={`${d.day.slice(5).replace('-', '/')} · ${d.tokens.toLocaleString('en-US')} 토큰`}
                />
              )
            })}
          </div>
          <div className="usage-axis">
            <span>30일 전</span>
            <span>오늘</span>
          </div>
        </section>

        <section className="panel panel--wide">
          <div className="panel__head">
            <span className="panel__title">로드맵</span>
          </div>
          <div className="steps">
            {STEPS.map((st, i) => (
              <Fragment key={st.n}>
                {i > 0 && <span className="steps__line" />}
                <div className={`step ${st.state ? `step--${st.state}` : ''}`}>
                  <span className="step__num">{st.state === 'done' ? '✓' : st.n}</span>
                  {st.label}
                </div>
              </Fragment>
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}

export default HomeView
