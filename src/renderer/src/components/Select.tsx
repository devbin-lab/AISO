import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDownIcon, SearchIcon } from './icons'

interface Props {
  value: string
  options: string[]
  onChange: (v: string) => void
  placeholder?: string
  disabled?: boolean
  ariaLabel?: string
  /** 옵션·트리거 왼쪽 상태 점: 'on'(초록)·'off'(빨강)·null(없음). 예: 모델 설치 여부. */
  status?: (value: string) => 'on' | 'off' | null
  /**
   * 검색칸을 띄우는 최소 항목 수.
   *
   * NVIDIA 모델 목록은 수백 개라 손으로 굴려 찾는 것이 사실상 불가능했다. 반대로
   * 서너 개짜리 목록에 검색칸이 뜨면 클릭이 한 번 늘 뿐이라, 길 때만 켠다.
   */
  searchThreshold?: number
}

const DEFAULT_SEARCH_THRESHOLD = 8

/**
 * 검색어를 항목에 맞춰 본다.
 *
 * 모델 ID 는 `meta/llama-3.3-70b-instruct` 처럼 제공자와 이름이 붙어 있어서, 사람이
 * 기억하는 조각("llama 70b")은 순서는 맞지만 사이가 떨어져 있다. 그래서 공백으로
 * 끊은 조각이 **모두** 들어 있으면 맞는 것으로 본다 — 붙여 친 "llama3.3" 같은 것도
 * 한 조각이라 그대로 부분 문자열로 걸린다.
 */
export function matchesQuery(option: string, query: string): boolean {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean)
  if (terms.length === 0) return true
  const haystack = option.toLowerCase()
  return terms.every((term) => haystack.includes(term))
}

/** Ollama·NVIDIA 모델 등 목록에서 고르는 커스텀 드롭다운 (키보드 탐색·검색 지원). */
function Select({
  value,
  options,
  onChange,
  placeholder,
  disabled,
  ariaLabel,
  status,
  searchThreshold = DEFAULT_SEARCH_THRESHOLD
}: Props): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  const [query, setQuery] = useState('')
  const rootRef = useRef<HTMLDivElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const searchable = options.length >= searchThreshold
  const visible = useMemo(
    () => (searchable && query.trim() ? options.filter((option) => matchesQuery(option, query)) : options),
    [options, query, searchable]
  )

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent): void => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  // 검색칸이 있으면 열자마자 타이핑할 수 있어야 한다. 한 번 더 클릭하게 만들면
  // 검색을 넣은 이유가 사라진다.
  useEffect(() => {
    if (open && searchable) searchRef.current?.focus()
  }, [open, searchable])

  // 화살표로 옮긴 항목이 화면 밖이면 따라 스크롤한다. 목록이 길 때 키보드 탐색이
  // 보이지 않는 곳에서 움직이는 문제를 없앤다.
  useEffect(() => {
    if (!open) return
    const list = listRef.current
    const active = list?.querySelector<HTMLElement>('[data-highlighted="true"]')
    // jsdom 등 scrollIntoView 가 없는 환경이 있다. 없다고 고르기가 막히면 안 된다.
    active?.scrollIntoView?.({ block: 'nearest' })
  }, [open, highlight, visible])

  const openMenu = (): void => {
    if (disabled) return
    const idx = options.indexOf(value)
    setHighlight(idx >= 0 ? idx : 0)
    setQuery('')
    setOpen(true)
  }

  const closeMenu = (): void => {
    setOpen(false)
    setQuery('')
  }

  const choose = (v: string): void => {
    onChange(v)
    closeMenu()
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
      closeMenu()
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlight((h) => Math.min(visible.length - 1, h + 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlight((h) => Math.max(0, h - 1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const v = visible[highlight]
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
        onClick={() => (open ? closeMenu() : openMenu())}
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
        <div className="select__menu">
          {searchable && (
            <div className="select__search">
              <SearchIcon size={13} />
              <input
                ref={searchRef}
                className="select__search-input"
                type="text"
                value={query}
                // 검색어를 고치면 목록이 통째로 바뀐다. 그때 예전 자리를 그대로 두면
                // 엉뚱한 항목이 켜져 있으므로 맨 위로 되돌린다.
                onChange={(e) => {
                  setQuery(e.target.value)
                  setHighlight(0)
                }}
                onKeyDown={onKeyDown}
                placeholder={`${options.length}개 중 검색`}
                aria-label="목록 검색"
                spellCheck={false}
                autoComplete="off"
              />
            </div>
          )}
          <div className="select__list" role="listbox" ref={listRef}>
            {visible.length === 0 ? (
              <div className="select__empty">
                {options.length === 0 ? '항목 없음' : '검색 결과 없음'}
              </div>
            ) : (
              visible.map((opt, i) => {
                const dot = status ? status(opt) : null
                return (
                  <button
                    key={opt}
                    type="button"
                    role="option"
                    aria-selected={opt === value}
                    data-highlighted={i === highlight}
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
        </div>
      )}
    </div>
  )
}

export default Select
