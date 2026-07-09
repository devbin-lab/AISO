import type { BackendInfo, HealthInfo } from '../../../shared/backend'

interface Props {
  backend: BackendInfo
  health: HealthInfo | null
}

type ConnKind = 'ok' | 'warn' | 'error' | 'checking'

function connState(backend: BackendInfo, health: HealthInfo | null): { kind: ConnKind; label: string; title?: string } {
  if (backend.state === 'starting') return { kind: 'checking', label: '엔진 시작 중' }
  if (backend.state === 'error' || backend.state === 'stopped')
    return { kind: 'error', label: '엔진 오류', title: backend.detail }
  if (!health) return { kind: 'checking', label: '확인 중' }
  if (!health.ollama) return { kind: 'error', label: 'Ollama 미연결', title: health.detail }
  return { kind: 'ok', label: '연결됨' }
}

function Titlebar({ backend, health }: Props): React.JSX.Element {
  const s = connState(backend, health)
  return (
    <header className="titlebar">
      <div className="titlebar__brand">
        <span className="titlebar__mark" />
        Aiso
      </div>
      <div className="titlebar__right">
        <div className={`conn conn--${s.kind}`} title={s.title}>
          <span className="conn__dot" />
          {s.label}
        </div>
      </div>
    </header>
  )
}

export default Titlebar
