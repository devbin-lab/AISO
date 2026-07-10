import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

export interface MenuItem {
  label: string
  onClick: () => void
  danger?: boolean
}

interface Props {
  x: number
  y: number
  items: MenuItem[]
  onClose: () => void
}

const MARGIN = 8

/** 커서 위치에 뜨는 우클릭 컨텍스트 메뉴 — body로 포털, 화면 밖으로 나가면 뒤집힌다.
 *  전체 화면 배경막 없이 문서 리스너로 바깥 상호작용을 감지해 닫으므로, 다른 행을
 *  한 번의 우클릭으로 곧바로 다시 열 수 있고(배경막이 이벤트를 삼키지 않음) 프레임리스
 *  타이틀바 드래그 영역도 가리지 않는다. */
function ContextMenu({ x, y, items, onClose }: Props): React.JSX.Element {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ left: x, top: y })

  // 렌더 후 크기를 재서 화면 안으로 클램프(우/하단 넘치면 왼쪽·위로 뒤집기). 페인트 전 동기 수행 → 깜빡임 없음.
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const w = el.offsetWidth
    const h = el.offsetHeight
    const vw = window.innerWidth
    const vh = window.innerHeight
    let left = x
    let top = y
    if (left + w > vw - MARGIN) left = Math.max(MARGIN, x - w)
    if (top + h > vh - MARGIN) top = Math.max(MARGIN, vh - h - MARGIN)
    setPos({ left, top })
  }, [x, y])

  useEffect(() => {
    // 메뉴 밖 mousedown이면 닫기. 다른 행 우클릭 시엔 이 mousedown(버튼2)이 먼저 닫고,
    // 곧이어 그 행의 onContextMenu가 새 메뉴를 연다 → 한 번의 우클릭으로 재타깃.
    const onDown = (e: MouseEvent): void => {
      if (!ref.current || !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDown, true)
    document.addEventListener('keydown', onKey, true)
    window.addEventListener('wheel', onClose, true)
    window.addEventListener('resize', onClose)
    window.addEventListener('blur', onClose)
    return () => {
      document.removeEventListener('mousedown', onDown, true)
      document.removeEventListener('keydown', onKey, true)
      window.removeEventListener('wheel', onClose, true)
      window.removeEventListener('resize', onClose)
      window.removeEventListener('blur', onClose)
    }
  }, [onClose])

  return createPortal(
    <div
      className="ctxmenu"
      ref={ref}
      role="menu"
      style={{ left: pos.left, top: pos.top }}
      onContextMenu={(e) => e.preventDefault()}
    >
      {items.map((it, i) => (
        <button
          key={i}
          type="button"
          role="menuitem"
          className={`ctxmenu__item ${it.danger ? 'ctxmenu__item--danger' : ''}`}
          onClick={() => {
            it.onClick()
            onClose()
          }}
        >
          {it.label}
        </button>
      ))}
    </div>,
    document.body
  )
}

export default ContextMenu
