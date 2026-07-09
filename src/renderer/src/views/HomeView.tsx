import { Fragment, useEffect, useState } from 'react'
import type { AppSettings } from '../../../shared/settings'
import type { PingResult } from '../../../shared/ipc'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import { RefreshIcon } from '../components/icons'

interface Props {
  settings: AppSettings
  backend: BackendInfo
  health: HealthInfo | null
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

function HomeView({ settings, backend, health }: Props): React.JSX.Element {
  const [ping, setPing] = useState<PingResult | null>(null)
  const [conn, setConn] = useState<Conn>('checking')

  const refresh = async (): Promise<void> => {
    setConn('checking')
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
  }, [])

  const connText = conn === 'ok' ? 'IPC 정상' : conn === 'checking' ? '확인 중' : '연결 안 됨'

  return (
    <div className="view">
      <header className="view__head">
        <h1>홈</h1>
        <p className="view__desc">로컬에서 도는, 스스로 검증하는 코딩·개발 크리에이터 도구</p>
      </header>

      <div className="grid">
        <section className="panel">
          <div className="panel__head">
            <span className="panel__title">시스템</span>
            <button className="iconbtn" title="새로고침" aria-label="새로고침" onClick={refresh}>
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
