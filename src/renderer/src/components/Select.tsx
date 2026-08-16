import { useEffect, useRef, useState } from 'react'
import { ChevronDownIcon } from './icons'

interface Props {
  value: string
  options: string[]
  onChange: (v: string) => void
  placeholder?: string
  disabled?: boolean
  ariaLabel?: string
  /** 옵션·트리거 왼쪽 상태 점: 'on'(초록)·'off'(빨강)·null(없음). 예: 모델 설치 여부. */
  status?: (value: string) => 'on' | 'off' | null
}

/** Ollama 모델 등 목록에서 고르는 커스텀 드롭다운 (키보드 탐색 지원). */
function Select({ value, options, onChange, placeholder, disabled, ariaLabel, status }: Props): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent): void => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const openMenu = (): void => {
    if (disabled) return
    const idx = options.indexOf(value)
    setHighlight(idx >= 0 ? idx : 0)
    setOpen(true)
  }

  const choose = (v: string): void => {
    onChange(v)
    setOpen(false)
  }

  const onKeyDown = (e: React.KeyboardEvent): void => {
    if (disabled) return
    if (!open) {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
        e.preventDefault()
        openMenu()
      }
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      setOpen(false)
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlight((h) => Math.min(options.length - 1, h + 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlight((h) => Math.max(0, h - 1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const v = options[highlight]
      if (v != null) choose(v)
    }
  }

  return (
    <div className={`select ${disabled ? 'select--disabled' : ''}`} ref={rootRef}>
      <button
        type="button"
        className="select__trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => (open ? setOpen(false) : openMenu())}
        onKeyDown={onKeyDown}
      >
        <span className={value ? 'mono' : 'select__placeholder'}>
          {status && value && status(value) && (
            <span className={`select__dot select__dot--${status(value)}`} />
          )}
          {value || placeholder || '선택'}
        </span>
        <ChevronDownIcon />
      </button>

      {open && (
        <div className="select__menu" role="listbox">
          {options.length === 0 ? (
            <div className="select__empty">항목 없음</div>
          ) : (
            options.map((opt, i) => {
              const dot = status ? status(opt) : null
              return (
                <button
                  key={opt}
                  type="button"
                  role="option"
                  aria-selected={opt === value}
                  className={`select__opt mono ${opt === value ? 'select__opt--on' : ''} ${
                    i === highlight ? 'select__opt--hi' : ''
                  }`}
                  onMouseEnter={() => setHighlight(i)}
                  onClick={() => choose(opt)}
                >
                  {dot && <span className={`select__dot select__dot--${dot}`} />}
                  {opt}
                </button>
              )
            })
          )}
        </div>
      )}
    </div>
  )
}

export default Select
