import { HomeIcon, ChatIcon, AgentIcon, SlidersIcon } from './icons'

export type ViewKey = 'home' | 'chat' | 'agent' | 'settings'

const NAV: { key: ViewKey; label: string; Icon: typeof HomeIcon }[] = [
  { key: 'home', label: '홈', Icon: HomeIcon },
  { key: 'chat', label: '채팅', Icon: ChatIcon },
  { key: 'agent', label: '에이전트', Icon: AgentIcon },
  { key: 'settings', label: '설정', Icon: SlidersIcon }
]

interface Props {
  view: ViewKey
  onNavigate: (v: ViewKey) => void
}

function Sidebar({ view, onNavigate }: Props): React.JSX.Element {
  return (
    <nav className="rail">
      {NAV.map(({ key, label, Icon }) => (
        <button
          key={key}
          type="button"
          title={label}
          aria-label={label}
          data-view={key}
          className={`rail__btn ${view === key ? 'rail__btn--active' : ''}`}
          onClick={() => onNavigate(key)}
        >
          <Icon />
        </button>
      ))}
    </nav>
  )
}

export default Sidebar
