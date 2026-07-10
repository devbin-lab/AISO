import { useState } from 'react'
import type { ConversationMeta } from '../../../shared/conversation'
import { relTime } from '../lib/conversations'
import { confirmDialog } from './ConfirmDialog'
import ContextMenu, { type MenuItem } from './ContextMenu'
import { TrashIcon, EditIcon, PinIcon } from './icons'

interface Props {
  items: ConversationMeta[]
  activeId: string | null
  onSelect: (id: string) => void
  onNew: () => void
  onRename: (id: string, title: string) => void
  onPin: (id: string, pinned: boolean) => void
  onDelete: (id: string) => void
}

/** 왼쪽 대화 목록 — 채팅·에이전트 뷰 공용. 접힘 토글은 타이틀바에서 관리한다. */
function ConversationList({
  items,
  activeId,
  onSelect,
  onNew,
  onRename,
  onPin,
  onDelete
}: Props): React.JSX.Element {
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [menu, setMenu] = useState<{ c: ConversationMeta; x: number; y: number } | null>(null)

  const startRename = (c: ConversationMeta): void => {
    setEditingId(c.id)
    setDraft(c.title)
  }
  const commitRename = (): void => {
    if (editingId) {
      const t = draft.trim()
      if (t) onRename(editingId, t)
    }
    setEditingId(null)
  }
  const deleteConfirm = (c: ConversationMeta): void => {
    void (async () => {
      const ok = await confirmDialog({
        title: '대화 삭제',
        message: `'${c.title}' 대화를 삭제할까요?`,
        confirmLabel: '삭제',
        danger: true
      })
      if (ok) onDelete(c.id)
    })()
  }
  const menuItems = (c: ConversationMeta): MenuItem[] => [
    { label: '이름 변경', onClick: () => startRename(c) },
    { label: c.pinned ? '고정 해제' : '고정', onClick: () => onPin(c.id, !c.pinned) },
    { label: '삭제', danger: true, onClick: () => deleteConfirm(c) }
  ]

  const pinned = items.filter((c) => c.pinned)
  const unpinned = items.filter((c) => !c.pinned)

  const renderRow = (c: ConversationMeta): React.JSX.Element => (
    <div
      key={c.id}
      className={`convrow ${c.id === activeId ? 'convrow--active' : ''}`}
      onClick={() => c.id !== editingId && onSelect(c.id)}
      onContextMenu={(e) => {
        e.preventDefault()
        if (editingId === c.id) return
        setMenu({ c, x: e.clientX, y: e.clientY })
      }}
    >
      {editingId === c.id ? (
        <input
          className="convrow__edit"
          value={draft}
          autoFocus
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commitRename}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commitRename()
            else if (e.key === 'Escape') setEditingId(null)
          }}
          onClick={(e) => e.stopPropagation()}
        />
      ) : (
        <div className="convrow__main" onDoubleClick={() => startRename(c)}>
          <div className="convrow__title" data-tip={c.title}>
            {c.pinned && (
              <span className="convrow__pinmark" aria-hidden>
                <PinIcon size={11} filled />
              </span>
            )}
            {c.title}
          </div>
          <div className="convrow__time">{relTime(c.updatedAt)}</div>
        </div>
      )}
      {editingId !== c.id && (
        <div className="convrow__acts">
          <button
            className={`convrow__act ${c.pinned ? 'convrow__act--on' : ''}`}
            data-tip={c.pinned ? '고정 해제' : '고정'}
            aria-label={c.pinned ? '고정 해제' : '고정'}
            onClick={(e) => {
              e.stopPropagation()
              onPin(c.id, !c.pinned)
            }}
          >
            <PinIcon size={13} filled={c.pinned} />
          </button>
          <button
            className="convrow__act"
            data-tip="이름 변경"
            aria-label="이름 변경"
            onClick={(e) => {
              e.stopPropagation()
              startRename(c)
            }}
          >
            <EditIcon size={13} />
          </button>
          <button
            className="convrow__act convrow__act--del"
            data-tip="삭제"
            aria-label="대화 삭제"
            onClick={(e) => {
              e.stopPropagation()
              deleteConfirm(c)
            }}
          >
            <TrashIcon size={13} />
          </button>
        </div>
      )}
    </div>
  )

  return (
    <div className="convlist">
      <div className="convlist__head">
        <button className="convlist__new" onClick={onNew}>
          ＋ 새 대화
        </button>
      </div>
      <div className="convlist__scroll">
        {items.length === 0 ? (
          <div className="convlist__empty">아직 대화가 없습니다</div>
        ) : (
          <>
            {pinned.map(renderRow)}
            {pinned.length > 0 && unpinned.length > 0 && (
              <div className="convlist__divider" role="separator" />
            )}
            {unpinned.map(renderRow)}
          </>
        )}
      </div>
      {menu && (
        <ContextMenu x={menu.x} y={menu.y} items={menuItems(menu.c)} onClose={() => setMenu(null)} />
      )}
    </div>
  )
}

export default ConversationList
