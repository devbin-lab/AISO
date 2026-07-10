import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

const SHOW_DELAY = 340 // 마우스를 올린 뒤 표시까지 지연(ms)
const GAP = 8 // 대상과 툴팁 사이 간격
const MARGIN = 8 // 화면 가장자리 최소 여백

/** data-tip 속성을 가진 요소에 마우스를 올리면(또는 포커스하면) 뜨는 커스텀 툴팁.
 *  앱 루트에 한 번만 마운트한다. body로 포털·position:fixed 라서 overflow:hidden
 *  컨테이너에 잘리지 않고 화면 가장자리에서도 뒤집혀 항상 보인다.
 *  표시 중에는 requestAnimationFrame 루프로 대상의 실시간 위치를 따라가고, 대상이
 *  DOM에서 사라지거나 숨겨지면 자동으로 닫힌다(포인터 이벤트 없이 목록 재정렬·삭제되는
 *  경우에도 툴팁이 허공에 남지 않게). 창 크기·확대 변경도 이 루프가 자연히 반영한다. */
function TooltipHost(): React.JSX.Element | null {
  const [text, setText] = useState<string | null>(null)
  const ref = useRef<HTMLDivElement>(null)
  const timerRef = useRef<number | null>(null)
  const targetRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    const clearTimer = (): void => {
      if (timerRef.current != null) {
        window.clearTimeout(timerRef.current)
        timerRef.current = null
      }
    }
    const hide = (): void => {
      clearTimer()
      targetRef.current = null
      setText(null)
    }
    const show = (el: HTMLElement): void => {
      const tip = el.getAttribute('data-tip')
      if (!tip) return
      clearTimer()
      targetRef.current = el
      timerRef.current = window.setTimeout(() => {
        if (targetRef.current === el && el.isConnected) setText(tip)
      }, SHOW_DELAY)
    }
    const tipEl = (t: EventTarget | null): HTMLElement | null =>
      (t as Element | null)?.closest?.('[data-tip]') as HTMLElement | null

    const onOver = (e: PointerEvent): void => {
      const el = tipEl(e.target)
      if (el && el !== targetRef.current) show(el)
    }
    const onOut = (e: PointerEvent): void => {
      const el = tipEl(e.target)
      if (el && el === targetRef.current) {
        const to = e.relatedTarget as Node | null
        if (!to || !el.contains(to)) hide() // 요소 내부 이동(자식 아이콘 등)에는 유지
      }
    }
    const onFocusIn = (e: FocusEvent): void => {
      const el = tipEl(e.target)
      if (el) show(el)
    }
    document.addEventListener('pointerover', onOver, true)
    document.addEventListener('pointerout', onOut, true)
    document.addEventListener('focusin', onFocusIn, true)
    document.addEventListener('focusout', hide, true)
    document.addEventListener('pointerdown', hide, true) // 클릭 시 숨김
    window.addEventListener('blur', hide)
    return () => {
      clearTimer()
      document.removeEventListener('pointerover', onOver, true)
      document.removeEventListener('pointerout', onOut, true)
      document.removeEventListener('focusin', onFocusIn, true)
      document.removeEventListener('focusout', hide, true)
      document.removeEventListener('pointerdown', hide, true)
      window.removeEventListener('blur', hide)
    }
  }, [])

  // 표시되는 동안 대상 실시간 위치를 따라가며 배치. 대상이 사라지면 닫는다.
  useLayoutEffect(() => {
    if (text == null) return
    const el = ref.current
    if (!el) return
    // 대상 기준으로 위치 계산(위 우선, 공간 없으면 아래로 뒤집기, 좌우 클램프).
    // 대상이 없거나 DOM에서 분리·숨김되면 false → 툴팁 닫기.
    const place = (): boolean => {
      const target = targetRef.current
      if (!target || !target.isConnected) return false
      const rect = target.getBoundingClientRect()
      if (rect.width === 0 && rect.height === 0) return false // display:none 등
      const tw = el.offsetWidth
      const th = el.offsetHeight
      const vw = window.innerWidth
      const vh = window.innerHeight
      let top = rect.top - th - GAP
      if (top < MARGIN) {
        top = rect.bottom + GAP
        if (top + th > vh - MARGIN) top = Math.max(MARGIN, vh - th - MARGIN)
      }
      let left = rect.left + rect.width / 2 - tw / 2
      left = Math.max(MARGIN, Math.min(left, vw - tw - MARGIN))
      el.style.top = `${Math.round(top)}px`
      el.style.left = `${Math.round(left)}px`
      return true
    }
    // 첫 배치는 페인트 전에 동기 수행 → 좌상단에서 튀는 깜빡임 없음
    if (!place()) {
      targetRef.current = null
      setText(null)
      return
    }
    let raf = 0
    const loop = (): void => {
      if (!place()) {
        targetRef.current = null
        setText(null)
        return
      }
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf)
  }, [text])

  if (text == null) return null
  return createPortal(
    <div className="tooltip" ref={ref} role="tooltip">
      {text}
    </div>,
    document.body
  )
}

export default TooltipHost
