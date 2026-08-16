import { app, BrowserWindow, dialog } from 'electron'
import { cp, lstat, mkdir, readdir, rm, writeFile } from 'fs/promises'
import { basename, extname, join, resolve } from 'path'
import { randomUUID } from 'crypto'
import type { AttachmentRef } from '../shared/attachments'

const MAX_FILES = 1_000
const MAX_TOTAL_BYTES = 250 * 1024 * 1024

const MEDIA_TYPES: Record<string, string> = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif',
  '.pdf': 'application/pdf', '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.pptm': 'application/vnd.ms-powerpoint.presentation.macroEnabled.12',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.txt': 'text/plain', '.md': 'text/markdown'
}

interface SourceStats {
  files: number
  bytes: number
}

function attachmentRoot(): string {
  return join(app.getPath('userData'), 'attachments')
}

export function attachmentStorePath(): string {
  return attachmentRoot()
}

function mediaType(path: string): string | null {
  return MEDIA_TYPES[extname(path).toLowerCase()] ?? null
}

async function inspectSource(path: string, total: SourceStats): Promise<void> {
  const stat = await lstat(path)
  if (stat.isSymbolicLink()) throw new Error(`바로가기·심볼릭 링크는 첨부할 수 없습니다: ${basename(path)}`)
  if (stat.isFile()) {
    total.files += 1
    total.bytes += stat.size
  } else if (stat.isDirectory()) {
    for (const entry of await readdir(path, { withFileTypes: true })) {
      await inspectSource(join(path, entry.name), total)
    }
  } else {
    throw new Error(`지원하지 않는 첨부 항목입니다: ${basename(path)}`)
  }
  if (total.files > MAX_FILES) throw new Error(`첨부는 최대 ${MAX_FILES.toLocaleString()}개 파일까지 가능합니다.`)
  if (total.bytes > MAX_TOTAL_BYTES) throw new Error('첨부 전체 크기는 250 MB를 초과할 수 없습니다.')
}

async function stagePaths(paths: string[]): Promise<AttachmentRef[]> {
  const selected = [...new Set(paths.map((path) => resolve(path)))].filter(Boolean)
  if (selected.length === 0) return []
  const total: SourceStats = { files: 0, bytes: 0 }
  const sourceStats = new Map<string, SourceStats>()
  for (const path of selected) {
    const stats: SourceStats = { files: 0, bytes: 0 }
    await inspectSource(path, stats)
    total.files += stats.files
    total.bytes += stats.bytes
    if (total.files > MAX_FILES) throw new Error(`첨부는 최대 ${MAX_FILES.toLocaleString()}개 파일까지 가능합니다.`)
    if (total.bytes > MAX_TOTAL_BYTES) throw new Error('첨부 전체 크기는 250 MB를 초과할 수 없습니다.')
    sourceStats.set(path, stats)
  }

  await mkdir(attachmentRoot(), { recursive: true })
  const staged: AttachmentRef[] = []
  for (const source of selected) {
    const stat = await lstat(source)
    const id = randomUUID()
    const directory = join(attachmentRoot(), id)
    const name = basename(source)
    try {
      await mkdir(directory, { recursive: false })
      const destination = join(directory, name)
      await cp(source, destination, { recursive: stat.isDirectory(), force: false, errorOnExist: true, dereference: false })
      const item: AttachmentRef = {
        id,
        name,
        kind: stat.isDirectory() ? 'folder' : 'file',
        fileCount: sourceStats.get(source)?.files ?? 1,
        size: sourceStats.get(source)?.bytes ?? stat.size,
        mediaType: stat.isFile() ? mediaType(source) : null
      }
      await writeFile(join(directory, 'manifest.json'), JSON.stringify(item), 'utf8')
      staged.push(item)
    } catch (error) {
      await rm(directory, { recursive: true, force: true }).catch(() => undefined)
      throw error
    }
  }
  return staged
}

export async function pickAttachmentFiles(win: BrowserWindow): Promise<AttachmentRef[]> {
  const result = await dialog.showOpenDialog(win, {
    title: '파일 첨부',
    properties: ['openFile', 'multiSelections'],
    filters: [
      {
        name: '지원 문서와 이미지',
        extensions: [
          'pdf', 'pptx', 'pptm', 'docx', 'xlsx', 'hwp', 'hwpx',
          'txt', 'md', 'csv', 'json', 'png', 'jpg', 'jpeg', 'webp', 'gif'
        ]
      },
      { name: '모든 파일', extensions: ['*'] }
    ]
  })
  return result.canceled ? [] : stagePaths(result.filePaths)
}

export async function pickAttachmentFolder(win: BrowserWindow): Promise<AttachmentRef[]> {
  const result = await dialog.showOpenDialog(win, {
    title: '폴더 첨부',
    properties: ['openDirectory']
  })
  return result.canceled ? [] : stagePaths(result.filePaths)
}

export async function importDroppedAttachments(paths: unknown): Promise<AttachmentRef[]> {
  if (!Array.isArray(paths) || paths.length === 0 || paths.length > 32 || paths.some((path) => typeof path !== 'string' || !path.trim())) {
    throw new Error('드래그한 첨부 경로가 올바르지 않습니다.')
  }
  return stagePaths(paths)
}
