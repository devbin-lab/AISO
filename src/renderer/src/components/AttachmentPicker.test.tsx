import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AttachmentDropEvent, AttachmentRef } from '../../../shared/attachments'
import AttachmentPicker from './AttachmentPicker'

const file: AttachmentRef = {
  id: '123e4567-e89b-42d3-a456-426614174000',
  name: 'brief.pdf',
  kind: 'file',
  fileCount: 1,
  size: 100,
  mediaType: 'application/pdf'
}

describe('AttachmentPicker', () => {
  let dropListener: ((event: AttachmentDropEvent) => void) | null
  let pickFiles: ReturnType<typeof vi.fn>
  let pickFolder: ReturnType<typeof vi.fn>

  beforeEach(() => {
    dropListener = null
    pickFiles = vi.fn().mockResolvedValue([file])
    pickFolder = vi.fn().mockResolvedValue([])
    Object.defineProperty(window, 'api', {
      configurable: true,
      value: {
        attachments: {
          pickFiles,
          pickFolder,
          onDrop: (listener: (event: AttachmentDropEvent) => void) => {
            dropListener = listener
            return () => { dropListener = null }
          }
        }
      }
    })
  })

  it('offers separate native file and folder choices', async () => {
    const onChange = vi.fn()
    render(<AttachmentPicker value={[]} onChange={onChange} />)

    fireEvent.click(screen.getByRole('button', { name: '파일 또는 폴더 첨부' }))
    fireEvent.click(screen.getByRole('menuitem', { name: '파일 첨부' }))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith([file]))
    expect(pickFiles).toHaveBeenCalledTimes(1)
    expect(pickFolder).not.toHaveBeenCalled()
  })

  it('accepts the staged result emitted by the preload drop bridge', () => {
    const onChange = vi.fn()
    const { container } = render(<AttachmentPicker value={[]} onChange={onChange} />)
    const targetId = container.querySelector<HTMLElement>('[data-aiso-attachment-drop-target]')
      ?.dataset.aisoAttachmentDropTarget
    expect(targetId).toBeTruthy()

    act(() => dropListener?.({ targetId: targetId!, status: 'done', attachments: [file] }))

    expect(onChange).toHaveBeenCalledWith([file])
  })
})
