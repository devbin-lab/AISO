import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

export interface ConfirmOptions {
  title?: string
  message: string
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
}

interface Pending extends ConfirmOptions {
  resolve: (ok: boolean) => void
}

let openFn: ((p: Pending) => void) | null = null

/** 커스텀 확인 대화상자 — 네이티브 window.confirm 대체.
 *  사용: `if (await confirmDialog({ message: '삭제할까요?', danger: true })) { ... }` */
export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    if (openFn) openFn({ ...opts, resolve })
    else resolve(false) // 호스트 미마운트 시 안전하게 취소 처리
  })
}

/** 확인 대화상자 호스트 — 앱 루트에 한 번만 마운트. */
export function ConfirmHost(): React.JSX.Element | null {
  const [pending, setPending] = useState<Pending | null>(null)
  const okRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    openFn = setPending
    return () => {
      openFn = null
    }
  }, [])

  const close = (ok: boolean): void => {
    pending?.resolve(ok)
    setPending(null)
  }

  useEffect(() => {
    if (!pending) return
    okRef.current?.focus()
    // Escape만 전역 처리. Enter는 포커스된 버튼이 스스로 처리(확인 버튼 자동 포커스라
    // 기본 Enter=확인, Cancel로 Tab 후 Enter=취소) — 포커스와 무관하게 확인되던 버그 방지.
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.preventDefault()
        close(false)
      }
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [pending])

  if (!pending) return null
  return createPortal(
    <div className="confirm" onMouseDown={() => close(false)}>
      <div
        className="confirm__card"
        role="alertdialog"
        aria-modal="true"
        onMouseDown={(e) => e.stopPropagation()}
      >
        {pending.title && <div className="confirm__title">{pending.title}</div>}
        <div className="confirm__msg">{pending.message}</div>
        <div className="confirm__actions">
          <button className="confirm__btn confirm__btn--cancel" onClick={() => close(false)}>
            {pending.cancelLabel ?? '취소'}
          </button>
          <button
            ref={okRef}
            className={`confirm__btn ${pending.danger ? 'confirm__btn--danger' : 'confirm__btn--ok'}`}
            onClick={() => close(true)}
          >
            {pending.confirmLabel ?? '확인'}
          </button>
        </div>
      </div>
    </div>,
    document.body
  )
}
