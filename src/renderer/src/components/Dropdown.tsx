import { useEffect, useRef, useState } from 'react'
import { ChevronDownIcon } from './icons'

export interface DropdownOption {
  value: string
  label: string
  hint?: string
}

interface Props {
  value: string
  options: DropdownOption[]
  onChange: (v: string) => void
  mono?: boolean
  align?: 'left' | 'right'
  disabled?: boolean
  title?: string
  placeholder?: string
}

/** 입력창 하단용 칩형 드롭다운 — 화면 아래쪽이라 메뉴가 위로 열린다. */
function Dropdown({
  value,
  options,
  onChange,
  mono,
  align = 'left',
  disabled,
  title,
  placeholder
}: Props): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const current = options.find((o) => o.value === value)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent): void => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  return (
    <div className={`dd ${disabled ? 'dd--disabled' : ''}`} ref={rootRef}>
      <button
        type="button"
        className="dd__chip"
        data-tip={title}
        aria-label={title}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => !disabled && setOpen((v) => !v)}
      >
        <span className={mono ? 'mono' : ''}>{current ? current.label : (placeholder ?? '선택')}</span>
        <ChevronDownIcon size={12} />
      </button>
      {open && (
        <div className={`dd__menu dd__menu--${align}`} role="listbox">
          {options.length === 0 ? (
            <div className="dd__empty">항목 없음</div>
          ) : (
            options.map((o) => (
              <button
                key={o.value}
                type="button"
                role="option"
                aria-selected={o.value === value}
                className={`dd__opt ${o.value === value ? 'dd__opt--on' : ''}`}
                onClick={() => {
                  onChange(o.value)
                  setOpen(false)
                }}
              >
                <span className={mono ? 'mono' : ''}>{o.label}</span>
                {o.hint && <em className="dd__hint">{o.hint}</em>}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  )
}

export default Dropdown
