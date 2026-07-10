import { useLayoutEffect, useRef, useState } from 'react'

export interface SegOption<T> {
  v: T
  label: string
}

interface Props<T> {
  value: T
  options: SegOption<T>[]
  onChange: (v: T) => void
}

/** 세그먼트 토글 — 연한 회색 트랙은 하나로 이어지고, 흰 선택 칩(thumb)이 실제
 *  버튼 위치·너비를 측정해 그 위를 슬라이드하며 이동한다(라벨 폭이 서로 달라도 정확히 맞음). */
function Segmented<T,>({ value, options, onChange }: Props<T>): React.JSX.Element {
  const ref = useRef<HTMLDivElement>(null)
  const [thumb, setThumb] = useState<{ left: number; width: number } | null>(null)

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = (): void => {
      const btn = el.querySelector<HTMLButtonElement>('.seg__opt--on')
      // offsetWidth>0 가드: 숨겨진 뷰(display:none)에서 마운트되면 0이라 측정을 건너뛴다.
      if (btn && btn.offsetWidth > 0) setThumb({ left: btn.offsetLeft, width: btn.offsetWidth })
    }
    measure()
    // 뷰가 숨김→표시로 바뀌며 컨테이너 크기가 0→N이 될 때(또는 창 리사이즈 시) 재측정.
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [value, options])

  return (
    <div className="seg" ref={ref}>
      {thumb && <div className="seg__thumb" style={{ left: thumb.left, width: thumb.width }} />}
      {options.map((o) => (
        <button
          key={String(o.v)}
          type="button"
          className={`seg__opt ${value === o.v ? 'seg__opt--on' : ''}`}
          onClick={() => onChange(o.v)}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

export default Segmented
