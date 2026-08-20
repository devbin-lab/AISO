import { app, BrowserWindow, dialog } from 'electron'
import { cp, lstat, mkdir, readdir, rm, stat, writeFile } from 'fs/promises'
import {
  looksLikeAttachmentId,
  unreferencedAttachmentIds,
  type AttachmentDirEntry
} from './attachment-gc'
import { basename, extname, join, resolve } from 'path'
import { randomUUID } from 'crypto'
import type { AttachmentRef } from '../shared/attachments'

/**
 * 폴더 첨부에서 복사하지 않을 디렉터리 이름. python/attachments.py 의 _SKIP_PARTS 와
 * 정책을 맞추되, 읽기 측에 없는 .aiso(작업 폴더 RAG 색인)도 포함한다 — 색인 산출물은
 * 원본보다 클 수 있고 첨부로서 아무 의미가 없다.
 */
const SKIP_COPY_PARTS = new Set(['.git', '.venv', 'venv', 'node_modules', '__pycache__', '.aiso'])

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
      await cp(source, destination, {
        recursive: stat.isDirectory(),
        force: false,
        errorOnExist: true,
        dereference: false,
        // 읽기 측(python/attachments.py의 _SKIP_PARTS)이 어차피 건너뛰는 디렉터리를
        // 복사부터 하지 않는다. 실측에서 폴더 첨부가 소스의 .aiso/rag/vectors.npy 같은
        // 색인 산출물까지 통째로 복사해 저장량이 원본보다 크게 부풀었다.
        // .aiso 는 읽기 측 목록에도 없어 여기서 함께 막는다.
        filter: (from) => !SKIP_COPY_PARTS.has(basename(from).toLowerCase())
      })
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


/**
 * 참조되지 않은 첨부 폴더를 정리한다. 실패해도 조용히 넘어간다 — 유지보수 작업이
 * 앱 동작을 막으면 안 된다.
 *
 * 왜 참조 카운팅 스윕인가: 고아가 되는 경로가 대화 삭제만이 아니다. 첨부 칩을 ×로
 * 지우거나 첨부만 하고 전송하지 않아도 그 폴더는 어디에서도 참조되지 않는다
 * (제거 IPC 자체가 없다). 실측된 고아 23MB가 전부 그 부류였다.
 *
 * live 를 넘기지 못하는 상황(대화 DB 읽기 실패)에서는 호출부가 예외를 받고 스윕을
 * 건너뛰어야 한다. 빈 집합으로 부르면 저장소를 통째로 지운다.
 */
export async function sweepUnreferencedAttachments(live: ReadonlySet<string>): Promise<number> {
  const root = attachmentRoot()
  let names: string[]
  try {
    names = await readdir(root)
  } catch {
    return 0 // 저장소가 아직 없다
  }
  const entries: AttachmentDirEntry[] = []
  for (const name of names) {
    if (!looksLikeAttachmentId(name)) continue
    try {
      const info = await stat(join(root, name))
      if (info.isDirectory()) entries.push({ id: name, modifiedAtMs: info.mtimeMs })
    } catch {
      /* 사라졌거나 읽을 수 없음 — 건너뛴다 */
    }
  }
  const removable = unreferencedAttachmentIds({ entries, live, nowMs: Date.now() })
  let removed = 0
  for (const id of removable) {
    try {
      await rm(join(root, id), { recursive: true, force: true })
      removed += 1
    } catch {
      /* 사용 중이거나 권한 없음 — 다음 기회에 */
    }
  }
  return removed
}

/**
 * 첨부 저장소를 통째로 비운다. 공장초기화 전용 — "처음 설치 상태로 돌아갑니다"라는
 * 약속을 지키려면 유예 기간을 적용하지 않는다.
 */
export async function clearAttachmentStore(): Promise<void> {
  await rm(attachmentRoot(), { recursive: true, force: true }).catch(() => undefined)
}
