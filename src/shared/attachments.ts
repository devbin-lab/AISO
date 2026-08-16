export type AttachmentKind = 'file' | 'folder'

/**
 * A renderer-safe handle to a file explicitly selected by the user.
 * The absolute source path never crosses the preload boundary; Electron copies
 * it into the app-managed attachment store and the backend resolves by id.
 */
export interface AttachmentRef {
  id: string
  name: string
  kind: AttachmentKind
  fileCount: number
  size: number
  mediaType: string | null
}

export type AttachmentDropEvent =
  | { targetId: string; status: 'start' }
  | { targetId: string; status: 'done'; attachments: AttachmentRef[] }
  | { targetId: string; status: 'error'; error: string }
