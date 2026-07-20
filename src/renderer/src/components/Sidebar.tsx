import { useLayoutEffect, useRef, useState } from 'react'
import { HomeIcon, ChatIcon, AgentIcon, ComfyIcon, SlidersIcon } from './icons'

export type ViewKey = 'home' | 'chat' | 'agent' | 'comfy' | 'settings'

const NAV: { key: ViewKey; label: string; Icon: typeof HomeIcon }[] = [
  { key: 'home', label: '홈', Icon: HomeIcon },
  { key: 'chat', label: '채팅', Icon: ChatIcon },
  { key: 'agent', label: '에이전트', Icon: AgentIcon },
  { key: 'comfy', label: 'ComfyUI', Icon: ComfyIcon },
  { key: 'settings', label: '설정', Icon: SlidersIcon }
]

interface Props {
  view: ViewKey
  onNavigate: (v: ViewKey) => void
}

function Sidebar({ view, onNavigate }: Props): React.JSX.Element {
  const railRef = useRef<HTMLElement>(null)
  const [thumb, setThumb] = useState<{ top: number; height: number } | null>(null)

  // 선택된 아이콘 버튼의 실제 위치를 측정해 thumb(강조 배경+표시바)를 그 자리로 슬라이드시킨다.
  useLayoutEffect(() => {
    const el = railRef.current
    if (!el) return
    const measure = (): void => {
      const btn = el.querySelector<HTMLButtonElement>('.rail__btn--active')
      if (btn && btn.offsetHeight > 0) setThumb({ top: btn.offsetTop, height: btn.offsetHeight })
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [view])

  return (
    <nav className="rail" ref={railRef}>
      {thumb && <div className="rail__thumb" style={{ top: thumb.top, height: thumb.height }} />}
      {NAV.map(({ key, label, Icon }) => (
        <button
          key={key}
          type="button"
          data-tip={label}
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
