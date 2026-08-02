import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import type { ActiveLlmProvider } from '../../../shared/settings'
import { PanelRightIcon } from './icons'

interface ConvToggle {
  collapsed: boolean
  onToggle: () => void
}

interface Props {
  backend: BackendInfo
  health: HealthInfo | null
  provider: ActiveLlmProvider
  convToggle?: ConvToggle | null
}

type ConnKind = 'ok' | 'warn' | 'error' | 'checking'

function connState(
  backend: BackendInfo,
  health: HealthInfo | null,
  provider: ActiveLlmProvider
): { kind: ConnKind; label: string; title?: string } {
  if (backend.state === 'starting') return { kind: 'checking', label: '엔진 시작 중' }
  if (backend.state === 'error' || backend.state === 'stopped')
    return { kind: 'error', label: '엔진 오류', title: backend.detail }
  if (provider === 'nvidia') return { kind: 'warn', label: 'NVIDIA 선택됨', title: '대화는 설정된 NVIDIA 서비스로 전송됩니다.' }
  if (!health) return { kind: 'checking', label: '확인 중' }
  if (!health.ollama) return { kind: 'error', label: 'Ollama 미연결', title: health.detail }
  return { kind: 'ok', label: '연결됨' }
}

function Titlebar({ backend, health, provider, convToggle }: Props): React.JSX.Element {
  const s = connState(backend, health, provider)
  return (
    <header className="titlebar">
      <div className="titlebar__left">
        <div className="titlebar__brand">
          <span className="titlebar__mark" />
          Aiso
        </div>
        {convToggle && (
          <button
            className="iconbtn titlebar__convtoggle"
            data-tip={convToggle.collapsed ? '대화 목록 열기' : '대화 목록 접기'}
            aria-label="대화 목록 토글"
            onClick={convToggle.onToggle}
          >
            <PanelRightIcon size={15} />
          </button>
        )}
      </div>
      <div className="titlebar__right">
        <div className={`conn conn--${s.kind}`} data-tip={s.title}>
          <span className="conn__dot" />
          {s.label}
        </div>
      </div>
    </header>
  )
}

export default Titlebar
