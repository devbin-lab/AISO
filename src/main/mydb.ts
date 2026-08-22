/**
 * Standalone, user-owned My DB library.
 *
 * This module intentionally has no dependency on the Python sidecar, agent
 * execution, conversations, RAG, or the attachment staging area.  Electron's
 * main process owns the SQLite connection and performs all filesystem writes;
 * renderers receive only stable IDs and library-relative paths.
 */

import { createHash, randomUUID } from 'crypto'
import { createReadStream, createWriteStream, existsSync, mkdirSync, readdirSync, renameSync, rmdirSync, unwatchFile, watchFile } from 'fs'
import { copyFile, lstat, mkdir, readFile, readdir, rename, rm, stat } from 'fs/promises'
import { DatabaseSync } from 'node:sqlite'
import { basename, dirname, extname, isAbsolute, join, relative, resolve, sep } from 'path'
import { Transform } from 'stream'
import { pipeline } from 'stream/promises'
import type {
  MyDbCoreExportResult,
  MyDbDailyReport,
  MyDbDeleteOptions,
  MyDbEdge,
  MyDbFileType,
  MyDbHistoryAction,
  MyDbHistoryEntry,
  MyDbHistorySnapshot,
  MyDbFileHistory,
  MyDbGraphCheckpoint,
  MyDbImportResult,
  MyDbNode,
  MyDbNodeKind,
  MyDbRelation,
  MyDbRevision,
  MyDbRevisionReason,
  MyDbSnapshot,
  MyDbTextDiff,
  MyDbTrashPurgeResult,
  MyDbTrashSnapshot
} from '../shared/mydb.ts'

const LIBRARY_DATABASE_FILE = 'library.sqlite3'
const LIBRARY_FILES_DIR = 'files'
const LIBRARY_REVISIONS_DIR = 'revisions'
const LIBRARY_UNSORTED_DIR = '미분류'
const LIBRARY_TRASH_DIR = '휴지통'
const HISTORY_LIMIT = 250
const TEXT_DIFF_MAX_BYTES = 512 * 1024
const TEXT_DIFF_MAX_LINES = 1_200
const WATCH_DEBOUNCE_MS = 900
const MAX_TITLE_LENGTH = 160
const MAX_FILE_NAME_LENGTH = 180
// 그래프 시점(체크포인트) 보존 한도. 복원 버튼은 히스토리 목록(HISTORY_LIMIT)에서만
// 노출되므로 그 밖의 시점은 UI에서 도달할 수 없다 — 잘라도 사용자가 잃는 기능이 없다.
// 실측: 노드 1000개 라이브러리에서 구조 변경 100회에 48MB가 늘고 총량이 2차로 증가한다.
const GRAPH_CHECKPOINT_LIMIT = 200
// 항목당 리비전 보존 한도. 초기본(sequence=1)은 이 한도와 무관하게 항상 남긴다 —
// "수정되면 어떻게 수정되었는지 기록이 남아서 복구할 수 있으면 좋겠다"는 요구사항의 최소선.
const REVISION_KEEP_PER_ITEM = 30

const LEGACY_CORE_LINK_MIGRATION_KEY = 'legacy_core_links_to_contains_v1'

const ALLOWED_RELATIONS = new Set<MyDbRelation>([
  'contains',
  'related',
  'references',
  'depends_on'
])

const CODE_EXTENSIONS = new Set([
  '.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.cs', '.css', '.go', '.html', '.java', '.js',
  '.jsx', '.json', '.kt', '.lua', '.mdx', '.php', '.py', '.rs', '.scss', '.sh', '.sql', '.swift',
  '.toml', '.ts', '.tsx', '.xml', '.yaml', '.yml'
])
const IMAGE_EXTENSIONS = new Set(['.avif', '.bmp', '.gif', '.heic', '.jpeg', '.jpg', '.png', '.svg', '.tiff', '.webp'])
const ARCHIVE_EXTENSIONS = new Set(['.7z', '.bz2', '.gz', '.rar', '.tar', '.xz', '.zip'])
const TEXT_DIFF_EXTENSIONS = new Set([
  '.c', '.cc', '.cpp', '.cs', '.css', '.csv', '.go', '.h', '.hpp', '.html', '.java', '.js', '.json',
  '.jsx', '.kt', '.lua', '.md', '.markdown', '.mdx', '.php', '.py', '.rs', '.scss', '.sh', '.sql',
  '.svg', '.swift', '.toml', '.ts', '.tsx', '.txt', '.xml', '.yaml', '.yml'
])

interface CoreRow {
  id: string
  title: string
  created_at: string
  updated_at: string
  deleted_at: string | null
}

interface ItemRow {
  id: string
  title: string
  extension: string
  file_type: string
  tags_json: string
  size: number
  relative_path: string
  /** Main-process-only upstream file path for one-way source → My DB sync. */
  source_path: string | null
  created_at: string
  updated_at: string
  deleted_at: string | null
}

interface EdgeRow {
  id: string
  source_id: string
  target_id: string
  relation: string
  created_at: string
  updated_at: string
}

interface HistoryRow {
  id: string
  action: string
  subject_id: string | null
  subject_kind: string | null
  subject_title: string
  related_id: string | null
  related_kind: string | null
  related_title: string | null
  detail: string | null
  graph_checkpoint_id: string | null
  created_at: string
}

interface DailyReportRow {
  report_date: string
  generated_at: string
  total_changes: number
  body: string
}

interface DailyCreatedNodeRow {
  id: string
  kind: MyDbNodeKind
  title: string
  created_at: string
  /** 파일에만 있다. 코어에는 전부 null 이 들어온다. */
  extension: string | null
  file_type: string | null
  size: number | null
  /** 외부 원본에서 가져온 파일이면 그 경로. 직접 만든 파일은 null. */
  source_path: string | null
}

interface RevisionRow {
  id: string
  item_id: string
  sequence: number
  content_hash: string
  size: number
  snapshot_relative_path: string
  reason: string
  created_at: string
}

interface GraphCheckpointRow {
  id: string
  reason: string
  node_count: number
  edge_count: number
  snapshot_json: string
  created_at: string
}

interface GraphSnapshotData {
  version: 1
  cores: CoreRow[]
  items: ItemRow[]
  edges: EdgeRow[]
}

interface NodeRow {
  id: string
  kind: MyDbNodeKind
  title: string
  file_type: string | null
  tags_json: string | null
  size: number | null
  relative_path: string | null
  created_at: string
  updated_at: string
  deleted_at: string | null
}

interface CreatedCore {
  node: MyDbNode
  edge?: MyDbEdge
}

interface CreatedItem {
  node: MyDbNode
  edge?: MyDbEdge
}

interface CreatedEdge {
  edge: MyDbEdge
  created: boolean
}

function now(): string {
  return new Date().toISOString()
}

function normalizeTitle(value: string, fallback: string): string {
  const title = value.trim().replace(/\s+/g, ' ')
  if (!title) throw new Error(`${fallback} 이름을 입력해 주세요.`)
  return title.slice(0, MAX_TITLE_LENGTH)
}

function normalizeRelativePath(value: string): string {
  return value.split(sep).join('/')
}

function isWithin(parent: string, candidate: string): boolean {
  const rel = relative(parent, candidate)
  return rel === '' || (!rel.startsWith(`..${sep}`) && rel !== '..' && !isAbsolute(rel))
}

function assertSafeFileName(value: string, currentExtension = ''): string {
  let name = value.trim()
  if (!name) throw new Error('새 이름을 입력해 주세요.')
  if (name === '.' || name === '..' || name.includes('/') || name.includes('\\')) {
    throw new Error('파일 이름에는 폴더 경로를 넣을 수 없습니다.')
  }
  if (/[<>:"|?*\u0000-\u001f]/.test(name)) {
    throw new Error('파일 이름에 사용할 수 없는 문자가 포함되어 있습니다.')
  }
  if (!extname(name) && currentExtension) name += currentExtension
  return name.slice(0, MAX_FILE_NAME_LENGTH)
}

function exportName(value: string, fallback: string): string {
  const normalized = value.trim().replace(/[<>:"|?*\\/\u0000-\u001f]/g, '_').replace(/[. ]+$/g, '')
  return assertSafeFileName(normalized || fallback)
}

function uniquePath(parent: string, requestedName: string): string {
  const extension = extname(requestedName)
  const stem = extension ? requestedName.slice(0, -extension.length) : requestedName
  let candidate = join(parent, requestedName)
  let suffix = 2
  while (existsSync(candidate)) {
    candidate = join(parent, `${stem} (${suffix})${extension}`)
    suffix += 1
  }
  return candidate
}

function detectFileType(extension: string): MyDbFileType {
  const ext = extension.toLowerCase()
  if (ext === '.md' || ext === '.markdown') return 'markdown'
  if (['.pdf', '.doc', '.docm', '.docx', '.hwp', '.hwpx', '.odt', '.rtf', '.txt'].includes(ext)) return 'document'
  if (['.pot', '.potx', '.pps', '.ppsx', '.ppt', '.pptm', '.pptx'].includes(ext)) return 'slides'
  if (['.csv', '.ods', '.tsv', '.xls', '.xlsm', '.xlsx'].includes(ext)) return 'spreadsheet'
  if (CODE_EXTENSIONS.has(ext)) return 'code'
  if (IMAGE_EXTENSIONS.has(ext)) return 'image'
  if (ARCHIVE_EXTENSIONS.has(ext)) return 'archive'
  return 'other'
}

function parseTags(raw: string | null): string[] {
  try {
    const value = JSON.parse(raw ?? '[]')
    return Array.isArray(value) ? value.filter((tag): tag is string => typeof tag === 'string') : []
  } catch {
    return []
  }
}

function nodeFromRow(row: NodeRow): MyDbNode {
  const file = row.kind === 'file'
  return {
    id: row.id,
    kind: row.kind,
    title: row.title,
    ...(file
      ? {
          fileType: (row.file_type || 'other') as MyDbFileType,
          relativePath: row.relative_path ?? undefined,
          size: Number(row.size ?? 0),
          tags: parseTags(row.tags_json)
        }
      : {}),
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    ...(row.deleted_at ? { deletedAt: row.deleted_at } : {})
  }
}

function edgeFromRow(row: EdgeRow): MyDbEdge {
  return {
    id: row.id,
    sourceId: row.source_id,
    targetId: row.target_id,
    relation: row.relation as MyDbRelation,
    createdAt: row.created_at,
    updatedAt: row.updated_at
  }
}

function historyFromRow(row: HistoryRow): MyDbHistoryEntry {
  return {
    id: row.id,
    action: row.action as MyDbHistoryAction,
    ...(row.subject_id ? { subjectId: row.subject_id } : {}),
    ...(row.subject_kind ? { subjectKind: row.subject_kind as MyDbNodeKind } : {}),
    subjectTitle: row.subject_title,
    ...(row.related_id ? { relatedId: row.related_id } : {}),
    ...(row.related_kind ? { relatedKind: row.related_kind as MyDbNodeKind } : {}),
    ...(row.related_title ? { relatedTitle: row.related_title } : {}),
    ...(row.detail ? { detail: row.detail } : {}),
    ...(row.graph_checkpoint_id ? { graphCheckpointId: row.graph_checkpoint_id } : {}),
    createdAt: row.created_at
  }
}

function dailyReportFromRow(row: DailyReportRow): MyDbDailyReport {
  return {
    reportDate: row.report_date,
    generatedAt: row.generated_at,
    totalChanges: Number(row.total_changes),
    body: row.body
  }
}

function localDateKey(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function previousLocalDay(reference: Date): Date {
  const result = new Date(reference)
  result.setHours(0, 0, 0, 0)
  result.setDate(result.getDate() - 1)
  return result
}

/** 보고서에 쓰는 파일 크기 표기. 소수 한 자리까지만 — 읽으려고 넣은 값이다. */
function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return ''
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = size
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${unit === 0 ? value : Number(value.toFixed(1))}${units[unit]}`
}

/** 변경 이력 줄과 같은 24시간 표기(HH:MM). 파싱 불가한 값은 조용히 비운다. */
function reportTimeOf(iso: string): string {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  return at.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false })
}

function historyActionReportLabel(action: string): string {
  const labels: Record<string, string> = {
    core_created: '코어 생성',
    imported: '자료 추가',
    renamed: '이름 변경',
    moved_to_trash: '휴지통 이동',
    restored: '복원',
    linked: '연결',
    unlinked: '연결 해제',
    content_changed: '파일 변경',
    revision_restored: '파일 버전 복원',
    source_synced: '외부 원본 반영',
    source_linked: '외부 원본 연결',
    graph_restored: '그래프 복원',
    exported: '폴더 내보내기'
  }
  return labels[action] ?? action
}

function revisionFromRow(row: RevisionRow): MyDbRevision {
  return {
    id: row.id,
    itemId: row.item_id,
    sequence: Number(row.sequence),
    contentHash: row.content_hash,
    size: Number(row.size),
    reason: row.reason as MyDbRevisionReason,
    createdAt: row.created_at
  }
}

function splitTextLines(value: string): string[] {
  const normalized = value.replace(/\r\n?/g, '\n')
  const lines = normalized.split('\n')
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop()
  return lines
}

function compactDiffLines<T extends { kind: 'context' | 'added' | 'removed' }>(lines: T[]): { lines: T[]; truncated: boolean } {
  if (lines.length <= TEXT_DIFF_MAX_LINES) return { lines, truncated: false }
  const changed = lines.map((line, index) => line.kind === 'context' ? -1 : index).filter((index) => index >= 0)
  if (changed.length === 0) return { lines: lines.slice(0, TEXT_DIFF_MAX_LINES), truncated: true }
  const visible = new Set<number>()
  for (const index of changed) {
    for (let cursor = Math.max(0, index - 3); cursor <= Math.min(lines.length - 1, index + 3); cursor += 1) visible.add(cursor)
  }
  const compacted = lines.filter((_, index) => visible.has(index))
  if (compacted.length <= TEXT_DIFF_MAX_LINES) return { lines: compacted, truncated: true }
  const head = compacted.slice(0, Math.floor(TEXT_DIFF_MAX_LINES / 2))
  const tail = compacted.slice(-(TEXT_DIFF_MAX_LINES - head.length))
  return { lines: [...head, ...tail], truncated: true }
}

function buildTextDiff(beforeText: string, afterText: string): Pick<MyDbTextDiff, 'addedLines' | 'removedLines' | 'lines' | 'truncated'> {
  const before = splitTextLines(beforeText)
  const after = splitTextLines(afterText)
  if (before.length === after.length && before.every((line, index) => line === after[index])) {
    return { addedLines: 0, removedLines: 0, lines: [], truncated: false }
  }

  // A bounded LCS gives a familiar line-level diff for ordinary source and
  // text files without turning a very large document into an O(n²) memory use.
  if (before.length <= 900 && after.length <= 900) {
    const width = after.length + 1
    const table = new Uint16Array((before.length + 1) * width)
    for (let left = before.length - 1; left >= 0; left -= 1) {
      for (let right = after.length - 1; right >= 0; right -= 1) {
        const index = left * width + right
        table[index] = before[left] === after[right]
          ? table[(left + 1) * width + right + 1] + 1
          : Math.max(table[(left + 1) * width + right], table[left * width + right + 1])
      }
    }
    const lines: MyDbTextDiff['lines'] = []
    let left = 0
    let right = 0
    let addedLines = 0
    let removedLines = 0
    while (left < before.length || right < after.length) {
      if (left < before.length && right < after.length && before[left] === after[right]) {
        lines.push({ kind: 'context', oldLine: left + 1, newLine: right + 1, text: before[left] })
        left += 1
        right += 1
      } else if (right < after.length && (left === before.length || table[left * width + right + 1] >= table[(left + 1) * width + right])) {
        lines.push({ kind: 'added', newLine: right + 1, text: after[right] })
        addedLines += 1
        right += 1
      } else {
        lines.push({ kind: 'removed', oldLine: left + 1, text: before[left] })
        removedLines += 1
        left += 1
      }
    }
    const compacted = compactDiffLines(lines)
    return { addedLines, removedLines, lines: compacted.lines, truncated: compacted.truncated }
  }

  let prefix = 0
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) prefix += 1
  let suffix = 0
  while (
    suffix < before.length - prefix
    && suffix < after.length - prefix
    && before[before.length - suffix - 1] === after[after.length - suffix - 1]
  ) suffix += 1
  const lines: MyDbTextDiff['lines'] = []
  for (let index = Math.max(0, prefix - 3); index < prefix; index += 1) {
    lines.push({ kind: 'context', oldLine: index + 1, newLine: index + 1, text: before[index] })
  }
  const removed = before.slice(prefix, before.length - suffix)
  const added = after.slice(prefix, after.length - suffix)
  for (let index = 0; index < removed.length; index += 1) lines.push({ kind: 'removed', oldLine: prefix + index + 1, text: removed[index] })
  for (let index = 0; index < added.length; index += 1) lines.push({ kind: 'added', newLine: prefix + index + 1, text: added[index] })
  for (let index = suffix; index > 0; index -= 1) {
    const oldLine = before.length - index + 1
    const newLine = after.length - index + 1
    lines.push({ kind: 'context', oldLine, newLine, text: before[oldLine - 1] })
  }
  const compacted = compactDiffLines(lines)
  return { addedLines: added.length, removedLines: removed.length, lines: compacted.lines, truncated: true }
}

/**
 * Make one immutable revision copy while calculating its content hash. A
 * stream avoids holding a large binary file in memory. The size check avoids
 * recording a half-saved file while an external editor is still writing it.
 */
async function copyAndHashFile(source: string, destination: string, expectedSize: number): Promise<{ hash: string; size: number }> {
  const hash = createHash('sha256')
  let size = 0
  const hashing = new Transform({
    transform(chunk: Buffer | string, _encoding, callback): void {
      try {
        const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
        hash.update(buffer)
        size += buffer.byteLength
        callback(null, buffer)
      } catch (error) {
        callback(error as Error)
      }
    }
  })
  await pipeline(createReadStream(source), hashing, createWriteStream(destination, { flags: 'wx' }))
  if (size !== expectedSize) throw new Error('파일이 저장되는 중입니다. 잠시 후 다시 버전을 확인해 주세요.')
  return { hash: hash.digest('hex'), size }
}

/**
 * A private library store.  Construct this only in Electron's main process.
 * Tests may construct one with a temporary root without importing Electron.
 */
export class MyDbStore {
  readonly root: string
  readonly filesRoot: string
  readonly revisionsRoot: string
  private readonly database: DatabaseSync
  private readonly revisionTrackingReady: Promise<void>
  private readonly pendingFileChanges = new Map<string, ReturnType<typeof setTimeout>>()
  private readonly revisionChains = new Map<string, Promise<unknown>>()
  private readonly watchedPaths = new Map<string, string>()
  private readonly watchedSourcePaths = new Map<string, string>()
  private readonly pendingSourceChanges = new Map<string, ReturnType<typeof setTimeout>>()
  private closed = false

  constructor(root: string) {
    const resolvedRoot = resolve(root)
    this.root = resolvedRoot
    // Files remain in one managed container. Core folders begin immediately
    // beneath it: `files/<core>/<child core>/<file>`.
    this.filesRoot = join(resolvedRoot, LIBRARY_FILES_DIR)
    this.revisionsRoot = join(resolvedRoot, LIBRARY_REVISIONS_DIR)
    mkdirSync(this.filesRoot, { recursive: true })
    mkdirSync(this.revisionsRoot, { recursive: true })
    this.database = new DatabaseSync(join(resolvedRoot, LIBRARY_DATABASE_FILE))
    this.database.exec('PRAGMA foreign_keys = ON; PRAGMA journal_mode = WAL;')
    this.initializeSchema()
    this.migrateLegacyCoreLinksToContains()
    this.organizeManagedFilesByCore()
    this.revisionTrackingReady = this.initializeRevisionTracking()
  }

  close(): void {
    this.closed = true
    for (const path of this.watchedPaths.values()) unwatchFile(path)
    this.watchedPaths.clear()
    for (const path of this.watchedSourcePaths.values()) unwatchFile(path)
    this.watchedSourcePaths.clear()
    for (const timer of this.pendingFileChanges.values()) clearTimeout(timer)
    this.pendingFileChanges.clear()
    for (const timer of this.pendingSourceChanges.values()) clearTimeout(timer)
    this.pendingSourceChanges.clear()
    this.database.close()
  }

  snapshot(): MyDbSnapshot {
    const rows = this.database.prepare(
      `SELECT id, 'core' AS kind, title, NULL AS file_type, NULL AS tags_json, NULL AS size,
              NULL AS relative_path, created_at, updated_at, deleted_at
       FROM mydb_cores
       WHERE deleted_at IS NULL
       UNION ALL
       SELECT id, 'file' AS kind, title, file_type, tags_json, size, relative_path,
              created_at, updated_at, deleted_at
       FROM mydb_items
       WHERE deleted_at IS NULL
       ORDER BY updated_at DESC, title COLLATE NOCASE`
    ).all() as unknown as NodeRow[]
    const activeIds = new Set(rows.map((row) => row.id))
    const edges = (this.database.prepare(
      'SELECT id, source_id, target_id, relation, created_at, updated_at FROM mydb_edges ORDER BY updated_at DESC'
    ).all() as unknown as EdgeRow[])
      .filter((edge) => activeIds.has(edge.source_id) && activeIds.has(edge.target_id))
      .map(edgeFromRow)
    return { nodes: rows.map(nodeFromRow), edges }
  }

  trash(): MyDbTrashSnapshot {
    const rows = this.database.prepare(
      `SELECT id, 'core' AS kind, title, NULL AS file_type, NULL AS tags_json, NULL AS size,
              NULL AS relative_path, created_at, updated_at, deleted_at
       FROM mydb_cores
       WHERE deleted_at IS NOT NULL
       UNION ALL
       SELECT id, 'file' AS kind, title, file_type, tags_json, size, relative_path,
              created_at, updated_at, deleted_at
       FROM mydb_items
       WHERE deleted_at IS NOT NULL
       ORDER BY updated_at DESC, title COLLATE NOCASE`
    ).all() as unknown as NodeRow[]
    return { nodes: rows.map(nodeFromRow) }
  }

  history(): MyDbHistorySnapshot {
    const rows = this.database.prepare(
      `SELECT id, action, subject_id, subject_kind, subject_title,
              related_id, related_kind, related_title, detail, graph_checkpoint_id, created_at
       FROM mydb_history
       ORDER BY created_at DESC, id DESC
       LIMIT ?`
    ).all(HISTORY_LIMIT) as unknown as HistoryRow[]
    const reports = this.database.prepare(
      `SELECT report_date, generated_at, total_changes, body
       FROM mydb_daily_reports
       ORDER BY report_date DESC
       LIMIT ?`
    ).all(60) as unknown as DailyReportRow[]
    return { entries: rows.map(historyFromRow), dailyReports: reports.map(dailyReportFromRow) }
  }

  /**
   * Write one report for the preceding local calendar day, but only once.
   * A report is also stored for a no-change day so an open Aiso never retries
   * the same calendar date indefinitely.
   */
  ensurePreviousDayReport(reference = new Date()): MyDbDailyReport | null {
    const reportDay = previousLocalDay(reference)
    const reportDate = localDateKey(reportDay)
    const existing = this.database.prepare(
      'SELECT report_date, generated_at, total_changes, body FROM mydb_daily_reports WHERE report_date = ?'
    ).get(reportDate) as unknown as DailyReportRow | undefined
    if (existing) return null

    const dayEnd = new Date(reportDay)
    dayEnd.setDate(dayEnd.getDate() + 1)
    const rows = this.database.prepare(
      `SELECT id, action, subject_id, subject_kind, subject_title,
              related_id, related_kind, related_title, detail, graph_checkpoint_id, created_at
       FROM mydb_history
       WHERE created_at >= ? AND created_at < ?
       ORDER BY created_at ASC, id ASC`
    ).all(reportDay.toISOString(), dayEnd.toISOString()) as unknown as HistoryRow[]
    // 파일은 무엇이 들어왔는지 알아볼 수 있게 형식·크기·외부 원본까지 함께 읽는다.
    // 코어에는 해당 열이 없으므로 NULL 을 채워 UNION 의 열 수를 맞춘다.
    const createdNodes = this.database.prepare(
      `SELECT id, 'core' AS kind, title, created_at,
              NULL AS extension, NULL AS file_type, NULL AS size, NULL AS source_path
       FROM mydb_cores
       WHERE created_at >= ? AND created_at < ?
       UNION ALL
       SELECT id, 'file' AS kind, title, created_at,
              extension, file_type, size, source_path
       FROM mydb_items
       WHERE created_at >= ? AND created_at < ?
       ORDER BY created_at ASC, title COLLATE NOCASE`
    ).all(
      reportDay.toISOString(), dayEnd.toISOString(),
      reportDay.toISOString(), dayEnd.toISOString()
    ) as unknown as DailyCreatedNodeRow[]
    const generatedAt = now()
    const report: MyDbDailyReport = {
      reportDate,
      generatedAt,
      totalChanges: rows.length,
      body: this.buildDailyReportBody(reportDate, rows, createdNodes)
    }
    this.database.prepare(
      `INSERT INTO mydb_daily_reports (report_date, generated_at, total_changes, body)
       VALUES (?, ?, ?, ?)`
    ).run(report.reportDate, report.generatedAt, report.totalChanges, report.body)
    return report
  }

  restoreGraphCheckpoint(checkpointId: string): MyDbGraphCheckpoint {
    const checkpoint = this.database.prepare(
      `SELECT id, reason, node_count, edge_count, snapshot_json, created_at
       FROM mydb_graph_checkpoints WHERE id = ?`
    ).get(checkpointId) as unknown as GraphCheckpointRow | undefined
    if (!checkpoint) throw new Error('복원할 그래프 시점을 찾을 수 없습니다.')
    const state = this.parseGraphSnapshot(checkpoint.snapshot_json)
    const restoredAt = now()
    // A checkpoint is a snapshot of rows, and restoring re-inserts them.  Two
    // things must never come back:
    //  1) anything the user permanently deleted — the tombstone records that
    //     decision, and it outranks any older snapshot;
    //  2) a file row whose managed copy is gone for any other reason (disk
    //     loss, external deletion).  Re-inserting it would put a node on the
    //     graph that nothing can open.
    // Skipping both keeps the invariant "every live file node has its bytes".
    const purged = this.purgedIds()
    const restorableCores = state.cores.filter((core) => !purged.has(core.id))
    const restorableItems = state.items.filter(
      (item) => !purged.has(item.id) && existsSync(this.resolveLibraryFile(item.relative_path))
    )
    const droppedCount =
      state.cores.length - restorableCores.length + (state.items.length - restorableItems.length)

    this.transaction(() => {
      const knownCoreIds = new Set(restorableCores.map((core) => core.id))
      const knownItemIds = new Set(restorableItems.map((item) => item.id))
      for (const core of restorableCores) {
        this.database.prepare(
          `INSERT INTO mydb_cores (id, title, created_at, updated_at, deleted_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET title = excluded.title, updated_at = excluded.updated_at, deleted_at = excluded.deleted_at`
        ).run(core.id, core.title, core.created_at, core.updated_at, core.deleted_at)
      }
      for (const item of restorableItems) {
        this.database.prepare(
          `INSERT INTO mydb_items
           (id, title, extension, file_type, tags_json, size, relative_path, source_path, created_at, updated_at, deleted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             title = excluded.title, extension = excluded.extension, file_type = excluded.file_type,
             tags_json = excluded.tags_json, size = excluded.size, source_path = excluded.source_path,
             updated_at = excluded.updated_at, deleted_at = excluded.deleted_at`
        ).run(
          item.id, item.title, item.extension, item.file_type, item.tags_json, item.size,
          item.relative_path, item.source_path, item.created_at, item.updated_at, item.deleted_at
        )
      }

      const currentCoreRows = this.database.prepare('SELECT id FROM mydb_cores').all() as Array<{ id: string }>
      const currentItemRows = this.database.prepare('SELECT id FROM mydb_items').all() as Array<{ id: string }>
      for (const row of currentCoreRows) {
        if (!knownCoreIds.has(row.id)) {
          this.database.prepare('UPDATE mydb_cores SET deleted_at = ?, updated_at = ? WHERE id = ?').run(restoredAt, restoredAt, row.id)
        }
      }
      for (const row of currentItemRows) {
        if (!knownItemIds.has(row.id)) {
          this.database.prepare('UPDATE mydb_items SET deleted_at = ?, updated_at = ? WHERE id = ?').run(restoredAt, restoredAt, row.id)
        }
      }

      this.database.prepare('DELETE FROM mydb_edges').run()
      const insertEdge = this.database.prepare(
        `INSERT INTO mydb_edges (id, source_id, target_id, relation, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?)`
      )
      // An edge whose endpoint was not restored would point at nothing.  The
      // graph view resolves edges through the node map, so a dangling edge
      // renders as a line to an invisible node.
      const restoredIds = new Set([...knownCoreIds, ...knownItemIds])
      for (const edge of state.edges) {
        if (!restoredIds.has(edge.source_id) || !restoredIds.has(edge.target_id)) continue
        insertEdge.run(edge.id, edge.source_id, edge.target_id, edge.relation, edge.created_at, edge.updated_at)
      }
      const when = new Date(checkpoint.created_at).toLocaleString('ko-KR')
      this.recordHistory({
        action: 'graph_restored',
        subjectTitle: 'My DB 그래프',
        detail: droppedCount > 0
          ? `${when} 시점으로 복원 · 완전히 삭제됐거나 파일이 없는 ${droppedCount}개는 제외`
          : `${when} 시점으로 복원`
      })
    })
    this.restartFileWatchers()
    return this.graphCheckpointFromRow(checkpoint)
  }

  async fileHistory(itemId: string): Promise<MyDbFileHistory> {
    await this.revisionTrackingReady
    const item = this.requireItem(itemId, false)
    await this.captureChangedRevision(item, 'content_changed')
    const revisions = this.listRevisions(item.id)
    return {
      item: this.requireNode(item.id, false),
      revisions: revisions.map(revisionFromRow)
    }
  }

  async compareRevisions(itemId: string, beforeRevisionId: string, afterRevisionId: string): Promise<MyDbTextDiff> {
    await this.revisionTrackingReady
    const item = this.requireItem(itemId, false)
    const before = this.requireRevision(item.id, beforeRevisionId)
    const after = this.requireRevision(item.id, afterRevisionId)
    const base = {
      itemId: item.id,
      before: revisionFromRow(before),
      after: revisionFromRow(after)
    }
    if (!TEXT_DIFF_EXTENSIONS.has(item.extension.toLowerCase())) {
      return {
        ...base,
        available: false,
        reason: '이 파일 형식은 현재 내용 비교를 지원하지 않습니다. 버전 복원은 가능합니다.',
        addedLines: 0,
        removedLines: 0,
        lines: [],
        truncated: false
      }
    }
    if (before.size > TEXT_DIFF_MAX_BYTES || after.size > TEXT_DIFF_MAX_BYTES) {
      return {
        ...base,
        available: false,
        reason: '512KB를 넘는 텍스트 파일은 버전 보관과 복원만 지원합니다.',
        addedLines: 0,
        removedLines: 0,
        lines: [],
        truncated: false
      }
    }
    const [beforeBuffer, afterBuffer] = await Promise.all([
      readFile(this.resolveRevisionFile(before.snapshot_relative_path)),
      readFile(this.resolveRevisionFile(after.snapshot_relative_path))
    ])
    if (beforeBuffer.includes(0) || afterBuffer.includes(0)) {
      return {
        ...base,
        available: false,
        reason: '바이너리로 판단된 파일은 내용 비교를 지원하지 않습니다. 버전 복원은 가능합니다.',
        addedLines: 0,
        removedLines: 0,
        lines: [],
        truncated: false
      }
    }
    const diff = buildTextDiff(beforeBuffer.toString('utf8'), afterBuffer.toString('utf8'))
    return { ...base, available: true, ...diff }
  }

  async restoreRevision(itemId: string, revisionId: string): Promise<MyDbNode> {
    await this.revisionTrackingReady
    const item = this.requireItem(itemId, false)
    const revision = this.requireRevision(item.id, revisionId)
    await this.captureChangedRevision(item, 'content_changed')

    const source = this.resolveRevisionFile(revision.snapshot_relative_path)
    const destination = this.resolveLibraryFile(item.relative_path)
    await copyFile(source, destination)
    const restored = await this.snapshotCurrentFile(item, 'restored', true)
    if (!restored) throw new Error('복원한 파일의 새 버전을 만들지 못했습니다.')
    const updatedAt = now()
    this.database.prepare('UPDATE mydb_items SET size = ?, updated_at = ? WHERE id = ?').run(restored.size, updatedAt, item.id)
    const node = this.requireNode(item.id, false)
    this.recordHistory({
      action: 'revision_restored',
      subject: node,
      detail: `v${revision.sequence} 버전으로 복원`
    })
    return node
  }

  async setSourcePath(itemId: string, sourcePath: string): Promise<MyDbNode> {
    await this.revisionTrackingReady
    const item = this.requireItem(itemId, false)
    const source = resolve(sourcePath)
    await this.assertSafeImportSource(source)
    const sourceStats = await stat(source)
    if (!sourceStats.isFile()) throw new Error('외부 원본으로는 파일만 연결할 수 있습니다.')
    if (extname(source).toLowerCase() !== item.extension.toLowerCase()) {
      throw new Error('같은 확장자의 파일만 외부 원본으로 연결할 수 있습니다.')
    }
    this.database.prepare('UPDATE mydb_items SET source_path = ?, updated_at = ? WHERE id = ?').run(source, now(), item.id)
    const updated = this.requireNode(item.id, false)
    this.watchSourceFile(this.requireItem(item.id, false))
    this.recordHistory({ action: 'source_linked', subject: updated, detail: '외부 원본 연결' })
    return updated
  }

  createCore(title: string, parentId?: string | null): MyDbNode {
    const created = this.createCoreWithOptionalParent(title, parentId)
    this.recordHistory({ action: 'core_created', subject: created.node })
    return created.node
  }

  async renameNode(id: string, title: string): Promise<MyDbNode> {
    await this.revisionTrackingReady
    const row = this.requireNodeRow(id, false)
    if (row.kind === 'core') {
      const clean = normalizeTitle(title, '코어')
      const previousTitle = row.title
      if (clean === previousTitle) return this.requireNode(id, false)
      const updatedAt = now()
      this.database.prepare('UPDATE mydb_cores SET title = ?, updated_at = ? WHERE id = ?').run(clean, updatedAt, id)
      this.organizeManagedFilesByCore()
      const renamed = this.requireNode(id, false)
      this.recordHistory({ action: 'renamed', subject: renamed, detail: `이전 이름: ${previousTitle}` })
      return renamed
    }

    const item = this.requireItem(id, false)
    const fileName = assertSafeFileName(title, item.extension)
    if (fileName === item.title) return this.requireNode(id, false)
    const currentPath = this.resolveLibraryFile(item.relative_path)
    const targetPath = join(dirname(currentPath), fileName)
    if (existsSync(targetPath)) throw new Error('같은 이름의 파일이 이미 My DB에 있습니다.')

    // Filesystem and SQLite cannot share one atomic transaction.  Rename the
    // copied library file first, then compensate by restoring it if the DB
    // update cannot commit.
    const renamed = await this.renameItemAtomically(item, currentPath, targetPath, fileName)
    this.watchManagedFile(this.requireItem(id, false))
    this.recordHistory({ action: 'renamed', subject: renamed, detail: `이전 이름: ${item.title}` })
    return renamed
  }

  deleteNode(id: string, options: MyDbDeleteOptions = {}): void {
    const node = this.requireNodeRow(id, true)
    if (node.deleted_at) return
    const deletedAt = now()
    this.transaction(() => {
      if (node.kind === 'file') {
        this.database.prepare('UPDATE mydb_items SET deleted_at = ?, updated_at = ? WHERE id = ?').run(deletedAt, deletedAt, id)
        this.recordHistory({ action: 'moved_to_trash', subject: nodeFromRow(node) })
        return
      }
      if (!options.cascade) {
        this.database.prepare('UPDATE mydb_cores SET deleted_at = ?, updated_at = ? WHERE id = ?').run(deletedAt, deletedAt, id)
        this.recordHistory({ action: 'moved_to_trash', subject: nodeFromRow(node) })
        return
      }

      const coreIds = this.collectCoreSubtree(id)
      const itemIds = this.collectOrphanItemsForCores(coreIds)
      this.updateDeleted('mydb_cores', coreIds, deletedAt)
      this.updateDeleted('mydb_items', itemIds, deletedAt)
      const detail = [
        coreIds.size > 1 ? `하위 코어 ${coreIds.size - 1}개` : '',
        itemIds.size > 0 ? `파일 ${itemIds.size}개` : ''
      ].filter(Boolean).join(' · ')
      this.recordHistory({ action: 'moved_to_trash', subject: nodeFromRow(node), detail })
    })
    this.restartFileWatchers()
  }

  restoreNode(id: string): MyDbNode {
    const node = this.requireNodeRow(id, true)
    const updatedAt = now()
    const table = node.kind === 'core' ? 'mydb_cores' : 'mydb_items'
    this.database.prepare(`UPDATE ${table} SET deleted_at = NULL, updated_at = ? WHERE id = ?`).run(updatedAt, id)
    const restored = this.requireNode(id, false)
    if (restored.kind === 'file') {
      const item = this.requireItem(id, false)
      this.watchManagedFile(item)
      this.watchSourceFile(item)
    }
    this.organizeManagedFilesByCore()
    this.recordHistory({ action: 'restored', subject: restored })
    return restored
  }

  /**
   * Permanently remove one trashed node: database rows, the managed library
   * copy, and every revision snapshot.  This is the only path in My DB that
   * destroys data, so it is deliberately narrow.
   *
   * Two-step by design.  Only a node that is already in the trash can be
   * purged, so no single action can destroy something that was live.
   *
   * The reason this was blocked until now is `restoreGraphCheckpoint`: it
   * re-inserts every core and item found in a snapshot, so restoring a
   * checkpoint taken *before* a purge would resurrect a row whose file no
   * longer exists — a node the graph shows but nothing can open.  A tombstone
   * in `mydb_purged` records the decision, and the restore path consults it.
   * Checkpoints stay immutable; the purge is expressed as new information
   * rather than by rewriting history.
   *
   * History rows are kept.  They carry captured titles, not foreign keys, so
   * the audit trail survives the subject's removal — and "기록이 남아서
   * 복구할 수 있으면 좋겠다"는 요구는 기록 자체를 지우지 않는다는 뜻이다.
   */
  async purgeNode(id: string): Promise<void> {
    const node = this.requireNodeRow(id, true)
    if (!node.deleted_at) {
      throw new Error('휴지통에 있는 항목만 완전히 삭제할 수 있습니다.')
    }
    const purgedAt = now()
    // Collect the on-disk paths before the rows disappear.
    const libraryFile = node.kind === 'file' && node.relative_path
      ? this.resolveLibraryFile(node.relative_path)
      : null
    const revisionDirectory = node.kind === 'file' ? join(this.revisionsRoot, id) : null

    // Watchers hold handles on the managed copy; on Windows an open handle
    // makes the unlink fail.  Stop them before touching the filesystem.
    this.stopWatching(id)

    this.transaction(() => {
      this.database.prepare('DELETE FROM mydb_edges WHERE source_id = ? OR target_id = ?').run(id, id)
      if (node.kind === 'file') {
        this.database.prepare('DELETE FROM mydb_revisions WHERE item_id = ?').run(id)
        this.database.prepare('DELETE FROM mydb_items WHERE id = ?').run(id)
      } else {
        this.database.prepare('DELETE FROM mydb_cores WHERE id = ?').run(id)
      }
      this.database.prepare(
        'INSERT OR REPLACE INTO mydb_purged (id, kind, title, purged_at) VALUES (?, ?, ?, ?)'
      ).run(id, node.kind, node.title, purgedAt)
      this.recordHistory({ action: 'purged', subject: nodeFromRow(node) })
    })

    // Filesystem last: a committed transaction with leftover bytes is
    // recoverable noise, but deleted bytes with a rolled-back transaction
    // would leave a live row pointing at nothing.
    if (libraryFile) await rm(libraryFile, { force: true }).catch(() => undefined)
    if (revisionDirectory) {
      await rm(revisionDirectory, { recursive: true, force: true }).catch(() => undefined)
    }
    this.restartFileWatchers()
  }

  /**
   * 휴지통을 비운다. `before` 를 주면 그 시각 **이전에** 버려진 것만 지운다(자동 비우기).
   *
   * 일괄 SQL 로 지우지 않고 purgeNode 를 그대로 돌린다. 완전 삭제는 행 삭제만이
   * 아니라 보관 파일·리비전 폴더 제거, 감시 핸들 정리, tombstone 기록, 이력 남기기가
   * 한 묶음이다. 여기서 따로 구현하면 단건 삭제와 조용히 어긋난다.
   *
   * 한 건이 실패해도 나머지는 계속 지운다 — 중간에 멈추면 사용자는 무엇이 남았는지
   * 알 수 없고, 다시 눌러도 같은 항목에서 다시 멈춘다.
   */
  async purgeTrash(before?: string | null): Promise<MyDbTrashPurgeResult> {
    const rows = this.database.prepare(
      `SELECT id, deleted_at FROM mydb_cores WHERE deleted_at IS NOT NULL
       UNION ALL
       SELECT id, deleted_at FROM mydb_items WHERE deleted_at IS NOT NULL
       ORDER BY deleted_at ASC`
    ).all() as Array<{ id: string; deleted_at: string }>

    const targets = before ? rows.filter((row) => row.deleted_at < before) : rows
    let purged = 0
    let failed = 0
    for (const row of targets) {
      try {
        await this.purgeNode(row.id)
        purged += 1
      } catch {
        // 이미 사라진 행은 실패로 세지 않는다 — 아직 남아 있을 때만 진짜 실패다.
        let stillThere = false
        try {
          this.requireNodeRow(row.id, true)
          stillThere = true
        } catch {
          stillThere = false
        }
        if (stillThere) failed += 1
      }
    }
    return { purged, failed }
  }

  private purgedIds(): Set<string> {
    const rows = this.database.prepare('SELECT id FROM mydb_purged').all() as Array<{ id: string }>
    return new Set(rows.map((row) => row.id))
  }

  link(sourceId: string, targetId: string, relation: MyDbRelation = 'related'): MyDbEdge {
    const linked = this.transaction(() => {
      const source = this.requireNode(sourceId, false)
      const target = this.requireNode(targetId, false)
      const created = this.createEdge(sourceId, targetId, relation)
      if (created.created) {
        this.recordHistory({ action: 'linked', subject: source, related: target, detail: relation })
      }
      return created.edge
    })
    this.organizeManagedFilesByCore()
    return linked
  }

  unlink(edgeId: string): void {
    this.transaction(() => {
      const edge = this.database.prepare(
        'SELECT id, source_id, target_id, relation, created_at, updated_at FROM mydb_edges WHERE id = ?'
      ).get(edgeId) as unknown as EdgeRow | undefined
      if (!edge) return
      const source = this.requireNode(edge.source_id, true)
      const target = this.requireNode(edge.target_id, true)
      this.database.prepare('DELETE FROM mydb_edges WHERE id = ?').run(edgeId)
      this.recordHistory({ action: 'unlinked', subject: source, related: target, detail: edge.relation })
    })
    this.organizeManagedFilesByCore()
  }

  /**
   * Copies explicitly selected files/folders into the library.  A folder is
   * represented as a core tree; each file is linked to its containing core.
   */
  async importPaths(paths: readonly string[], parentCoreId?: string | null): Promise<MyDbImportResult> {
    await this.revisionTrackingReady
    const resolvedPaths: string[] = []
    for (const rawPath of paths) {
      if (typeof rawPath !== 'string' || !rawPath.trim()) {
        throw new Error('가져올 경로가 올바르지 않습니다.')
      }
      resolvedPaths.push(resolve(rawPath))
    }
    const unique = [...new Set(resolvedPaths)].filter(
      (candidate) => !resolvedPaths.some((parent) => parent !== candidate && isWithin(parent, candidate))
    )
    if (unique.length === 0) throw new Error('가져올 파일 또는 폴더를 선택해 주세요.')
    if (parentCoreId) this.requireCore(parentCoreId, false)

    for (const source of unique) await this.assertSafeImportSource(source)

    const result: MyDbImportResult = { createdNodes: [], createdEdges: [], skippedPaths: [] }
    for (const source of unique) {
      const sourceStat = await lstat(source)
      if (sourceStat.isFile()) {
        const created = await this.importFile(source, parentCoreId ?? null)
        result.createdNodes.push(created.node)
        if (created.edge) result.createdEdges.push(created.edge)
      } else if (sourceStat.isDirectory()) {
        await this.importFolder(source, parentCoreId ?? null, result)
      } else {
        result.skippedPaths.push(source)
      }
    }
    if (result.createdNodes.length > 0) {
      const coreCount = result.createdNodes.filter((node) => node.kind === 'core').length
      const fileCount = result.createdNodes.filter((node) => node.kind === 'file').length
      // 개수만 남기면 나중에 "무엇이 들어왔는지" 알 수 없다. 이름을 함께 적되,
      // 대량 가져오기에서 detail 이 무한정 길어지지 않게 앞 5개까지만 남긴다.
      // (전체 목록은 보고서의 [새로 생성된 코어와 파일] 절이 항상 보여 준다.)
      const names = result.createdNodes.filter((node) => node.kind === 'file').map((node) => node.title)
      // 한 개만 들어왔으면 subjectTitle 이 이미 그 이름이다 — 같은 이름을 두 번 적지 않는다.
      const listed = names.length > 1 ? names : []
      const shown = listed.slice(0, 5).join(', ')
      const rest = listed.length > 5 ? ` 외 ${listed.length - 5}개` : ''
      const detail = [
        coreCount > 0 ? `코어 ${coreCount}개` : '',
        fileCount > 0 ? `파일 ${fileCount}개` : '',
        shown ? `${shown}${rest}` : ''
      ].filter(Boolean).join(' · ')
      this.recordHistory({
        action: 'imported',
        subjectTitle: result.createdNodes.length === 1 ? result.createdNodes[0]!.title : `${result.createdNodes.length}개 항목`,
        detail
      })
    }
    return result
  }

  /**
   * Materializes a focused core's containment tree as ordinary folders and
   * files. This is an export only: it never changes managed files, revisions,
   * or an external upstream source.
   */
  async exportCore(coreId: string, destinationParent: string): Promise<MyDbCoreExportResult> {
    const rootCore = this.requireCore(coreId, false)
    const resolvedDestination = resolve(destinationParent)
    if (isWithin(this.root, resolvedDestination)) {
      throw new Error('My DB 저장소 내부에는 내보낼 수 없습니다. 다른 폴더를 선택해 주세요.')
    }
    const destinationStats = await stat(resolvedDestination)
    if (!destinationStats.isDirectory()) throw new Error('내보낼 폴더를 찾을 수 없습니다.')

    const rootDirectory = uniquePath(resolvedDestination, exportName(rootCore.title, 'My DB 자료'))
    const result: MyDbCoreExportResult = {
      folderName: basename(rootDirectory),
      exportedCores: 0,
      exportedFiles: 0,
      skippedFiles: 0
    }
    const visiting = new Set<string>()

    const exportTree = async (currentCore: CoreRow, directory: string): Promise<void> => {
      if (visiting.has(currentCore.id)) throw new Error('순환된 코어 관계는 내보낼 수 없습니다.')
      visiting.add(currentCore.id)
      await mkdir(directory, { recursive: true })
      result.exportedCores += 1

      // Older libraries used `related` for a core ↔ file connection, and the
      // connection UI also permits selecting the file first. Export every
      // directly linked active file once, regardless of that direction. Core
      // → core still uses only `contains` below because that is the folder
      // hierarchy.
      const files = this.database.prepare(
        `SELECT DISTINCT item.id, item.title, item.extension, item.file_type, item.tags_json, item.size,
                item.relative_path, item.source_path, item.created_at, item.updated_at, item.deleted_at
         FROM mydb_edges AS edge
         JOIN mydb_items AS item ON item.id = edge.target_id
         WHERE edge.source_id = ? AND item.deleted_at IS NULL
         UNION
         SELECT DISTINCT item.id, item.title, item.extension, item.file_type, item.tags_json, item.size,
                item.relative_path, item.source_path, item.created_at, item.updated_at, item.deleted_at
         FROM mydb_edges AS edge
         JOIN mydb_items AS item ON item.id = edge.source_id
         WHERE edge.target_id = ? AND item.deleted_at IS NULL
         ORDER BY item.title COLLATE NOCASE, item.id`
      ).all(currentCore.id, currentCore.id) as unknown as ItemRow[]
      for (const item of files) {
        const source = this.resolveLibraryFile(item.relative_path)
        if (!existsSync(source)) {
          result.skippedFiles += 1
          continue
        }
        const destination = uniquePath(directory, exportName(item.title, `자료${item.extension || ''}`))
        await copyFile(source, destination)
        result.exportedFiles += 1
      }

      const children = this.database.prepare(
        `SELECT core.id, core.title, core.created_at, core.updated_at, core.deleted_at
         FROM mydb_edges AS edge
         JOIN mydb_cores AS core ON core.id = edge.target_id
         WHERE edge.source_id = ? AND edge.relation = 'contains' AND core.deleted_at IS NULL
         ORDER BY core.title COLLATE NOCASE, core.id`
      ).all(currentCore.id) as unknown as CoreRow[]
      for (const child of children) {
        const childDirectory = uniquePath(directory, exportName(child.title, '코어'))
        await exportTree(child, childDirectory)
      }
      visiting.delete(currentCore.id)
    }

    await exportTree(rootCore, rootDirectory)
    if (result.exportedFiles === 0 && result.skippedFiles === 0) {
      await rm(rootDirectory, { recursive: true, force: true })
      throw new Error('내보낼 활성 파일이 없습니다. 연결된 파일이 휴지통에 있다면 복원한 뒤 다시 시도해 주세요.')
    }
    this.recordHistory({
      action: 'exported',
      subject: this.requireNode(rootCore.id, false),
      detail: `폴더 내보내기 · 코어 ${result.exportedCores}개 · 파일 ${result.exportedFiles}개${result.skippedFiles ? ` · 누락 ${result.skippedFiles}개` : ''}`
    })
    return result
  }

  /** Resolve an item only for a main-process action such as shell.openPath. */
  resolveItemPath(id: string): string {
    const item = this.requireItem(id, false)
    return this.resolveLibraryFile(item.relative_path)
  }

  private buildDailyReportBody(
    reportDate: string,
    rows: HistoryRow[],
    createdNodes: DailyCreatedNodeRow[]
  ): string {
    const nodes = this.database.prepare(
      `SELECT id, 'core' AS kind, title FROM mydb_cores
       UNION ALL
       SELECT id, 'file' AS kind, title FROM mydb_items`
    ).all() as Array<{ id: string; kind: MyDbNodeKind; title: string }>
    const nodesById = new Map(nodes.map((node) => [node.id, node]))
    const coreParents = new Map<string, string>()
    const fileOwners = new Map<string, string>()
    const relations = this.database.prepare(
      `SELECT source_id, target_id FROM mydb_edges WHERE relation = 'contains'`
    ).all() as Array<{ source_id: string; target_id: string }>
    for (const relation of relations) {
      const source = nodesById.get(relation.source_id)
      const target = nodesById.get(relation.target_id)
      if (source?.kind !== 'core' || !target) continue
      if (target.kind === 'core' && !coreParents.has(target.id)) coreParents.set(target.id, source.id)
      if (target.kind === 'file' && !fileOwners.has(target.id)) fileOwners.set(target.id, source.id)
    }

    const coreAncestorsFor = (node: { id: string; kind: MyDbNodeKind }): string[] => {
      const ancestorIds: string[] = []
      let cursor = node.kind === 'file' ? fileOwners.get(node.id) : node.id
      const seen = new Set<string>()
      while (cursor && !seen.has(cursor)) {
        seen.add(cursor)
        ancestorIds.unshift(cursor)
        cursor = coreParents.get(cursor)
      }
      return ancestorIds
    }
    const displayPathFor = (node: { id: string; kind: MyDbNodeKind }, title: string): { rootId?: string; path: string[] } => {
      const ancestorIds = coreAncestorsFor(node)
      const corePath = ancestorIds.map((id) => nodesById.get(id)?.title).filter((entry): entry is string => Boolean(entry))
      return {
        rootId: ancestorIds[0],
        path: node.kind === 'file'
          ? [...corePath, title]
          : corePath.length > 0 ? [...corePath.slice(0, -1), title] : [title]
      }
    }

    /**
     * 추가된 자료가 무엇인지 알아볼 수 있게 붙이는 꼬리표.
     * 이름만으로는 "무엇이 들어왔는지" 확인이 안 돼서, 형식·크기·시각과
     * 외부에서 가져온 파일이면 그 원본 경로까지 함께 적는다.
     */
    const fileFacts = (created: DailyCreatedNodeRow): string => {
      const facts: string[] = []
      // 확장자가 있으면 그쪽이 구체적이다 — file_type 은 'document' 처럼 뭉뚱그려진다.
      const kind = (created.extension || created.file_type || '').replace(/^\./, '')
      if (kind) facts.push(kind.toUpperCase())
      if (typeof created.size === 'number' && created.size > 0) facts.push(formatBytes(created.size))
      const time = reportTimeOf(created.created_at)
      if (time) facts.push(time)
      return facts.join(' · ')
    }

    const createdGroups = new Map<string, { title: string; cores: string[]; files: string[] }>()
    const unlinkedCreated: string[] = []
    for (const created of createdNodes) {
      const node = nodesById.get(created.id) ?? created
      const { rootId, path } = displayPathFor(node, created.title)
      const label = path.slice(1).join(' > ') || path[0] || created.title
      const isFile = created.kind === 'file'
      const facts = isFile ? fileFacts(created) : reportTimeOf(created.created_at)
      // 외부 원본에서 가져온 자료는 출처를 남긴다 — 어디서 온 자료인지가 곧 근거다.
      const origin = isFile && created.source_path ? `\n      원본: ${created.source_path}` : ''
      const entry = `${label}${facts ? ` (${facts})` : ''}${origin}`
      if (!rootId) {
        unlinkedCreated.push(`- ${isFile ? '파일' : '코어'}: ${entry}`)
        continue
      }
      const root = nodesById.get(rootId)
      const group = createdGroups.get(rootId) ?? { title: root?.title ?? '연결 경로 확인 필요', cores: [], files: [] }
      if (created.kind === 'core') group.cores.push(entry)
      else group.files.push(entry)
      createdGroups.set(rootId, group)
    }
    const createdSections = [...createdGroups.values()]
      .sort((left, right) => left.title.localeCompare(right.title, 'ko-KR'))
      .flatMap((group) => [
        `[${group.title}]`,
        ...(group.cores.length > 0 ? ['  생성된 코어', ...group.cores.map((title) => `  - ${title}`)] : []),
        ...(group.files.length > 0 ? ['  생성된 파일', ...group.files.map((title) => `  - ${title}`)] : []),
        ''
      ])
    if (unlinkedCreated.length > 0) createdSections.push('[미분류 또는 연결 경로가 없는 생성 항목]', ...unlinkedCreated, '')

    const groups = new Map<string, { title: string; lines: string[] }>()
    const unlinked: string[] = []
    for (const row of rows) {
      const subject = row.subject_id ? nodesById.get(row.subject_id) : undefined
      const resolved = subject ? displayPathFor(subject, row.subject_title) : { rootId: undefined, path: [row.subject_title] }
      const time = new Date(row.created_at).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false })
      const detail = [row.detail, row.related_title ? `대상: ${row.related_title}` : ''].filter(Boolean).join(' · ')
      const line = `- ${time} · ${historyActionReportLabel(row.action)} · ${resolved.path.join(' > ')}${detail ? ` (${detail})` : ''}`
      const rootId = resolved.rootId
      if (!rootId) {
        unlinked.push(line)
        continue
      }
      const root = nodesById.get(rootId)
      const group = groups.get(rootId) ?? { title: root?.title ?? '연결 경로 확인 필요', lines: [] }
      group.lines.push(line)
      groups.set(rootId, group)
    }

    const sections = [...groups.values()]
      .sort((left, right) => left.title.localeCompare(right.title, 'ko-KR'))
      .flatMap((group) => [`[${group.title}]`, ...group.lines, ''])
    if (unlinked.length > 0) sections.push('[연결 경로가 없는 변경]', ...unlinked, '')
    const creationBody = createdSections.length > 0
      ? createdSections.join('\n').trimEnd()
      : '새로 생성된 코어 또는 파일이 없습니다.'
    const changeBody = sections.length > 0
      ? sections.join('\n').trimEnd()
      : '전날 기록된 My DB 변경이 없습니다.'
    // 요약 줄은 "무엇이 얼마나 늘었는지"를 한눈에 보여 준다. 항목 수만 적으면
    // 코어가 늘었는지 자료가 들어왔는지 구분이 안 된다.
    const newCores = createdNodes.filter((node) => node.kind === 'core').length
    const newFiles = createdNodes.filter((node) => node.kind === 'file').length
    const addedBytes = createdNodes.reduce((sum, node) => sum + (node.size ?? 0), 0)
    const summary = [
      `총 ${rows.length}건의 변경`,
      newCores > 0 ? `새 코어 ${newCores}개` : '',
      newFiles > 0 ? `새 자료 ${newFiles}개` : '',
      addedBytes > 0 ? `추가 용량 ${formatBytes(addedBytes)}` : ''
    ].filter(Boolean).join(' · ')
    return `${reportDate} My DB 변경 보고\n${summary}\n\n[새로 생성된 코어와 파일]\n${creationBody}\n\n[변경 이력]\n${changeBody}`
  }

  private initializeSchema(): void {
    this.database.exec(
      `CREATE TABLE IF NOT EXISTS mydb_meta (
         key TEXT PRIMARY KEY,
         value TEXT NOT NULL
       );
       CREATE TABLE IF NOT EXISTS mydb_cores (
         id TEXT PRIMARY KEY,
         title TEXT NOT NULL,
         created_at TEXT NOT NULL,
         updated_at TEXT NOT NULL,
         deleted_at TEXT
       );
       CREATE TABLE IF NOT EXISTS mydb_items (
         id TEXT PRIMARY KEY,
         title TEXT NOT NULL,
         extension TEXT NOT NULL,
         file_type TEXT NOT NULL,
         tags_json TEXT NOT NULL DEFAULT '[]',
         size INTEGER NOT NULL,
         relative_path TEXT NOT NULL UNIQUE,
         source_path TEXT,
         created_at TEXT NOT NULL,
         updated_at TEXT NOT NULL,
         deleted_at TEXT
       );
       CREATE TABLE IF NOT EXISTS mydb_edges (
         id TEXT PRIMARY KEY,
         source_id TEXT NOT NULL,
         target_id TEXT NOT NULL,
         relation TEXT NOT NULL,
         created_at TEXT NOT NULL,
         updated_at TEXT NOT NULL,
         UNIQUE(source_id, target_id, relation)
       );
       CREATE TABLE IF NOT EXISTS mydb_history (
         id TEXT PRIMARY KEY,
         action TEXT NOT NULL,
         subject_id TEXT,
         subject_kind TEXT,
         subject_title TEXT NOT NULL,
         related_id TEXT,
         related_kind TEXT,
         related_title TEXT,
         detail TEXT,
         graph_checkpoint_id TEXT,
         created_at TEXT NOT NULL
       );
       CREATE TABLE IF NOT EXISTS mydb_daily_reports (
         report_date TEXT PRIMARY KEY,
         generated_at TEXT NOT NULL,
         total_changes INTEGER NOT NULL,
         body TEXT NOT NULL
       );
       CREATE TABLE IF NOT EXISTS mydb_graph_checkpoints (
         id TEXT PRIMARY KEY,
         reason TEXT NOT NULL,
         node_count INTEGER NOT NULL,
         edge_count INTEGER NOT NULL,
         snapshot_json TEXT NOT NULL,
         created_at TEXT NOT NULL
       );
       CREATE TABLE IF NOT EXISTS mydb_purged (
         id TEXT PRIMARY KEY,
         kind TEXT NOT NULL,
         title TEXT NOT NULL,
         purged_at TEXT NOT NULL
       );
       CREATE TABLE IF NOT EXISTS mydb_revisions (
         id TEXT PRIMARY KEY,
         item_id TEXT NOT NULL,
         sequence INTEGER NOT NULL,
         content_hash TEXT NOT NULL,
         size INTEGER NOT NULL,
         snapshot_relative_path TEXT NOT NULL UNIQUE,
         reason TEXT NOT NULL,
         created_at TEXT NOT NULL,
         UNIQUE(item_id, sequence)
       );
       CREATE INDEX IF NOT EXISTS idx_mydb_cores_live ON mydb_cores(deleted_at, updated_at DESC);
       CREATE INDEX IF NOT EXISTS idx_mydb_items_live ON mydb_items(deleted_at, updated_at DESC);
       CREATE INDEX IF NOT EXISTS idx_mydb_edges_source ON mydb_edges(source_id, relation);
       CREATE INDEX IF NOT EXISTS idx_mydb_edges_target ON mydb_edges(target_id, relation);
       CREATE INDEX IF NOT EXISTS idx_mydb_history_created ON mydb_history(created_at DESC);
       CREATE INDEX IF NOT EXISTS idx_mydb_daily_reports_generated ON mydb_daily_reports(generated_at DESC);
       CREATE INDEX IF NOT EXISTS idx_mydb_graph_checkpoints_created ON mydb_graph_checkpoints(created_at DESC);
       CREATE INDEX IF NOT EXISTS idx_mydb_revisions_item ON mydb_revisions(item_id, sequence DESC);
       CREATE INDEX IF NOT EXISTS idx_mydb_purged_at ON mydb_purged(purged_at DESC);`
    )
    this.addColumnIfMissing('mydb_items', 'source_path TEXT')
    this.addColumnIfMissing('mydb_history', 'graph_checkpoint_id TEXT')
    // Existing libraries predate graph checkpoints. The column must be added
    // before creating its index or SQLite rejects the entire startup schema.
    this.database.exec('CREATE INDEX IF NOT EXISTS idx_mydb_history_checkpoint ON mydb_history(graph_checkpoint_id);')
    this.database.prepare("INSERT OR IGNORE INTO mydb_meta (key, value) VALUES ('schema_version', '7')").run()
    this.database.prepare("UPDATE mydb_meta SET value = '7' WHERE key = 'schema_version'").run()
  }

  private addColumnIfMissing(table: string, definition: string): void {
    const column = definition.trim().split(/\s+/, 1)[0]
    const columns = this.database.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name?: string }>
    if (columns.some((entry) => entry.name === column)) return
    this.database.exec(`ALTER TABLE ${table} ADD COLUMN ${definition}`)
  }

  /**
   * The first My DB UI exposed a single "connect" action and wrote every
   * connection as `related`. For core → core links that action was meant to
   * create the same parent → child hierarchy as the original My DB System.
   * Migrate that unambiguous legacy shape once, preserving any edge that would
   * create a containment cycle as an ordinary relation instead.
   */
  private migrateLegacyCoreLinksToContains(): void {
    const migration = this.database.prepare('SELECT value FROM mydb_meta WHERE key = ?').get(LEGACY_CORE_LINK_MIGRATION_KEY) as { value?: string } | undefined
    if (migration?.value === 'done') return

    this.transaction(() => {
      const rows = this.database.prepare(
        `SELECT edge.id, edge.source_id, edge.target_id
         FROM mydb_edges AS edge
         JOIN mydb_cores AS source ON source.id = edge.source_id AND source.deleted_at IS NULL
         JOIN mydb_cores AS target ON target.id = edge.target_id AND target.deleted_at IS NULL
         WHERE edge.relation = 'related'
         ORDER BY edge.created_at ASC, edge.id ASC`
      ).all() as unknown as Array<Pick<EdgeRow, 'id' | 'source_id' | 'target_id'>>
      const containsExists = this.database.prepare(
        `SELECT id FROM mydb_edges
         WHERE source_id = ? AND target_id = ? AND relation = 'contains'`
      )
      const promote = this.database.prepare("UPDATE mydb_edges SET relation = 'contains', updated_at = ? WHERE id = ?")
      const remove = this.database.prepare('DELETE FROM mydb_edges WHERE id = ?')

      for (const row of rows) {
        if (this.wouldCreateContainmentCycle(row.source_id, row.target_id)) continue
        if (containsExists.get(row.source_id, row.target_id)) remove.run(row.id)
        else promote.run(now(), row.id)
      }
      this.database.prepare('INSERT OR REPLACE INTO mydb_meta (key, value) VALUES (?, ?)').run(LEGACY_CORE_LINK_MIGRATION_KEY, 'done')
    })
  }

  private transaction<T>(action: () => T): T {
    this.database.exec('BEGIN IMMEDIATE')
    try {
      const result = action()
      this.database.exec('COMMIT')
      return result
    } catch (error) {
      try {
        this.database.exec('ROLLBACK')
      } catch {
        // The original error is more helpful and may have happened before BEGIN.
      }
      throw error
    }
  }

  private createCoreWithOptionalParent(title: string, parentId?: string | null): CreatedCore {
    return this.transaction(() => {
      const createdAt = now()
      const id = randomUUID()
      this.database.prepare(
        'INSERT INTO mydb_cores (id, title, created_at, updated_at, deleted_at) VALUES (?, ?, ?, ?, NULL)'
      ).run(id, normalizeTitle(title, '코어'), createdAt, createdAt)
      const node = this.requireNode(id, false)
      const edge = parentId ? this.createEdge(parentId, id, 'contains').edge : undefined
      return { node, edge }
    })
  }

  private createEdge(sourceId: string, targetId: string, relation: MyDbRelation): CreatedEdge {
    if (!ALLOWED_RELATIONS.has(relation)) throw new Error('지원하지 않는 관계입니다.')
    if (sourceId === targetId) throw new Error('같은 항목끼리는 연결할 수 없습니다.')
    const source = this.requireNode(sourceId, false)
    const target = this.requireNode(targetId, false)

    if (relation === 'contains') {
      if (source.kind !== 'core') throw new Error('포함 관계의 시작점은 코어여야 합니다.')
      if (target.kind === 'core' && this.wouldCreateContainmentCycle(sourceId, targetId)) {
        throw new Error('코어 포함 관계가 순환하도록 만들 수 없습니다.')
      }
    }

    const existing = this.database.prepare(
      'SELECT id, source_id, target_id, relation, created_at, updated_at FROM mydb_edges WHERE source_id = ? AND target_id = ? AND relation = ?'
    ).get(source.id, target.id, relation) as unknown as EdgeRow | undefined
    if (existing) return { edge: edgeFromRow(existing), created: false }

    const createdAt = now()
    const edge: EdgeRow = {
      id: randomUUID(),
      source_id: source.id,
      target_id: target.id,
      relation,
      created_at: createdAt,
      updated_at: createdAt
    }
    this.database.prepare(
      'INSERT INTO mydb_edges (id, source_id, target_id, relation, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)'
    ).run(edge.id, edge.source_id, edge.target_id, edge.relation, edge.created_at, edge.updated_at)
    return { edge: edgeFromRow(edge), created: true }
  }

  private wouldCreateContainmentCycle(parentId: string, childId: string): boolean {
    const seen = new Set<string>()
    const queue = [childId]
    while (queue.length > 0) {
      const current = queue.shift() as string
      if (current === parentId) return true
      if (seen.has(current)) continue
      seen.add(current)
      const rows = this.database.prepare(
        `SELECT edge.target_id
         FROM mydb_edges edge
         JOIN mydb_cores child ON child.id = edge.target_id AND child.deleted_at IS NULL
         WHERE edge.source_id = ? AND edge.relation = 'contains'`
      ).all(current) as unknown as Array<{ target_id: string }>
      for (const row of rows) queue.push(row.target_id)
    }
    return false
  }

  private collectCoreSubtree(rootId: string): Set<string> {
    const result = new Set<string>()
    const queue = [rootId]
    while (queue.length > 0) {
      const current = queue.shift() as string
      if (result.has(current)) continue
      result.add(current)
      const children = this.database.prepare(
        `SELECT edge.target_id
         FROM mydb_edges edge
         JOIN mydb_cores child ON child.id = edge.target_id AND child.deleted_at IS NULL
         WHERE edge.source_id = ? AND edge.relation = 'contains'`
      ).all(current) as unknown as Array<{ target_id: string }>
      for (const child of children) queue.push(child.target_id)
    }
    return result
  }

  private collectOrphanItemsForCores(coreIds: ReadonlySet<string>): Set<string> {
    if (coreIds.size === 0) return new Set()
    const coreMarks = [...coreIds].map(() => '?').join(', ')
    const candidates = this.database.prepare(
      `SELECT DISTINCT edge.target_id AS item_id
       FROM mydb_edges edge
       JOIN mydb_items item ON item.id = edge.target_id AND item.deleted_at IS NULL
       WHERE edge.relation = 'contains' AND edge.source_id IN (${coreMarks})`
    ).all(...coreIds) as unknown as Array<{ item_id: string }>
    const result = new Set<string>()
    for (const candidate of candidates) {
      const external = this.database.prepare(
        `SELECT 1
         FROM mydb_edges edge
         JOIN mydb_cores core ON core.id = edge.source_id AND core.deleted_at IS NULL
         WHERE edge.target_id = ? AND edge.relation = 'contains' AND edge.source_id NOT IN (${coreMarks})
         LIMIT 1`
      ).get(candidate.item_id, ...coreIds)
      if (!external) result.add(candidate.item_id)
    }
    return result
  }

  private updateDeleted(table: 'mydb_cores' | 'mydb_items', ids: ReadonlySet<string>, deletedAt: string): void {
    if (ids.size === 0) return
    const marks = [...ids].map(() => '?').join(', ')
    this.database.prepare(
      `UPDATE ${table} SET deleted_at = ?, updated_at = ? WHERE id IN (${marks})`
    ).run(deletedAt, deletedAt, ...ids)
  }

  private async importFolder(source: string, parentCoreId: string | null, result: MyDbImportResult): Promise<void> {
    const created = this.createCoreWithOptionalParent(basename(source), parentCoreId)
    result.createdNodes.push(created.node)
    if (created.edge) result.createdEdges.push(created.edge)

    const entries = await readdir(source, { withFileTypes: true })
    entries.sort((left, right) => left.name.localeCompare(right.name, undefined, { sensitivity: 'base' }))
    for (const entry of entries) {
      const childPath = join(source, entry.name)
      if (entry.isSymbolicLink()) throw new Error(`바로가기 또는 심볼릭 링크는 가져올 수 없습니다: ${entry.name}`)
      if (entry.isDirectory()) {
        await this.importFolder(childPath, created.node.id, result)
      } else if (entry.isFile()) {
        const item = await this.importFile(childPath, created.node.id)
        result.createdNodes.push(item.node)
        if (item.edge) result.createdEdges.push(item.edge)
      } else {
        result.skippedPaths.push(childPath)
      }
    }
  }

  private async importFile(source: string, parentCoreId: string | null): Promise<CreatedItem> {
    const sourceStats = await stat(source)
    if (!sourceStats.isFile()) throw new Error(`파일이 아닙니다: ${basename(source)}`)
    if (parentCoreId) this.requireCore(parentCoreId, false)

    const originalName = assertSafeFileName(basename(source))
    const extension = extname(originalName).toLowerCase()
    const fileType = detectFileType(extension)
    const id = randomUUID()
    const directory = this.storageDirectoryForCore(parentCoreId)
    const destination = uniquePath(directory, exportName(originalName, `자료${extension || ''}`))
    const relativePath = this.toLibraryRelative(destination)

    await mkdir(directory, { recursive: true })
    await copyFile(source, destination)
    try {
      const created = this.transaction(() => {
        const createdAt = now()
        this.database.prepare(
          `INSERT INTO mydb_items
           (id, title, extension, file_type, tags_json, size, relative_path, source_path, created_at, updated_at, deleted_at)
           VALUES (?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, NULL)`
        ).run(id, originalName, extension, fileType, sourceStats.size, relativePath, resolve(source), createdAt, createdAt)
        const node = this.requireNode(id, false)
        const edge = parentCoreId ? this.createEdge(parentCoreId, id, 'contains').edge : undefined
        return { node, edge }
      })
      const item = this.requireItem(id, false)
      const initial = await this.snapshotCurrentFile(item, 'initial', true)
      if (!initial) throw new Error('처음 보관한 파일의 원본 버전을 만들지 못했습니다.')
      this.watchManagedFile(item)
      this.watchSourceFile(item)
      return created
    } catch (error) {
      this.transaction(() => {
        this.database.prepare('DELETE FROM mydb_edges WHERE source_id = ? OR target_id = ?').run(id, id)
        this.database.prepare('DELETE FROM mydb_revisions WHERE item_id = ?').run(id)
        this.database.prepare('DELETE FROM mydb_items WHERE id = ?').run(id)
      })
      await rm(destination, { force: true }).catch(() => undefined)
      await rm(join(this.revisionsRoot, id), { recursive: true, force: true }).catch(() => undefined)
      throw error
    }
  }

  private async assertSafeImportSource(source: string): Promise<void> {
    if (isWithin(this.root, source)) {
      throw new Error('My DB 저장소 안의 파일을 다시 가져올 수 없습니다.')
    }
    const sourceStats = await lstat(source)
    if (sourceStats.isSymbolicLink()) {
      throw new Error(`바로가기 또는 심볼릭 링크는 가져올 수 없습니다: ${basename(source)}`)
    }
    if (sourceStats.isFile()) return
    if (!sourceStats.isDirectory()) throw new Error(`지원하지 않는 항목입니다: ${basename(source)}`)
    const entries = await readdir(source, { withFileTypes: true })
    for (const entry of entries) await this.assertSafeImportSource(join(source, entry.name))
  }

  private async renameItemAtomically(item: ItemRow, currentPath: string, targetPath: string, fileName: string): Promise<MyDbNode> {
    const targetRelativePath = this.toLibraryRelative(targetPath)
    const extension = extname(fileName).toLowerCase()
    const fileType = detectFileType(extension)
    const updatedAt = now()
    try {
      mkdirSync(dirname(targetPath), { recursive: true })
      await rename(currentPath, targetPath)
      try {
        return this.transaction(() => {
          this.database.prepare(
            `UPDATE mydb_items
             SET title = ?, extension = ?, file_type = ?, relative_path = ?, updated_at = ?
             WHERE id = ?`
          ).run(fileName, extension, fileType, targetRelativePath, updatedAt, item.id)
          return this.requireNode(item.id, false)
        })
      } catch (error) {
        await rename(targetPath, currentPath).catch(() => undefined)
        throw error
      }
    } catch (error) {
      throw error
    }
  }

  private requireCore(id: string, includeDeleted: boolean): CoreRow {
    const row = this.database.prepare(
      `SELECT id, title, created_at, updated_at, deleted_at FROM mydb_cores WHERE id = ?${includeDeleted ? '' : ' AND deleted_at IS NULL'}`
    ).get(id) as unknown as CoreRow | undefined
    if (!row) throw new Error('대상 코어를 찾을 수 없습니다.')
    return row
  }

  private requireItem(id: string, includeDeleted: boolean): ItemRow {
    const row = this.database.prepare(
      `SELECT id, title, extension, file_type, tags_json, size, relative_path, source_path, created_at, updated_at, deleted_at
       FROM mydb_items WHERE id = ?${includeDeleted ? '' : ' AND deleted_at IS NULL'}`
    ).get(id) as unknown as ItemRow | undefined
    if (!row) throw new Error('대상 파일을 찾을 수 없습니다.')
    return row
  }

  private requireNodeRow(id: string, includeDeleted: boolean): NodeRow {
    const row = this.database.prepare(
      `SELECT id, 'core' AS kind, title, NULL AS file_type, NULL AS tags_json, NULL AS size,
              NULL AS relative_path, created_at, updated_at, deleted_at
       FROM mydb_cores WHERE id = ?
       UNION ALL
       SELECT id, 'file' AS kind, title, file_type, tags_json, size, relative_path,
              created_at, updated_at, deleted_at
       FROM mydb_items WHERE id = ?
       LIMIT 1`
    ).get(id, id) as unknown as NodeRow | undefined
    if (!row || (!includeDeleted && row.deleted_at)) throw new Error('대상 항목을 찾을 수 없습니다.')
    return row
  }

  private requireNode(id: string, includeDeleted: boolean): MyDbNode {
    return nodeFromRow(this.requireNodeRow(id, includeDeleted))
  }

  private resolveLibraryFile(relativePath: string): string {
    const absolute = resolve(this.root, ...relativePath.split('/'))
    if (!this.isManagedLibraryPath(absolute)) throw new Error('My DB 파일 경로가 올바르지 않습니다.')
    return absolute
  }

  private toLibraryRelative(absolutePath: string): string {
    const resolved = resolve(absolutePath)
    if (!this.isManagedLibraryPath(resolved)) throw new Error('My DB 파일은 전용 저장소 안에 있어야 합니다.')
    return normalizeRelativePath(relative(this.root, resolved))
  }

  private isManagedLibraryPath(absolutePath: string): boolean {
    if (!isWithin(this.filesRoot, absolutePath)) return false
    return normalizeRelativePath(relative(this.filesRoot, absolutePath)) !== ''
  }

  /** Accept retired layouts only while the startup migration is running. */
  private resolveStoredLibraryFile(relativePath: string): string {
    try {
      return this.resolveLibraryFile(relativePath)
    } catch {
      const absolute = resolve(this.root, ...relativePath.split('/'))
      const topLevel = normalizeRelativePath(relative(this.root, absolute)).split('/')[0]
      if (isWithin(this.root, absolute) && [LIBRARY_UNSORTED_DIR, LIBRARY_TRASH_DIR, '코어'].includes(topLevel)) {
        return absolute
      }
      throw new Error('My DB 파일 경로가 올바르지 않습니다.')
    }
  }

  /** Returns a readable folder path that mirrors the current core hierarchy. */
  private storageDirectoryForCore(coreId: string | null): string {
    if (!coreId) return join(this.filesRoot, LIBRARY_UNSORTED_DIR)
    const segments: string[] = []
    const visited = new Set<string>()
    let currentId: string | null = coreId

    while (currentId && !visited.has(currentId)) {
      visited.add(currentId)
      const core = this.requireCore(currentId, false)
      segments.unshift(exportName(core.title, '코어'))
      const parent = this.database.prepare(
        `SELECT parent.id
         FROM mydb_edges AS edge
         JOIN mydb_cores AS parent ON parent.id = edge.source_id AND parent.deleted_at IS NULL
         WHERE edge.target_id = ? AND edge.relation = 'contains'
         ORDER BY edge.created_at ASC, edge.id ASC
         LIMIT 1`
      ).get(currentId) as { id?: string } | undefined
      currentId = parent?.id ?? null
    }

    return join(this.filesRoot, ...segments)
  }

  /** Prefer a structural owner, then accept an older direct connection. */
  private storageOwnerCoreId(itemId: string): string | null {
    const row = this.database.prepare(
      `SELECT core_id FROM (
         SELECT core.id AS core_id, 0 AS priority, edge.created_at, edge.id
         FROM mydb_edges AS edge
         JOIN mydb_cores AS core ON core.id = edge.source_id AND core.deleted_at IS NULL
         WHERE edge.target_id = ? AND edge.relation = 'contains'
         UNION ALL
         SELECT core.id AS core_id, 1 AS priority, edge.created_at, edge.id
         FROM mydb_edges AS edge
         JOIN mydb_cores AS core ON core.id = edge.source_id AND core.deleted_at IS NULL
         WHERE edge.target_id = ? AND edge.relation <> 'contains'
         UNION ALL
         SELECT core.id AS core_id, 2 AS priority, edge.created_at, edge.id
         FROM mydb_edges AS edge
         JOIN mydb_cores AS core ON core.id = edge.target_id AND core.deleted_at IS NULL
         WHERE edge.source_id = ?
       ) ORDER BY priority ASC, created_at ASC, id ASC LIMIT 1`
    ).get(itemId, itemId, itemId) as { core_id?: string } | undefined
    return row?.core_id ?? null
  }

  /**
   * Keeps the on-disk library readable: each live item lives below its owner
   * core's folder hierarchy, while unlinked files go to a clearly named inbox.
   * Existing legacy files are migrated one by one and failures leave their
   * current DB path untouched.
   */
  private organizeManagedFilesByCore(): void {
    const items = this.database.prepare(
      `SELECT id, title, extension, file_type, tags_json, size, relative_path, source_path, created_at, updated_at, deleted_at
       FROM mydb_items`
    ).all() as unknown as ItemRow[]

    for (const item of items) {
      try {
        const current = this.resolveStoredLibraryFile(item.relative_path)
        if (!existsSync(current)) continue
        const ownerId = item.deleted_at ? null : this.storageOwnerCoreId(item.id)
        const directory = item.deleted_at
          ? join(this.filesRoot, LIBRARY_TRASH_DIR)
          : this.storageDirectoryForCore(ownerId)
        const requested = exportName(item.title, `자료${item.extension || ''}`)
        const requestedPath = join(directory, requested)
        if (resolve(current) === resolve(requestedPath)) continue

        mkdirSync(directory, { recursive: true })
        const destination = uniquePath(directory, requested)
        renameSync(current, destination)
        this.database.prepare('UPDATE mydb_items SET relative_path = ? WHERE id = ?').run(this.toLibraryRelative(destination), item.id)
      } catch {
        // A migration failure must never make the My DB library unavailable.
        // The current record remains valid and can be retried after restart.
      }
    }
    this.removeEmptyRetiredDirectories()
  }

  /** Remove only empty folders from retired intermediate layouts. */
  private removeEmptyRetiredDirectories(): void {
    const prune = (directory: string): boolean => {
      if (!existsSync(directory)) return true
      for (const entry of readdirSync(directory, { withFileTypes: true })) {
        if (entry.isDirectory()) prune(join(directory, entry.name))
      }
      if (readdirSync(directory).length > 0) return false
      rmdirSync(directory)
      return true
    }
    try {
      // Old `files/코어/...` entries are moved to `files/<core>/...`.
      prune(join(this.filesRoot, '코어'))
      // Also recover safely if the short-lived root-level layout was used.
      for (const directory of [LIBRARY_UNSORTED_DIR, LIBRARY_TRASH_DIR, '코어']) {
        prune(join(this.root, directory))
      }
    } catch {
      // Leave a folder alone if another process is using it.
    }
  }

  private resolveRevisionFile(relativePath: string): string {
    const absolute = resolve(this.root, ...relativePath.split('/'))
    if (!isWithin(this.revisionsRoot, absolute)) throw new Error('My DB 버전 파일 경로가 올바르지 않습니다.')
    return absolute
  }

  private toRevisionRelative(absolutePath: string): string {
    const resolved = resolve(absolutePath)
    if (!isWithin(this.revisionsRoot, resolved)) throw new Error('My DB 버전 파일은 전용 버전 저장소 안에 있어야 합니다.')
    return normalizeRelativePath(relative(this.root, resolved))
  }

  private listRevisions(itemId: string): RevisionRow[] {
    return this.database.prepare(
      `SELECT id, item_id, sequence, content_hash, size, snapshot_relative_path, reason, created_at
       FROM mydb_revisions
       WHERE item_id = ?
       ORDER BY sequence DESC`
    ).all(itemId) as unknown as RevisionRow[]
  }

  private requireRevision(itemId: string, revisionId: string): RevisionRow {
    const row = this.database.prepare(
      `SELECT id, item_id, sequence, content_hash, size, snapshot_relative_path, reason, created_at
       FROM mydb_revisions
       WHERE item_id = ? AND id = ?`
    ).get(itemId, revisionId) as unknown as RevisionRow | undefined
    if (!row) throw new Error('선택한 파일 버전을 찾을 수 없습니다.')
    return row
  }

  private async initializeRevisionTracking(): Promise<void> {
    try {
      const items = this.database.prepare(
        `SELECT id, title, extension, file_type, tags_json, size, relative_path, source_path, created_at, updated_at, deleted_at
         FROM mydb_items`
      ).all() as unknown as ItemRow[]
      for (const item of items) {
        if (this.closed || !existsSync(this.resolveLibraryFile(item.relative_path))) continue
        if (this.listRevisions(item.id).length > 0) continue
        try {
          await this.snapshotCurrentFile(item, 'initial', true)
        } catch (error) {
          // Keep the rest of the private library available even if one legacy
          // file cannot be read. The original remains in place and can be
          // retried the next time the app starts.
          console.warn('[mydb] 초기 버전 보관 실패:', item.title, error)
        }
      }
      if (!this.closed) this.startFileWatchers(items)
    } catch (error) {
      console.warn('[mydb] 파일 버전 감시를 시작하지 못했습니다:', error)
    }
  }

  private startFileWatchers(items: readonly ItemRow[]): void {
    if (this.closed) return
    for (const item of items) {
      this.watchManagedFile(item)
      this.watchSourceFile(item)
    }
  }

  /**
   * Release both watchers for one item before its file is unlinked.  A trashed
   * item is normally unwatched already (watchManagedFile skips deleted rows),
   * but purge must not depend on that: an open handle makes unlink fail on
   * Windows and would leave the bytes behind after the row is gone.
   */
  private stopWatching(id: string): void {
    const managed = this.watchedPaths.get(id)
    if (managed) {
      unwatchFile(managed)
      this.watchedPaths.delete(id)
    }
    const source = this.watchedSourcePaths.get(id)
    if (source) {
      unwatchFile(source)
      this.watchedSourcePaths.delete(id)
    }
  }

  private restartFileWatchers(): void {
    for (const path of this.watchedPaths.values()) unwatchFile(path)
    for (const path of this.watchedSourcePaths.values()) unwatchFile(path)
    this.watchedPaths.clear()
    this.watchedSourcePaths.clear()
    const items = this.database.prepare(
      `SELECT id, title, extension, file_type, tags_json, size, relative_path, source_path, created_at, updated_at, deleted_at
       FROM mydb_items`
    ).all() as unknown as ItemRow[]
    this.startFileWatchers(items)
  }

  /**
   * Poll individual managed files rather than recursively watching the entire
   * library. This remains reliable for editors that use atomic replacement
   * saves and avoids platform-specific recursive-watch failures.
   */
  private watchManagedFile(item: ItemRow): void {
    if (this.closed || item.deleted_at) return
    const absolutePath = this.resolveLibraryFile(item.relative_path)
    const existingPath = this.watchedPaths.get(item.id)
    if (existingPath === absolutePath) return
    if (existingPath) unwatchFile(existingPath)
    this.watchedPaths.set(item.id, absolutePath)
    watchFile(absolutePath, { interval: 750, persistent: false }, (current, previous) => {
      if (this.closed || (!current.isFile() && current.size === 0)) return
      if (current.mtimeMs === previous.mtimeMs && current.size === previous.size) return
      this.queueManagedFileChange(absolutePath)
    })
  }

  /**
   * A source is upstream only: a save in the selected original is copied into
   * My DB, while My DB edits never write back to that source.
   */
  private watchSourceFile(item: ItemRow): void {
    if (this.closed || item.deleted_at || !item.source_path) return
    const absolutePath = resolve(item.source_path)
    const existingPath = this.watchedSourcePaths.get(item.id)
    if (existingPath === absolutePath) return
    if (existingPath) unwatchFile(existingPath)
    this.watchedSourcePaths.set(item.id, absolutePath)
    watchFile(absolutePath, { interval: 750, persistent: false }, (current, previous) => {
      if (this.closed || (!current.isFile() && current.size === 0)) return
      if (current.mtimeMs === previous.mtimeMs && current.size === previous.size) return
      this.queueSourceFileChange(item.id)
    })
  }

  private queueSourceFileChange(itemId: string): void {
    if (this.closed) return
    const currentTimer = this.pendingSourceChanges.get(itemId)
    if (currentTimer) clearTimeout(currentTimer)
    const timer = setTimeout(() => {
      this.pendingSourceChanges.delete(itemId)
      void this.syncFromSource(itemId)
    }, WATCH_DEBOUNCE_MS)
    this.pendingSourceChanges.set(itemId, timer)
  }

  private async syncFromSource(itemId: string): Promise<void> {
    if (this.closed) return
    try {
      const item = this.requireItem(itemId, false)
      if (!item.source_path) return
      const source = resolve(item.source_path)
      const sourceStats = await stat(source)
      if (!sourceStats.isFile()) return

      // Preserve a local edit as a normal version before an upstream source
      // update overwrites the managed copy.
      await this.captureChangedRevision(item, 'content_changed')
      await copyFile(source, this.resolveLibraryFile(item.relative_path))
      const synced = await this.snapshotCurrentFile(item, 'source_synced', false)
      if (!synced) return
      const updatedAt = now()
      this.database.prepare('UPDATE mydb_items SET size = ?, updated_at = ? WHERE id = ?').run(synced.size, updatedAt, item.id)
      this.recordHistory({ action: 'source_synced', subject: this.requireNode(item.id, false), detail: `외부 원본 반영 · v${synced.sequence}` })
    } catch (error) {
      if (!this.closed) console.warn('[mydb] 외부 원본 동기화 실패:', error)
    }
  }

  private queueManagedFileChange(absolutePath: string): void {
    if (this.closed || !isWithin(this.filesRoot, absolutePath)) return
    const currentTimer = this.pendingFileChanges.get(absolutePath)
    if (currentTimer) clearTimeout(currentTimer)
    const timer = setTimeout(() => {
      this.pendingFileChanges.delete(absolutePath)
      void this.captureWatchedFileChange(absolutePath)
    }, WATCH_DEBOUNCE_MS)
    this.pendingFileChanges.set(absolutePath, timer)
  }

  private async captureWatchedFileChange(absolutePath: string): Promise<void> {
    if (this.closed || !isWithin(this.filesRoot, absolutePath)) return
    try {
      const sourceStats = await stat(absolutePath)
      if (!sourceStats.isFile()) return
      const relativePath = this.toLibraryRelative(absolutePath)
      const item = this.database.prepare(
        `SELECT id, title, extension, file_type, tags_json, size, relative_path, created_at, updated_at, deleted_at
         FROM mydb_items
         WHERE relative_path = ? AND deleted_at IS NULL`
      ).get(relativePath) as unknown as ItemRow | undefined
      if (!item) return
      await this.captureChangedRevision(item, 'content_changed')
    } catch (error) {
      if (!this.closed) console.warn('[mydb] 파일 변경 버전 기록 실패:', error)
    }
  }

  private async captureChangedRevision(item: ItemRow, reason: MyDbRevisionReason): Promise<MyDbRevision | null> {
    return this.withRevisionLock(item.id, async () => {
      const revision = await this.snapshotCurrentFileUnsafe(item, reason, false)
      if (!revision) return null
      const updatedAt = now()
      this.database.prepare('UPDATE mydb_items SET size = ?, updated_at = ? WHERE id = ?').run(revision.size, updatedAt, item.id)
      const changed = this.requireNode(item.id, false)
      this.recordHistory({
        action: 'content_changed',
        subject: changed,
        detail: `v${revision.sequence} 저장`
      })
      return revision
    })
  }

  private async snapshotCurrentFile(item: ItemRow, reason: MyDbRevisionReason, force: boolean): Promise<MyDbRevision | null> {
    return this.withRevisionLock(item.id, () => this.snapshotCurrentFileUnsafe(item, reason, force))
  }

  private async snapshotCurrentFileUnsafe(item: ItemRow, reason: MyDbRevisionReason, force: boolean): Promise<MyDbRevision | null> {
    const source = this.resolveLibraryFile(item.relative_path)
    const sourceStats = await stat(source)
    if (!sourceStats.isFile()) throw new Error('보관한 파일을 찾을 수 없습니다.')
    const previous = this.listRevisions(item.id)[0]
    const sequence = (previous?.sequence ?? 0) + 1
    const revisionId = randomUUID()
    const extension = item.extension || '.bin'
    const destinationDir = join(this.revisionsRoot, item.id)
    const destination = join(destinationDir, `${String(sequence).padStart(6, '0')}-${revisionId}${extension}`)
    await mkdir(destinationDir, { recursive: true })
    try {
      const copied = await copyAndHashFile(source, destination, sourceStats.size)
      if (!force && previous?.content_hash === copied.hash) {
        await rm(destination, { force: true })
        return null
      }
      const row: RevisionRow = {
        id: revisionId,
        item_id: item.id,
        sequence,
        content_hash: copied.hash,
        size: copied.size,
        snapshot_relative_path: this.toRevisionRelative(destination),
        reason,
        created_at: now()
      }
      this.transaction(() => {
        this.database.prepare(
          `INSERT INTO mydb_revisions
           (id, item_id, sequence, content_hash, size, snapshot_relative_path, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
        ).run(
          row.id,
          row.item_id,
          row.sequence,
          row.content_hash,
          row.size,
          row.snapshot_relative_path,
          row.reason,
          row.created_at
        )
      })
      await this.pruneRevisions(item.id)
      return revisionFromRow(row)
    } catch (error) {
      await rm(destination, { force: true }).catch(() => undefined)
      throw error
    }
  }

  /**
   * 항목당 리비전을 한도까지만 남긴다. 정리가 없으면 자주 고치는 파일 하나가
   * 리비전 스냅샷 파일을 무한히 쌓는다.
   *
   * 초기본(sequence=1)은 한도와 무관하게 항상 남긴다. 사용자 요구사항이
   * "수정되면 어떻게 수정되었는지 기록이 남아서 복구할 수 있으면 좋겠다"이므로,
   * '처음 상태로 되돌리기'는 어떤 경우에도 보장돼야 한다.
   *
   * 이미 withRevisionLock 안에서 불리므로 동시 캡처와 충돌하지 않는다.
   */
  private async pruneRevisions(itemId: string): Promise<void> {
    try {
      const doomed = this.database.prepare(
        `SELECT id, snapshot_relative_path FROM mydb_revisions
          WHERE item_id = ? AND sequence <> 1
            AND id NOT IN (
              SELECT id FROM mydb_revisions
               WHERE item_id = ? ORDER BY sequence DESC LIMIT ?
            )`
      ).all(itemId, itemId, REVISION_KEEP_PER_ITEM) as Array<{
        id: string
        snapshot_relative_path: string
      }>
      if (doomed.length === 0) return
      const placeholders = doomed.map(() => '?').join(',')
      this.transaction(() => {
        this.database.prepare(
          `DELETE FROM mydb_revisions WHERE id IN (${placeholders})`
        ).run(...doomed.map((row) => row.id))
      })
      // DB에서 지운 뒤에 파일을 지운다. 순서가 반대면 행은 남고 파일만 사라져
      // 복구 버튼이 있는데 열 수 없는 상태가 된다.
      for (const row of doomed) {
        await rm(this.resolveRevisionFile(row.snapshot_relative_path), { force: true })
          .catch(() => undefined)
      }
    } catch (err) {
      console.error('[mydb] 리비전 정리 실패:', err)
    }
  }

  private async withRevisionLock<T>(itemId: string, operation: () => Promise<T>): Promise<T> {
    const previous = this.revisionChains.get(itemId) ?? Promise.resolve()
    const current = previous.catch(() => undefined).then(operation)
    this.revisionChains.set(itemId, current)
    try {
      return await current
    } finally {
      if (this.revisionChains.get(itemId) === current) this.revisionChains.delete(itemId)
    }
  }

  private recordHistory(input: {
    action: MyDbHistoryAction
    subject?: MyDbNode
    subjectTitle?: string
    related?: MyDbNode
    detail?: string
  }): void {
    const subjectTitle = input.subject?.title ?? input.subjectTitle
    if (!subjectTitle) throw new Error('My DB 이력의 대상 이름을 확인할 수 없습니다.')
    const historyId = randomUUID()
    this.database.prepare(
      `INSERT INTO mydb_history
       (id, action, subject_id, subject_kind, subject_title, related_id, related_kind, related_title, detail, graph_checkpoint_id, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)`
    ).run(
      historyId,
      input.action,
      input.subject?.id ?? null,
      input.subject?.kind ?? null,
      subjectTitle,
      input.related?.id ?? null,
      input.related?.kind ?? null,
      input.related?.title ?? null,
      input.detail ?? null,
      now()
    )
    if (!this.shouldCheckpointGraph(input.action)) return
    const checkpoint = this.captureGraphCheckpoint(input.action)
    this.database.prepare('UPDATE mydb_history SET graph_checkpoint_id = ? WHERE id = ?').run(checkpoint.id, historyId)
    this.pruneGraphCheckpoints()
  }

  private shouldCheckpointGraph(action: MyDbHistoryAction): boolean {
    return !['content_changed', 'revision_restored', 'source_synced', 'source_linked', 'exported'].includes(action)
  }

  private captureGraphCheckpoint(reason: string): MyDbGraphCheckpoint {
    const state: GraphSnapshotData = {
      version: 1,
      cores: this.database.prepare(
        'SELECT id, title, created_at, updated_at, deleted_at FROM mydb_cores ORDER BY id'
      ).all() as unknown as CoreRow[],
      items: this.database.prepare(
        `SELECT id, title, extension, file_type, tags_json, size, relative_path, source_path, created_at, updated_at, deleted_at
         FROM mydb_items ORDER BY id`
      ).all() as unknown as ItemRow[],
      edges: this.database.prepare(
        'SELECT id, source_id, target_id, relation, created_at, updated_at FROM mydb_edges ORDER BY id'
      ).all() as unknown as EdgeRow[]
    }
    const row: GraphCheckpointRow = {
      id: randomUUID(),
      reason,
      node_count: state.cores.length + state.items.length,
      edge_count: state.edges.length,
      snapshot_json: JSON.stringify(state),
      created_at: now()
    }
    this.database.prepare(
      `INSERT INTO mydb_graph_checkpoints (id, reason, node_count, edge_count, snapshot_json, created_at)
       VALUES (?, ?, ?, ?, ?, ?)`
    ).run(row.id, row.reason, row.node_count, row.edge_count, row.snapshot_json, row.created_at)
    return this.graphCheckpointFromRow(row)
  }

  /**
   * 오래된 그래프 시점을 정리한다. 구조 변경마다 전체 그래프 JSON을 저장하므로
   * 정리가 없으면 라이브러리가 커질수록 저장량이 2차로 증가한다.
   *
   * 히스토리 목록이 HISTORY_LIMIT까지만 보이고 복원 버튼은 그 목록에서만 노출되므로,
   * 한도 밖 시점은 애초에 UI에서 도달할 수 없다. 지워도 잃는 기능이 없다.
   *
   * 남은 히스토리 행의 graph_checkpoint_id 를 NULL 로 만들면 MyDbView 가
   * `entry.graphCheckpointId &&` 로 버튼을 조건부 렌더하므로 버튼이 그냥 사라진다 —
   * 렌더러 수정이 필요 없다.
   *
   * transaction() 으로 감싸지 않는다. 이 메서드는 recordHistory 가 연 트랜잭션 안에서
   * 불리고, transaction() 은 BEGIN IMMEDIATE 라 중첩되지 않는다(captureGraphCheckpoint 가
   * 평문 run() 인 것과 같은 이유).
   */
  private pruneGraphCheckpoints(): void {
    try {
      const keep = this.database.prepare(
        'SELECT id FROM mydb_graph_checkpoints ORDER BY created_at DESC, id DESC LIMIT ?'
      ).all(GRAPH_CHECKPOINT_LIMIT) as Array<{ id: string }>
      if (keep.length < GRAPH_CHECKPOINT_LIMIT) return
      const placeholders = keep.map(() => '?').join(',')
      const ids = keep.map((row) => row.id)
      this.database.prepare(
        `UPDATE mydb_history SET graph_checkpoint_id = NULL
          WHERE graph_checkpoint_id IS NOT NULL AND graph_checkpoint_id NOT IN (${placeholders})`
      ).run(...ids)
      this.database.prepare(
        `DELETE FROM mydb_graph_checkpoints WHERE id NOT IN (${placeholders})`
      ).run(...ids)
    } catch (err) {
      // 보존 정리는 유지보수다. 실패해도 사용자 동작(임포트·이름변경 등)을 막지 않는다.
      console.error('[mydb] 그래프 시점 정리 실패:', err)
    }
  }

  private graphCheckpointFromRow(row: GraphCheckpointRow): MyDbGraphCheckpoint {
    return {
      id: row.id,
      reason: row.reason,
      nodeCount: row.node_count,
      edgeCount: row.edge_count,
      createdAt: row.created_at
    }
  }

  private parseGraphSnapshot(raw: string): GraphSnapshotData {
    let parsed: unknown
    try {
      parsed = JSON.parse(raw)
    } catch {
      throw new Error('저장된 그래프 시점의 형식이 올바르지 않습니다.')
    }
    if (!parsed || typeof parsed !== 'object') throw new Error('저장된 그래프 시점의 형식이 올바르지 않습니다.')
    const value = parsed as Partial<GraphSnapshotData>
    if (value.version !== 1 || !Array.isArray(value.cores) || !Array.isArray(value.items) || !Array.isArray(value.edges)) {
      throw new Error('지원하지 않는 그래프 시점입니다.')
    }
    return value as GraphSnapshotData
  }
}

let configuredStore: MyDbStore | null = null

/** Configure the singleton from Electron's main-process startup path. */
export function configureMyDbStorageRoot(root: string): MyDbStore {
  const resolved = resolve(root)
  if (configuredStore?.root === resolved) return configuredStore
  configuredStore?.close()
  configuredStore = new MyDbStore(resolved)
  return configuredStore
}

export function getMyDbStore(): MyDbStore {
  if (!configuredStore) throw new Error('My DB 저장소가 아직 준비되지 않았습니다.')
  return configuredStore
}

export function closeMyDbStorage(): void {
  configuredStore?.close()
  configuredStore = null
}

/**
 * Clears only My DB-owned data under the configured root. The chosen root and
 * unrelated user files are deliberately retained so the library can start
 * fresh immediately afterwards.
 */
export async function myDbClearAll(): Promise<void> {
  const root = myDbStorageRoot()
  closeMyDbStorage()
  await Promise.all([
    rm(join(root, LIBRARY_DATABASE_FILE), { force: true }),
    rm(join(root, `${LIBRARY_DATABASE_FILE}-wal`), { force: true }),
    rm(join(root, `${LIBRARY_DATABASE_FILE}-shm`), { force: true }),
    // Remove the short-lived root-level layout too, in case it was created
    // before the migration to `files/<core>/...` runs.
    rm(join(root, '코어'), { recursive: true, force: true }),
    rm(join(root, LIBRARY_UNSORTED_DIR), { recursive: true, force: true }),
    rm(join(root, LIBRARY_TRASH_DIR), { recursive: true, force: true }),
    rm(join(root, LIBRARY_FILES_DIR), { recursive: true, force: true }),
    rm(join(root, LIBRARY_REVISIONS_DIR), { recursive: true, force: true })
  ])
  configureMyDbStorageRoot(root)
}

export function myDbState(): MyDbSnapshot {
  return getMyDbStore().snapshot()
}

export function myDbTrash(): MyDbTrashSnapshot {
  return getMyDbStore().trash()
}

export function myDbHistory(): MyDbHistorySnapshot {
  return getMyDbStore().history()
}

/** Creates yesterday's report once when Aiso is running on a new local day. */
export function myDbEnsurePreviousDayReport(reference?: Date): MyDbDailyReport | null {
  return getMyDbStore().ensurePreviousDayReport(reference)
}

export function myDbRestoreGraphCheckpoint(checkpointId: string): MyDbGraphCheckpoint {
  return getMyDbStore().restoreGraphCheckpoint(checkpointId)
}

export async function myDbSetSourcePath(itemId: string, sourcePath: string): Promise<MyDbNode> {
  return getMyDbStore().setSourcePath(itemId, sourcePath)
}

export async function myDbExportCore(coreId: string, destinationParent: string): Promise<MyDbCoreExportResult> {
  return getMyDbStore().exportCore(coreId, destinationParent)
}

export async function myDbFileHistory(itemId: string): Promise<MyDbFileHistory> {
  return getMyDbStore().fileHistory(itemId)
}

export async function myDbCompareRevisions(
  itemId: string,
  beforeRevisionId: string,
  afterRevisionId: string
): Promise<MyDbTextDiff> {
  return getMyDbStore().compareRevisions(itemId, beforeRevisionId, afterRevisionId)
}

export async function myDbRestoreRevision(itemId: string, revisionId: string): Promise<MyDbNode> {
  return getMyDbStore().restoreRevision(itemId, revisionId)
}

export function myDbCreateCore(title: string, parentId?: string | null): MyDbNode {
  return getMyDbStore().createCore(title, parentId)
}

export async function myDbRenameNode(id: string, title: string): Promise<MyDbNode> {
  return getMyDbStore().renameNode(id, title)
}

export function myDbDeleteNode(id: string, options?: MyDbDeleteOptions): void {
  getMyDbStore().deleteNode(id, options)
}

/** 휴지통 항목 완전 삭제. 사용자 전용 — 에이전트 브리지(mydb_agent)에는 없다. */
export function myDbPurgeNode(id: string): Promise<void> {
  return getMyDbStore().purgeNode(id)
}

/**
 * 보관 기한이 지난 휴지통 항목의 기준 시각.
 *
 * `retentionDays` 가 0 이면 null — 자동 비우기를 하지 않는다는 뜻이고, 호출부는
 * 이 값이 null 이면 아무것도 지우지 않아야 한다. 0 을 '즉시 삭제'로 읽으면
 * 되돌릴 수 없는 동작이 사용자가 켜지도 않은 채로 돌아간다.
 *
 * 순수 계산이라 여기서 내보내 테스트로 고정한다 — 호출부가 같은 식을 따로
 * 갖고 있으면 한쪽만 바뀌어도 조용히 어긋난다.
 */
export function myDbTrashCutoff(retentionDays: number, reference: Date = new Date()): string | null {
  if (!Number.isFinite(retentionDays) || retentionDays <= 0) return null
  const cutoff = new Date(reference.getTime() - retentionDays * 24 * 60 * 60 * 1000)
  return cutoff.toISOString()
}

/**
 * 휴지통 비우기. `before` 가 없으면 전부, 있으면 그 시각 이전에 버려진 것만.
 * 사용자 전용 — 에이전트 브리지에는 없다.
 */
export function myDbPurgeTrash(before?: string | null): Promise<MyDbTrashPurgeResult> {
  return getMyDbStore().purgeTrash(before)
}

export function myDbRestoreNode(id: string): MyDbNode {
  return getMyDbStore().restoreNode(id)
}

export function myDbLink(sourceId: string, targetId: string, relation?: MyDbRelation): MyDbEdge {
  return getMyDbStore().link(sourceId, targetId, relation)
}

export function myDbUnlink(edgeId: string): void {
  getMyDbStore().unlink(edgeId)
}

export async function myDbImportDropped(paths: readonly string[], parentCoreId?: string | null): Promise<MyDbImportResult> {
  return getMyDbStore().importPaths(paths, parentCoreId)
}

export function myDbStorageRoot(): string {
  return getMyDbStore().root
}
