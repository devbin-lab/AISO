import { useEffect, useId, useState } from 'react'
import type { AttachmentRef } from '../../../shared/attachments'

interface Props {
  value: AttachmentRef[]
  disabled?: boolean
  onChange: (attachments: AttachmentRef[]) => void
}

function uniqueAttachments(items: AttachmentRef[]): AttachmentRef[] {
  return [...new Map(items.map((item) => [item.id, item])).values()]
}

export default function AttachmentPicker({ value, disabled = false, onChange }: Props): React.JSX.Element {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const dropTargetId = useId()

  const add = async (task: () => Promise<AttachmentRef[]>): Promise<void> => {
    if (disabled || busy) return
    setBusy(true)
    setError(null)
    try {
      onChange(uniqueAttachments([...value, ...(await task())]))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '첨부를 추가하지 못했습니다.')
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    const subscribe = window.api.attachments?.onDrop
    if (!subscribe) return undefined
    return subscribe((event) => {
      if (event.targetId !== dropTargetId) return
      if (event.status === 'start') {
        setBusy(true)
        setError(null)
        return
      }
      setBusy(false)
      if (event.status === 'error') {
        setError(event.error)
        return
      }
      onChange(uniqueAttachments([...value, ...event.attachments]))
    })
  }, [dropTargetId, onChange, value])

  const pick = (kind: 'file' | 'folder'): void => {
    setMenuOpen(false)
    void add(kind === 'file' ? window.api.attachments.pickFiles : window.api.attachments.pickFolder)
  }

  return (
    <div className="attachment-picker" data-aiso-attachment-drop-target={dropTargetId}>
      <button
        type="button"
        className="attachment-picker__add"
        aria-label="파일 또는 폴더 첨부"
        title="파일 또는 폴더 첨부"
        disabled={disabled || busy}
        onClick={() => setMenuOpen((current) => !current)}
      >
        {busy ? '…' : '+'}
      </button>
      {menuOpen && !disabled && !busy && (
        <div className="attachment-picker__menu" role="menu">
          <button type="button" role="menuitem" onClick={() => pick('file')}>파일 첨부</button>
          <button type="button" role="menuitem" onClick={() => pick('folder')}>폴더 첨부</button>
          <span>입력창에 끌어 놓아도 됩니다.</span>
        </div>
      )}
      {value.length > 0 && (
        <div className="attachment-picker__items" aria-label="첨부한 자료">
          {value.map((item) => (
            <span className="attachment-picker__chip" key={item.id} title={`${item.kind === 'folder' ? '폴더' : '파일'} · ${item.name}`}>
              {item.kind === 'folder' ? '폴더' : '파일'} · {item.name}
              <button
                type="button"
                aria-label={`${item.name} 첨부 제거`}
                disabled={disabled || busy}
                onClick={() => onChange(value.filter((candidate) => candidate.id !== item.id))}
              >×</button>
            </span>
          ))}
        </div>
      )}
      {error && <span className="attachment-picker__error" role="status">{error}</span>}
    </div>
  )
}
