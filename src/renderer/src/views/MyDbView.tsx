import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { AppSettings } from '../../../shared/settings'
import type { DragEvent as ReactDragEvent, FormEvent } from 'react'
import type {
  MyDbEdge,
  MyDbFileHistory,
  MyDbHistoryAction,
  MyDbHistoryEntry,
  MyDbHistorySnapshot,
  MyDbNode,
  MyDbNodeKind,
  MyDbRevision,
  MyDbSnapshot,
  MyDbTextDiff,
  MyDbTrashSnapshot
} from '../../../shared/mydb'
import { CloseIcon, DownloadIcon, EditIcon, FileIcon, FolderIcon, GraphIcon, LinkIcon, SearchIcon, TrashIcon, UnlinkIcon } from '../components/icons'
import { confirmDialog } from '../components/ConfirmDialog'
import { getMyDbBridge } from '../lib/mydb'
import { buildMonth, countByDay, intensityOf, localDayKey, monthRange, monthsWithHistory, resolveReportDate, shiftMonth } from '../lib/history-calendar'
import { applyRepulsion, BARNES_HUT_THETA, buildQuadTree } from './mydb-graph/quadtree'
import { resolveCollisions } from './mydb-graph/collision'
import { buildGraphRoutes } from './mydb-graph/routing'

interface Props {
  active: boolean
  /** 휴지통 자동 비우기 기한을 화면이 그대로 말해 주기 위해 필요하다. */
  settings: AppSettings
}

/**
 * 휴지통 항목의 자동 삭제까지 남은 기한 표시.
 *
 * 기한이 꺼져 있으면(0일) 아무것도 세지 않고 버린 날짜만 말한다 — 켜지지도
 * 않은 기능의 카운트다운을 보여 주면 곧 지워질 것처럼 읽힌다.
 * 이미 지난 항목은 '곧 삭제'다. 다음 검사 주기에 사라지므로 '0일 남음'처럼
 * 정확한 척하지 않는다.
 */
export function trashLeftLabel(
  node: Pick<MyDbNode, 'deletedAt'>,
  retentionDays: number,
  reference: Date = new Date()
): string {
  if (!node.deletedAt) return ''
  const deletedAt = new Date(node.deletedAt)
  if (Number.isNaN(deletedAt.getTime())) return ''
  if (!Number.isFinite(retentionDays) || retentionDays <= 0) {
    return `${deletedAt.getMonth() + 1}/${deletedAt.getDate()} 버림`
  }
  const dueMs = deletedAt.getTime() + retentionDays * 24 * 60 * 60 * 1000
  const leftDays = Math.ceil((dueMs - reference.getTime()) / (24 * 60 * 60 * 1000))
  return leftDays <= 0 ? '곧 삭제' : `${leftDays}일 남음`
}

type MyDbViewMode = 'graph' | 'list' | 'history'
type MyDbExtensionFilter = 'all' | string

interface LibraryTreeRow {
  node: MyDbNode
  depth: number
}

interface Point {
  x: number
  y: number
}

interface Viewport {
  x: number
  y: number
  scale: number
}

interface CanvasNode {
  node: MyDbNode
  x: number
  y: number
  vx: number
  vy: number
  radius: number
  press: number
}

interface GraphDragState {
  nodeId: string | null
  pan: boolean
  pointerId: number | null
  last: Point
  moved: number
}

interface GraphRuntime {
  selectedId: string | null
  linkSourceId: string | null
  targetSelectionSourceId: string | null
  contextMenuNodeId: string | null
  onSelect: (id: string | null) => void
  onLinkTarget: (id: string) => void
  onOpenNode: (node: MyDbNode) => void
  onContextMenu: (nodeId: string | null, point: Point) => void
  onContextMenuAnchorChange: (nodeId: string, point: Point) => void
}

interface NodeMenuState {
  nodeId: string
  x: number
  y: number
}

interface CreateCoreState {
  x: number
  y: number
  parentCoreId: string | null
  title: string
}

interface CanvasMenuState {
  x: number
  y: number
  query: string
}

interface RenameState {
  node: MyDbNode
  title: string
}

interface DeleteState {
  node: MyDbNode
  cascade: boolean
  scopeExplicit?: boolean
}

interface VersionState {
  item: MyDbNode
  history: MyDbFileHistory | null
  diff: MyDbTextDiff | null
  loading: boolean
  error: string | null
}

interface RestoreRevisionState {
  item: MyDbNode
  revision: MyDbRevision
}

interface RestoreGraphState {
  entry: MyDbHistoryEntry
}

interface UnlinkPanelPosition {
  x: number
  y: number
}

const EMPTY_SNAPSHOT: MyDbSnapshot = { nodes: [], edges: [] }
const EMPTY_HISTORY: MyDbHistorySnapshot = { entries: [], dailyReports: [] }
const INITIAL_VIEWPORT: Viewport = { x: 0, y: 0, scale: 1 }
const POINTER_CLICK_SLOP = 4
const savedNodePositions = new Map<string, Point & { vx: number; vy: number }>()
let savedViewport: Viewport = { ...INITIAL_VIEWPORT }
// The original graph keeps a world-space gravity centre across focus changes
// and window resizes. The camera moves, not the settled universe.
let savedGraphCenter: Point | null = null

function formatDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('ko-KR', { year: 'numeric', month: 'short', day: 'numeric' }).format(date)
}

function formatHistoryTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('ko-KR', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  }).format(date)
}

function formatRevisionReason(reason: MyDbRevision['reason']): string {
  if (reason === 'initial') return '원본'
  if (reason === 'restored') return '복원본'
  if (reason === 'source_synced') return '외부 원본 반영'
  return '변경본'
}

/** '2026-08-21' → '8월 21일 (금)'. 연도는 달력 머리가 이미 말한다. */
function formatReportDate(reportDate: string): string {
  const at = new Date(`${reportDate}T00:00:00`)
  if (Number.isNaN(at.getTime())) return reportDate
  const weekday = ['일', '월', '화', '수', '목', '금', '토'][at.getDay()]
  return `${at.getMonth() + 1}월 ${at.getDate()}일 (${weekday})`
}

function historyActionLabel(action: MyDbHistoryAction): string {
  const labels: Record<MyDbHistoryAction, string> = {
    core_created: '코어 생성',
    imported: '자료 추가',
    renamed: '이름 변경',
    moved_to_trash: '휴지통 이동',
    restored: '복원',
    purged: '완전 삭제',
    linked: '연결',
    unlinked: '연결 해제',
    content_changed: '내용 변경',
    revision_restored: '버전 복원',
    source_synced: '원본 동기화',
    source_linked: '원본 연결',
    graph_restored: '그래프 복원',
    exported: '폴더 내보내기'
  }
  return labels[action]
}

/**
 * 태그와 제목만으로 전해지지 않는 것을 한마디로 덧붙인다.
 *
 * 예전에는 동작마다 완성된 문장을 만들었는데("…을(를) 추가했습니다"), 바로 위
 * 태그가 같은 말을 하고 있었고 조사 처리(을/를, 과/와)도 괄호로 노출됐다.
 * 대부분의 동작은 태그 + 제목으로 충분하므로 정말 필요한 것만 남긴다.
 */
function historyNote(entry: MyDbHistoryEntry): string {
  switch (entry.action) {
    case 'purged':
      return '되돌릴 수 없음'
    case 'content_changed':
      return '새 버전으로 보관'
    case 'revision_restored':
      return '이전 버전으로 되돌림'
    case 'source_synced':
      return '외부 원본과 동기화'
    default:
      return ''
  }
}

function formatSize(bytes?: number): string {
  if (bytes == null || bytes < 0) return '크기 정보 없음'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`
}

function nodeTypeLabel(kind: MyDbNodeKind): string {
  return kind === 'core' ? '코어' : '파일'
}

function fileTypeLabel(node: MyDbNode): string {
  if (node.kind === 'core') return '코어'
  const labels: Record<NonNullable<MyDbNode['fileType']>, string> = {
    markdown: '문서',
    document: '문서',
    slides: '프레젠테이션',
    spreadsheet: '스프레드시트',
    code: '코드',
    image: '이미지',
    archive: '압축 파일',
    other: '파일'
  }
  return labels[node.fileType ?? 'other']
}

function fileExtension(node: MyDbNode): string {
  if (node.kind !== 'file') return ''
  const dot = node.title.lastIndexOf('.')
  if (dot <= 0 || dot === node.title.length - 1) return '기타'
  return node.title.slice(dot + 1).toLocaleUpperCase('en-US')
}

export function buildLibraryTreeRows(
  nodes: MyDbNode[],
  edges: MyDbEdge[],
  query: string,
  extension: MyDbExtensionFilter,
  collapsedCoreIds: ReadonlySet<string> = new Set<string>()
): LibraryTreeRow[] {
  const byId = new Map(nodes.map((node) => [node.id, node]))
  const childCores = new Map<string, string[]>()
  const filesByCore = new Map<string, string[]>()
  const coreParent = new Map<string, string>()
  const fileOwner = new Map<string, string>()

  for (const edge of edges) {
    if (edge.relation !== 'contains') continue
    const source = byId.get(edge.sourceId)
    const target = byId.get(edge.targetId)
    if (source?.kind !== 'core' || !target) continue
    if (target.kind === 'core' && !coreParent.has(target.id)) {
      coreParent.set(target.id, source.id)
      childCores.set(source.id, [...(childCores.get(source.id) ?? []), target.id])
    }
    if (target.kind === 'file' && !fileOwner.has(target.id)) {
      fileOwner.set(target.id, source.id)
      filesByCore.set(source.id, [...(filesByCore.get(source.id) ?? []), target.id])
    }
  }

  const normalized = query.trim().toLocaleLowerCase('ko-KR')
  const matchesText = (node: MyDbNode): boolean => (
    !normalized || `${node.title} ${(node.tags ?? []).join(' ')}`.toLocaleLowerCase('ko-KR').includes(normalized)
  )
  const matchesFile = (node: MyDbNode): boolean => {
    if (node.kind !== 'file') return false
    if (node.kind === 'file' && extension !== 'all' && fileExtension(node) !== extension) return false
    return matchesText(node)
  }
  const sortIds = (ids: string[]): string[] => ids.slice().sort((left, right) => (
    (byId.get(left)?.title ?? '').localeCompare(byId.get(right)?.title ?? '', 'ko-KR')
  ))
  const visibleCache = new Map<string, boolean>()
  const visibleCore = (id: string, seen = new Set<string>()): boolean => {
    if (visibleCache.has(id)) return visibleCache.get(id) ?? false
    if (seen.has(id)) return false
    seen.add(id)
    const core = byId.get(id)
    const visible = Boolean(core && normalized && matchesText(core))
      || sortIds(filesByCore.get(id) ?? []).some((fileId) => {
        const file = byId.get(fileId)
        return Boolean(file && matchesFile(file))
      })
      || sortIds(childCores.get(id) ?? []).some((childId) => visibleCore(childId, seen))
    seen.delete(id)
    visibleCache.set(id, visible)
    return visible
  }

  const rows: LibraryTreeRow[] = []
  const visited = new Set<string>()
  const appendCore = (id: string, depth: number): void => {
    if (visited.has(id) || !visibleCore(id)) return
    const core = byId.get(id)
    if (!core || core.kind !== 'core') return
    visited.add(id)
    rows.push({ node: core, depth })
    if (collapsedCoreIds.has(id)) return
    // Keep a core's own files directly below it. Rendering nested cores first
    // makes these files look like loose root items after a long child section.
    for (const fileId of sortIds(filesByCore.get(id) ?? [])) {
      const file = byId.get(fileId)
      if (file?.kind === 'file' && matchesFile(file)) rows.push({ node: file, depth: depth + 1 })
    }
    for (const childId of sortIds(childCores.get(id) ?? [])) appendCore(childId, depth + 1)
  }

  const coreIds = nodes.filter((node) => node.kind === 'core').map((node) => node.id)
  const rootCoreIds = sortIds(coreIds.filter((id) => !coreParent.has(id)))
  const reachableFromRoot = new Set<string>()
  const queue = [...rootCoreIds]
  while (queue.length > 0) {
    const current = queue.shift() as string
    if (reachableFromRoot.has(current)) continue
    reachableFromRoot.add(current)
    queue.push(...(childCores.get(current) ?? []))
  }

  for (const id of rootCoreIds) appendCore(id, 0)
  // Only recover malformed cyclic/disconnected core groups here. A normal
  // descendant may be absent from rows because its parent folder is collapsed;
  // it must never be rendered again as a false top-level folder.
  for (const id of sortIds(coreIds.filter((id) => !reachableFromRoot.has(id)))) appendCore(id, 0)
  for (const file of nodes
    .filter((node) => node.kind === 'file' && !fileOwner.has(node.id) && matchesFile(node))
    .sort((left, right) => left.title.localeCompare(right.title, 'ko-KR'))) {
    rows.push({ node: file, depth: 0 })
  }
  return rows
}

function compactText(context: CanvasRenderingContext2D, value: string, maxWidth: number): string {
  if (context.measureText(value).width <= maxWidth) return value
  const suffix = '…'
  let end = value.length
  while (end > 1 && context.measureText(`${value.slice(0, end)}${suffix}`).width > maxWidth) end -= 1
  return `${value.slice(0, end)}${suffix}`
}

function pointFromEvent(event: PointerEvent | MouseEvent | WheelEvent, target: HTMLCanvasElement): Point {
  const rect = target.getBoundingClientRect()
  return { x: event.clientX - rect.left, y: event.clientY - rect.top }
}

function descendantsOf(sourceId: string, edges: MyDbEdge[]): Set<string> {
  const children = new Map<string, string[]>()
  for (const edge of edges) {
    const existing = children.get(edge.sourceId) ?? []
    existing.push(edge.targetId)
    children.set(edge.sourceId, existing)
  }
  const result = new Set<string>([sourceId])
  const queue = [sourceId]
  while (queue.length > 0) {
    const current = queue.shift() as string
    for (const child of children.get(current) ?? []) {
      if (!result.has(child)) {
        result.add(child)
        queue.push(child)
      }
    }
  }
  return result
}

function visibleCoreBranchIds(sourceId: string, nodes: MyDbNode[], edges: MyDbEdge[]): Set<string> {
  const nodesById = new Map(nodes.map((node) => [node.id, node]))
  const { children } = buildCoreGraphStructure(nodes, edges)
  const result = new Set<string>([sourceId])
  const queue = [sourceId]

  while (queue.length > 0) {
    const current = queue.shift() as string
    for (const childId of children.get(current) ?? []) {
      if (!result.has(childId)) {
        result.add(childId)
        queue.push(childId)
      }
    }
    for (const edge of edges) {
      const source = nodesById.get(edge.sourceId)
      const target = nodesById.get(edge.targetId)
      if (!source || !target) continue
      if (edge.sourceId === current && target.kind === 'file') result.add(target.id)
      if (edge.targetId === current && source.kind === 'file') result.add(source.id)
    }
  }

  return result
}

export interface CoreGraphStructure {
  children: Map<string, string[]>
  attachedFiles: Map<string, string[]>
  fileCounts: Map<string, number>
  subtreeSizes: Map<string, number>
  heights: Map<string, number>
  coreRadii: Map<string, number>
  structuralCoreEdgeIds: Set<string>
  primaryCoreFileEdgeIds: Set<string>
}

export function buildCoreGraphStructure(nodes: MyDbNode[], edges: MyDbEdge[]): CoreGraphStructure {
  const nodesById = new Map(nodes.map((node) => [node.id, node]))
  const children = new Map<string, Set<string>>()
  const attachedFiles = new Map<string, Set<string>>()
  const parentByChild = new Map<string, string>()
  const structuralCoreEdgeIds = new Set<string>()
  const primaryCoreFileEdgeIds = new Set<string>()

  const addChild = (coreId: string, childId: string): void => {
    const values = children.get(coreId) ?? new Set<string>()
    values.add(childId)
    children.set(coreId, values)
  }
  const addFile = (coreId: string, fileId: string): void => {
    const values = attachedFiles.get(coreId) ?? new Set<string>()
    values.add(fileId)
    attachedFiles.set(coreId, values)
  }

  const createsCycle = (parentId: string, childId: string): boolean => {
    const visited = new Set<string>()
    const queue = [childId]
    while (queue.length > 0) {
      const current = queue.shift() as string
      if (current === parentId) return true
      if (visited.has(current)) continue
      visited.add(current)
      for (const next of children.get(current) ?? []) queue.push(next)
    }
    return false
  }

  // Only `contains` is hierarchy. Ordinary connections must never reshape the
  // tree; that separation is what keeps the original My DB sectors readable.
  const coreEdges = edges
    .filter((edge) => (
      edge.relation === 'contains'
      && nodesById.get(edge.sourceId)?.kind === 'core'
      && nodesById.get(edge.targetId)?.kind === 'core'
    ))
    .sort((left, right) => left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id))
  for (const edge of coreEdges) {
    if (parentByChild.has(edge.targetId) || createsCycle(edge.sourceId, edge.targetId)) continue
    addChild(edge.sourceId, edge.targetId)
    parentByChild.set(edge.targetId, edge.sourceId)
    structuralCoreEdgeIds.add(edge.id)
  }

  // A file belongs to one primary visual cluster. Extra relationships are
  // still drawn, but do not pull the same file into two competing hubs.
  const fileEdges = edges
    .flatMap((edge) => {
      const source = nodesById.get(edge.sourceId)
      const target = nodesById.get(edge.targetId)
      if (source?.kind === 'core' && target?.kind === 'file') return [{ edge, coreId: source.id, fileId: target.id }]
      if (source?.kind === 'file' && target?.kind === 'core') return [{ edge, coreId: target.id, fileId: source.id }]
      return []
    })
    .sort((left, right) => {
      const rank = (edge: MyDbEdge): number => edge.relation === 'contains' ? 0 : 1
      return rank(left.edge) - rank(right.edge) || left.edge.createdAt.localeCompare(right.edge.createdAt) || left.edge.id.localeCompare(right.edge.id)
    })
  const primaryFileOwner = new Set<string>()
  for (const entry of fileEdges) {
    if (primaryFileOwner.has(entry.fileId)) continue
    primaryFileOwner.add(entry.fileId)
    addFile(entry.coreId, entry.fileId)
    primaryCoreFileEdgeIds.add(entry.edge.id)
  }

  const childLists = new Map([...children].map(([id, values]) => [id, [...values]]))
  const fileLists = new Map([...attachedFiles].map(([id, values]) => [id, [...values]]))
  const fileCounts = new Map<string, number>()
  const subtreeSizes = new Map<string, number>()
  const heights = new Map<string, number>()
  const visiting = new Set<string>()
  const countFiles = (coreId: string): number => {
    const cached = fileCounts.get(coreId)
    if (cached != null) return cached
    if (visiting.has(coreId)) return 0
    visiting.add(coreId)
    let count = (fileLists.get(coreId) ?? []).length
    for (const childId of childLists.get(coreId) ?? []) count += countFiles(childId)
    visiting.delete(coreId)
    fileCounts.set(coreId, count)
    return count
  }
  const visitingHeight = new Set<string>()
  const heightOf = (coreId: string): number => {
    const cached = heights.get(coreId)
    if (cached != null) return cached
    if (visitingHeight.has(coreId)) return 0
    visitingHeight.add(coreId)
    let height = 0
    for (const childId of childLists.get(coreId) ?? []) height = Math.max(height, 1 + heightOf(childId))
    visitingHeight.delete(coreId)
    heights.set(coreId, height)
    return height
  }
  const visitingSubtree = new Set<string>()
  const subtreeOf = (coreId: string): number => {
    const cached = subtreeSizes.get(coreId)
    if (cached != null) return cached
    if (visitingSubtree.has(coreId)) return 0
    visitingSubtree.add(coreId)
    let size = (fileLists.get(coreId) ?? []).length
    for (const childId of childLists.get(coreId) ?? []) size += 1 + subtreeOf(childId)
    visitingSubtree.delete(coreId)
    subtreeSizes.set(coreId, size)
    return size
  }
  const coreRadii = new Map<string, number>()
  for (const node of nodes) {
    if (node.kind !== 'core') continue
    countFiles(node.id)
    coreRadii.set(node.id, Math.min(44, 10 + heightOf(node.id) * 5 + Math.sqrt(subtreeOf(node.id)) * 1.4))
  }

  return {
    children: childLists,
    attachedFiles: fileLists,
    fileCounts,
    subtreeSizes,
    heights,
    coreRadii,
    structuralCoreEdgeIds,
    primaryCoreFileEdgeIds
  }
}

export interface GraphLayoutPlan {
  positions: Map<string, Point>
  coreTargets: Map<string, Point>
  fileSlots: Map<string, { coreId: string; angle: number; radius: number }>
  orphanTargets: Map<string, Point>
  structuralCoreEdgeIds: Set<string>
  primaryCoreFileEdgeIds: Set<string>
  secondaryEdgeIds: Set<string>
  center: Point
}

/** 덩어리 사이에 반드시 남기는 간격. 붙어 보이면 한 덩어리로 읽힌다. */
const CLUSTER_GAP = 46

/**
 * 흩어진 덩어리를 **하나의 둥근 뭉치**로 모은다.
 *
 * 화면 비율에 맞춰 가로로 펼쳐 봤더니 덩어리가 서로 멀어져 빈 자리만 넓어졌다.
 * 옵시디언 그래프처럼 전체가 한 덩어리로 뭉쳐 보이려면, 남는 자리를 채우는 게
 * 아니라 **가운데로 끌어모아야** 한다. 그래서 덩어리 원반을 원형으로 채운다.
 *
 * 덩어리는 통째로 평행이동만 한다. 강체 이동이라 덩어리 안의 기하(교차 0 ·
 * 관통 0 · 겹침 0)는 그대로이고, 덩어리끼리는 간선이 없으므로 새 교차도 생기지
 * 않는다. 원반이 서로 안 겹치므로 남의 덩어리를 뚫는 일도 없다.
 */
function packClustersIntoCircle(
  positions: Map<string, Point>,
  coreTargets: Map<string, Point>,
  orphanTargets: Map<string, Point>,
  nodes: MyDbNode[],
  edges: MyDbEdge[],
  coreRadii: Map<string, number>
): void {
  // 간선으로 이어진 것끼리 한 덩어리다(union-find).
  const parent = new Map<string, string>()
  const find = (id: string): string => {
    let root = id
    while ((parent.get(root) ?? root) !== root) root = parent.get(root) as string
    let cursor = id
    while ((parent.get(cursor) ?? cursor) !== cursor) {
      const next = parent.get(cursor) as string
      parent.set(cursor, root)
      cursor = next
    }
    return root
  }
  const union = (a: string, b: string): void => {
    const ra = find(a)
    const rb = find(b)
    if (ra !== rb) parent.set(ra, rb)
  }
  for (const node of nodes) parent.set(node.id, node.id)
  for (const edge of edges) {
    if (positions.has(edge.sourceId) && positions.has(edge.targetId)) union(edge.sourceId, edge.targetId)
  }

  const groups = new Map<string, string[]>()
  for (const node of nodes) {
    if (!positions.has(node.id)) continue
    const key = find(node.id)
    const list = groups.get(key)
    if (list) list.push(node.id)
    else groups.set(key, [node.id])
  }
  if (groups.size < 2) return

  const radiusOfNode = new Map(nodes.map((node) => [
    node.id,
    node.kind === 'core' ? coreRadii.get(node.id) ?? 10 : 6
  ] as const))

  interface ClusterDisc { key: string; cx: number; cy: number; radius: number; members: string[] }
  const discs: ClusterDisc[] = []
  for (const [key, members] of groups) {
    let minX = Infinity
    let minY = Infinity
    let maxX = -Infinity
    let maxY = -Infinity
    for (const id of members) {
      const point = positions.get(id) as Point
      const r = radiusOfNode.get(id) ?? 10
      minX = Math.min(minX, point.x - r)
      minY = Math.min(minY, point.y - r)
      maxX = Math.max(maxX, point.x + r)
      maxY = Math.max(maxY, point.y + r)
    }
    const cx = (minX + maxX) / 2
    const cy = (minY + maxY) / 2
    // 덩어리를 감싸는 최소 반지름. 상자 대각선이 아니라 실제 노드까지의 거리로
    // 잡아야 원반이 헐렁해지지 않는다.
    let radius = 0
    for (const id of members) {
      const point = positions.get(id) as Point
      radius = Math.max(radius, Math.hypot(point.x - cx, point.y - cy) + (radiusOfNode.get(id) ?? 10))
    }
    discs.push({ key, cx, cy, radius, members })
  }

  // 큰 덩어리부터 가운데에 놓고, 나머지는 **원점에 가장 가까운 빈자리**에 붙인다.
  // 큰 것을 먼저 놓아야 작은 것이 틈을 메우며 전체가 둥글게 찬다.
  const order = [...discs].sort((left, right) => (
    right.radius - left.radius || left.key.localeCompare(right.key)
  ))
  const placed: { x: number; y: number; radius: number }[] = []
  const spot = new Map<string, { x: number; y: number }>()

  for (const disc of order) {
    if (placed.length === 0) {
      spot.set(disc.key, { x: 0, y: 0 })
      placed.push({ x: 0, y: 0, radius: disc.radius })
      continue
    }
    let best: { x: number; y: number } | null = null
    let bestDistance = Infinity
    // 이미 놓인 원반마다, 그 둘레를 따라 붙일 자리를 훑는다. 0.5° 간격이면
    // 눈에 띄는 틈이 남지 않고, 표본이 고정이라 결과도 늘 같다.
    const STEPS = 720
    for (const anchor of placed) {
      const distance = anchor.radius + disc.radius + CLUSTER_GAP
      for (let step = 0; step < STEPS; step += 1) {
        const angle = (step / STEPS) * Math.PI * 2
        const x = anchor.x + Math.cos(angle) * distance
        const y = anchor.y + Math.sin(angle) * distance
        let free = true
        for (const other of placed) {
          if (Math.hypot(x - other.x, y - other.y) < other.radius + disc.radius + CLUSTER_GAP - 0.5) {
            free = false
            break
          }
        }
        if (!free) continue
        // 원점에서 가장 가까운 자리를 고른다 — 그래야 전체가 원으로 뭉친다.
        const fromCentre = Math.hypot(x, y) + disc.radius
        if (fromCentre < bestDistance) {
          bestDistance = fromCentre
          best = { x, y }
        }
      }
    }
    const target = best ?? { x: 0, y: 0 }
    spot.set(disc.key, target)
    placed.push({ x: target.x, y: target.y, radius: disc.radius })
  }

  for (const disc of discs) {
    const target = spot.get(disc.key)
    if (!target) continue
    const dx = target.x - disc.cx
    const dy = target.y - disc.cy
    if (dx === 0 && dy === 0) continue
    for (const id of disc.members) {
      const point = positions.get(id) as Point
      positions.set(id, { x: point.x + dx, y: point.y + dy })
      const core = coreTargets.get(id)
      if (core) coreTargets.set(id, { x: core.x + dx, y: core.y + dy })
      const orphan = orphanTargets.get(id)
      if (orphan) orphanTargets.set(id, { x: orphan.x + dx, y: orphan.y + dy })
    }
  }
}


export function createInitialLayout(
  nodes: MyDbNode[],
  edges: MyDbEdge[],
  width: number,
  height: number,
  gravityCenter?: Point
): GraphLayoutPlan {
  const positions = new Map<string, Point>()
  const coreTargets = new Map<string, Point>()
  const fileSlots = new Map<string, { coreId: string; angle: number; radius: number }>()
  const orphanTargets = new Map<string, Point>()
  const coreNodes = nodes.filter((node) => node.kind === 'core')
  const coreIds = coreNodes.map((node) => node.id)
  const { children, attachedFiles, coreRadii, structuralCoreEdgeIds, primaryCoreFileEdgeIds } = buildCoreGraphStructure(nodes, edges)
  const childCores = new Set<string>()
  for (const childIds of children.values()) for (const childId of childIds) childCores.add(childId)

  const roots = coreIds.filter((id) => !childCores.has(id))
  const center = gravityCenter ?? { x: width / 2, y: height / 2 }
  const collator = new Intl.Collator('ko-KR', { numeric: true, sensitivity: 'base' })
  const titleById = new Map(nodes.map((node): [string, string] => [node.id, node.title]))
  const sorted = (ids: readonly string[]): string[] => [...ids].sort((left, right) => (
    collator.compare(titleById.get(left) ?? '', titleById.get(right) ?? '') || left.localeCompare(right)
  ))

  const full = Math.PI * 2
  const startAngle = -Math.PI / 2

  // 화면이 실제로 그리는 치수. 여기서 벗어나면 '겹치지 않게 쟀다'가 거짓말이 된다.
  const FILE_NODE_RADIUS = 6
  /** 코어 테두리에서 파일 고리까지 띄우는 거리. */
  const FILE_RING_GAP = 46
  /** 파일 하나가 고리에서 차지하는 호 길이. */
  const FILE_ARC = 22
  /** 원반(코어+파일 고리) 바깥에 두는 여백. 이만큼은 아무도 못 들어온다. */
  const DISC_PAD = 10
  /** 부모 원반과 자식 부분트리 사이 최소 간격. */
  const RING_MARGIN = 56
  /**
   * 부모로 돌아가는 선이 지날 자리로 자식이 비워 두는 반각(라디안).
   *
   * 자식 **수와 무관한 상수**다. 한 바퀴에서 딱 한 번 빠지므로 자식이 늘어도 요구가
   * 늘지 않는다. 자식 하나마다 고정 각을 떼어 주면 자식이 2π/각 명을 넘는 순간
   * 요구 합이 영영 한 바퀴를 넘어서고, 거리 탐색이 천장까지 달려가 노드가 천만 px
   * 밖으로 날아간다(실제로 겪은 사고다). 그래서 '자식당'이 아니라 '한 번'이다.
   */
  const PARENT_RESERVE = 0.12
  /** 원뿔의 합이 창에서 차지할 수 있는 최대 비율. 나머지는 형제 사이 틈 몫으로 남긴다. */
  const CONE_SHARE = 0.78
  /** 원뿔 하나의 반각 상한. 직각을 넘으면 원뿔이 볼록하지 않아 안의 선분이 밖으로 샌다. */
  const HALF_CONE_CAP = Math.PI * 0.49
  /**
   * '고르게 펴려는 욕심'이 거리를 밀어낼 수 있는 최대 배수.
   *
   * 겹치지 않을 최소 거리는 기하학적 하한이다. 그 몇 배 안으로 잠가 두면 크기는
   * 언제나 하한을 따라가고, 어떤 입력에서도 발산할 길이 없다.
   */
  const SPREAD_CLAMP = 4

  const parentOf = new Map<string, string>()
  for (const [parentId, childIds] of children) for (const childId of childIds) parentOf.set(childId, parentId)

  const subtreeCounts = new Map<string, number>()
  const visitingSubtree = new Set<string>()
  const subtreeOf = (id: string): number => {
    const cached = subtreeCounts.get(id)
    if (cached != null) return cached
    if (visitingSubtree.has(id)) return 0
    visitingSubtree.add(id)
    let count = (attachedFiles.get(id) ?? []).length
    for (const childId of children.get(id) ?? []) count += 1 + subtreeOf(childId)
    visitingSubtree.delete(id)
    subtreeCounts.set(id, count)
    return count
  }
  for (const id of coreIds) subtreeOf(id)

  // ── 원반(disc): 코어 하나가 자기 파일 고리까지 합쳐 실제로 먹는 자리 ──
  // 사용자가 말한 "보이지 않는 더 큰 충돌 원"이 이것이다. 배치가 각을 나눌 때
  // 자식 '수'가 아니라 이 원들을 재기 때문에, 파일이 많은 코어는 저절로 더 넓은
  // 자리를 받고 이웃과 절대 겹치지 않는다. DISC_PAD 만큼 실제 노드보다 크게 잡으므로
  // 원 밖의 점은 그 안의 어떤 노드와도 최소 그만큼 떨어져 있다 — 스쳐도 관통이 아니다.
  const orderedFiles = new Map<string, string[]>()
  const fileRingRadius = new Map<string, number>()
  const discRadius = new Map<string, number>()
  for (const id of coreIds) {
    const files = sorted(attachedFiles.get(id) ?? [])
    orderedFiles.set(id, files)
    const coreRadius = coreRadii.get(id) ?? 10
    if (files.length === 0) {
      fileRingRadius.set(id, 0)
      discRadius.set(id, coreRadius + DISC_PAD)
      continue
    }
    const ring = Math.max(coreRadius + FILE_RING_GAP, (files.length * FILE_ARC) / full)
    fileRingRadius.set(id, ring)
    discRadius.set(id, ring + FILE_NODE_RADIUS + DISC_PAD)
  }
  const discOf = (id: string): number => discRadius.get(id) ?? (10 + DISC_PAD)

  /**
   * 좁은 틈부터 물을 채우듯 여유 각을 부어, **가장 넓은 틈을 가장 좁게** 만든다.
   *
   * 화면에서 '부채꼴로 몰렸다'는 인상은 자식 중심 사이의 **제일 큰 틈** 하나가 정한다
   * (품질 자도 360°에서 그 틈을 뺀 값을 폭으로 센다). 남는 각을 형제마다 똑같이 나눠 주면
   * 원래 좁던 틈은 좁은 채로 남아 그 인상이 안 바뀐다. 수위를 하나로 맞춰야 한다.
   *
   * mins 의 합이 total 을 넘으면 부을 여유가 없다는 뜻이라 그대로 돌려준다 — 거리를
   * 그런 일이 없도록 고르므로 실제로는 오지 않는 길이고, 와도 불변식은 안 깨진다.
   */
  const waterFill = (mins: readonly number[], total: number): number[] => {
    const count = mins.length
    if (count === 0) return []
    let sum = 0
    for (const value of mins) sum += value
    if (sum >= total) return [...mins]
    const ascending = mins.map((_, index) => index).sort((left, right) => (
      ((mins[left] as number) - (mins[right] as number)) || (left - right)
    ))
    const suffix = new Array<number>(count + 1).fill(0)
    for (let k = count - 1; k >= 0; k -= 1) {
      suffix[k] = (suffix[k + 1] as number) + (mins[ascending[k] as number] as number)
    }
    let level = 0
    for (let k = count; k >= 1; k -= 1) {
      const candidate = (total - (suffix[k] as number)) / k
      if (candidate >= (mins[ascending[k - 1] as number] as number)) {
        level = candidate
        break
      }
    }
    return mins.map((value) => Math.max(value, level))
  }

  /**
   * 부분트리 하나를 **국소 좌표에 미리 조립해 둔** 결과.
   *
   * 원점은 이 부분트리의 뿌리, +x 는 부모의 반대(바깥) 방향이다. 위층은 이 좌표계를
   * 회전·평행이동만 하므로, 아래에서 조립한 모양이 위에서 글자 그대로 다시 쓰인다.
   * 크기를 재는 모델과 실제로 놓는 모델이 **같다** — 겹치지 않음을 보장하는 근거가
   * 바로 이 일치다. (예전 시도가 깨진 곳도 여기였다.)
   */
  interface SubtreeShape {
    /** 부분트리 전 노드의 국소 좌표. */
    offsets: Map<string, Point>
    /** 부분트리가 실제로 먹는 원들. 부모는 자식 '수'가 아니라 이 원들로 각을 잰다. */
    circles: Array<{ x: number; y: number; r: number }>
    /** 원점에서 잰 바깥 끝. 덩어리끼리 떼어 놓을 때만 쓴다. */
    radius: number
  }

  /** 트리가 아닌 입력(순환·다중 부모)에서도 같은 노드를 두 번 놓지 않게 막는다. */
  const claimed = new Set<string>()

  /**
   * 부분트리를 아래에서 위로 조립한다 — **부채꼴이 아니라 원뿔**로.
   *
   * 예전 배치는 자식을 부모에게 물려받은 부채꼴 안에서만 폈다. 그래야 형제 영역이
   * 겹치지 않아 선이 안 꼬이지만, 갈래질 때마다 각이 반씩 잘려 깊은 내부 노드는
   * 구조적으로 부채꼴이 된다(실측 157~193°). 부채꼴은 교차를 막기에 **충분하지만
   * 필요하지는 않다**.
   *
   * 여기서는 각 자식 부분트리를 부모에서 본 **원뿔**(방향각 ± 반각)로 잡는다.
   *   · 원뿔이 서로 배타적이면 그 안의 원들도 서로 떨어져 있다.
   *   · 부모→자식 선은 그 원뿔의 축이므로 남의 원뿔에 들어갈 수 없다.
   *   · 자식 쪽에서 그 선은 뒤쪽(-x)으로 뻗는 반직선이다. 자식이 그 방향으로
   *     PARENT_RESERVE 만큼만 비워 두면 손자와도 만나지 않는다.
   *   · 원은 노드 반지름에 DISC_PAD 를 더해 잡으므로, 원 밖의 점은 그 안의 어떤
   *     노드와도 최소 DISC_PAD 떨어져 있다 — 스치듯 접해도 관통이 아니다.
   * 각 구간만 갈라 두면 되니 자식은 부모 **둘레 어디에나** 앉을 수 있다.
   *
   * 원뿔을 '자식을 감싸는 원반의 반지름'이 아니라 **조립해 둔 모양 자체**로 재는 것이
   * 핵심이다. 원반으로 뭉뚱그리면 곧게 뻗은 가지도 뚱뚱한 공으로 취급되어 깊이마다
   * 크기가 배로 불어난다. 원뿔로 재면 곧은 가지는 가늘어 가까이 붙는다.
   */
  const layoutSubtree = (id: string, isRoot: boolean): SubtreeShape => {
    claimed.add(id)
    const ownRadius = discOf(id)
    const offsets = new Map<string, Point>([[id, { x: 0, y: 0 }]])
    const circles: Array<{ x: number; y: number; r: number }> = [{ x: 0, y: 0, r: ownRadius }]
    const kids = sorted(children.get(id) ?? []).filter((childId) => !claimed.has(childId))
    if (kids.length === 0) return { offsets, circles, radius: ownRadius }
    const subs = kids.map((childId) => layoutSubtree(childId, false))
    const count = kids.length

    // 자식 모양의 **어떤 원도** 내 원반(코어+파일 고리)을 건드리지 않는 최소 거리.
    // 모양을 직접 재므로 필요한 만큼만 밀어낸다.
    const clearance = ownRadius + RING_MARGIN
    const near = subs.map((sub) => {
      let need = 1
      for (const circle of sub.circles) {
        const reach = clearance + circle.r
        const inside = reach * reach - circle.y * circle.y
        const distance = -circle.x + (inside > 0 ? Math.sqrt(inside) : 0)
        if (distance > need) need = distance
      }
      return need
    })

    /** 거리 distance 에 놓았을 때 이 부분트리가 부모에서 차지하는 반각. */
    const halfConeAt = (index: number, distance: number): number => {
      const sub = subs[index] as SubtreeShape
      let half = 0
      for (const circle of sub.circles) {
        const px = circle.x + distance
        const length = Math.hypot(px, circle.y)
        if (length <= circle.r) return Math.PI * 0.99
        const towards = Math.abs(Math.atan2(circle.y, px))
        const spread = Math.asin(Math.min(0.999, circle.r / length))
        if (towards + spread > half) half = towards + spread
      }
      return Math.min(Math.PI * 0.99, half)
    }
    const halvesAt = (scale: number): number[] => kids.map(
      (_, index) => halfConeAt(index, scale * (near[index] as number))
    )

    // 뿌리가 아니면 부모로 돌아가는 자리를 양쪽 PARENT_RESERVE 만큼 비운다.
    const window = isRoot ? full : full - 2 * PARENT_RESERVE

    /**
     * (필수) 원뿔의 합이 창에 들어가고, 하나하나가 **직각 안**인가.
     *
     * 합 조건은 형제끼리 안 겹치게 한다. 직각 조건은 그보다 미묘하다 — 반각이 90°를
     * 넘으면 원뿔이 볼록하지 않아, 그 안의 두 점을 이은 선분이 꼭짓점 근처로 빠져나가
     * **원뿔 밖으로 나간다**. 실제로 그래서 청강대 수업→2학년 선이 2학년 안쪽의
     * 1학기→게임 기획 선과 한 번 교차했다. 90° 안으로 잡으면 원뿔이 볼록해지고,
     * 볼록한 영역은 자기 안의 선분을 전부 품으므로 부분트리의 **모든 간선**이
     * 자기 원뿔 안에 갇힌다. 그래야 교차 0 이 노드뿐 아니라 선까지 보장된다.
     */
    const fits = (halves: readonly number[]): boolean => {
      let sum = 0
      for (const half of halves) {
        if (half > HALF_CONE_CAP) return false
        sum += 2 * half
      }
      return sum <= window * CONE_SHARE
    }
    /**
     * (희망) 이웃한 두 원뿔이 중심 간격 2π/n 안에 들어가는가.
     *
     * 들어가면 물채우기가 모든 중심 간격을 2π/n 으로 맞출 수 있고, 그때 자식은
     * 부모를 정확히 빙 두른다. 자식이 늘수록 목표가 2π/n 로 **같이 좁아지므로**
     * 거리 요구는 자식 수에 선형이다 — 고정 각을 떼어 주다 발산했던 그 함정이 없다.
     */
    const even = (halves: readonly number[]): boolean => {
      const target = full / count
      for (let index = 0; index < count; index += 1) {
        const next = (index + 1) % count
        if ((halves[index] as number) + (halves[next] as number) > target) return false
      }
      return true
    }
    /** accept 를 만족하는 가장 작은 배율. 반각은 거리에 대해 단조 감소해 0 으로 간다. */
    const scaleFor = (accept: (halves: readonly number[]) => boolean): number => {
      if (accept(halvesAt(1))) return 1
      let low = 1
      let high = 2
      for (let step = 0; step < 48 && !accept(halvesAt(high)); step += 1) high *= 2
      for (let step = 0; step < 40; step += 1) {
        const mid = (low + high) / 2
        if (accept(halvesAt(mid))) high = mid
        else low = mid
      }
      return high
    }
    const needScale = scaleFor(fits)
    const wishScale = scaleFor((halves) => fits(halves) && even(halves))
    // 욕심은 필수치의 SPREAD_CLAMP 배 안에 가둔다. 이 한 줄이 발산을 원천 봉쇄한다 —
    // 필수치는 기하학적 하한이라, 그 몇 배 안이면 크기는 언제나 하한을 따라간다.
    const scale = Math.min(Math.max(needScale, wishScale), needScale * SPREAD_CLAMP)

    const distances = kids.map((_, index) => scale * (near[index] as number))
    const halves = kids.map((_, index) => halfConeAt(index, distances[index] as number))

    // 중심 사이 최소 각. 마지막 칸이 부모 쪽으로 열린 틈이라 예약분을 더 얹는다.
    const mins: number[] = []
    for (let index = 0; index < count; index += 1) {
      const next = (index + 1) % count
      const back = !isRoot && index === count - 1 ? 2 * PARENT_RESERVE : 0
      mins.push((halves[index] as number) + (halves[next] as number) + back)
    }
    const gaps = waterFill(mins, full)
    const spread = (widths: readonly number[]): number[] => {
      const angles: number[] = []
      // 부모로 돌아가는 선은 마지막 틈 안을 지난다. 그 틈을 **반씩 나누면 안 된다** —
      // 양옆 원뿔의 폭이 다르면 넓은 쪽이 선을 덮는다. 실제로 그래서 청강대 수업→2학년
      // 선이 1학기 안쪽 간선과 한 번 교차했다(1학기 원뿔 85° · 남은 자리 90°).
      // 각자 자기 폭과 예약분을 먼저 챙기고, 남는 만큼만 절반씩 더 가져간다.
      const slack = Math.max(0, (widths[count - 1] as number) - (mins[count - 1] as number))
      const back = isRoot ? 0 : (halves[0] as number) + PARENT_RESERVE + slack / 2
      let cursor = isRoot ? startAngle : Math.PI + back
      for (let index = 0; index < count; index += 1) {
        if (index > 0) cursor += widths[index - 1] as number
        angles.push(cursor)
      }
      return angles
    }

    /**
     * 넓힌 결과를 **믿지 않고 확인한다**.
     *
     * 원뿔이 서로 겹치거나 부모로 돌아갈 자리를 침범하면 참이다. 참이면 최소치로
     * 되돌린다 — 최소치는 합이 창보다 좁다는 것이 보장되어 있어 반드시 안전하다.
     * 가정이 아니라 검사라서, 앞의 계산이 어긋나도 교차 0 은 안 깨진다.
     */
    const conflicts = (angles: readonly number[]): boolean => {
      for (let i = 0; i < count; i += 1) {
        const ai = angles[i] as number
        const hi = halves[i] as number
        if (!isRoot) {
          let back = Math.abs(ai - Math.PI) % full
          if (back > Math.PI) back = full - back
          if (back < hi + PARENT_RESERVE) return true
        }
        for (let j = i + 1; j < count; j += 1) {
          let delta = Math.abs(ai - (angles[j] as number)) % full
          if (delta > Math.PI) delta = full - delta
          if (delta < hi + (halves[j] as number)) return true
        }
      }
      return false
    }

    // 최소치는 합이 창보다 좁다는 게 보장돼 있어 언제나 안전하다. 넓힌 배치가 검사를
    // 통과하지 못하면 군말 없이 그리로 되돌린다 — 그 노드만 조금 좁아질 뿐이다.
    let angles = spread(gaps)
    if (conflicts(angles)) angles = spread(mins)

    let radius = ownRadius
    kids.forEach((_, index) => {
      const angle = angles[index] as number
      const distance = distances[index] as number
      const cos = Math.cos(angle)
      const sin = Math.sin(angle)
      const sub = subs[index] as SubtreeShape
      // 자식 좌표를 +x 로 distance 만큼 민 뒤 방향각만큼 돌린다. 자식의 '바깥'이 그대로
      // 부모에서 멀어지는 방향이 되므로, 자식이 비워 둔 뒤쪽이 정확히 부모→자식 선 자리다.
      const shift = (point: { x: number; y: number }): Point => {
        const x = point.x + distance
        return { x: x * cos - point.y * sin, y: x * sin + point.y * cos }
      }
      for (const [descendantId, point] of sub.offsets) offsets.set(descendantId, shift(point))
      for (const circle of sub.circles) {
        const moved = shift(circle)
        circles.push({ x: moved.x, y: moved.y, r: circle.r })
        radius = Math.max(radius, Math.hypot(moved.x, moved.y) + circle.r)
      }
    })
    return { offsets, circles, radius }
  }

  interface ClusterPlan {
    rootId: string
    offsets: Map<string, Point>
    radius: number
  }

  const layoutCluster = (rootId: string): ClusterPlan => {
    const shape = layoutSubtree(rootId, true)
    return { rootId, offsets: shape.offsets, radius: Math.max(shape.radius, discOf(rootId)) }
  }

  const structuralRoots = sorted((roots.length > 0 ? roots : coreIds).filter((id) => subtreeOf(id) > 0))
  const clusters = structuralRoots.map(layoutCluster)

  // ── 뿌리가 여럿이면 각 덩어리를 하나의 원반으로 보고 바깥 원에 나눠 앉힌다 ──
  // 덩어리끼리 원반이 겹치지 않으면 덩어리 사이 선도 만날 수 없다.
  const origins = new Map<string, Point>()
  let extent = 0
  if (clusters.length === 1) {
    origins.set((clusters[0] as ClusterPlan).rootId, center)
    extent = (clusters[0] as ClusterPlan).radius
  } else if (clusters.length > 1) {
    const largest = clusters.reduce((max, cluster) => Math.max(max, cluster.radius), 0)
    const ringGutter = 34
    const spanAt = (perimeter: number): number => clusters.reduce(
      (sum, cluster) => sum + 2 * Math.asin(Math.min(0.999, cluster.radius / perimeter)) + ringGutter / perimeter,
      0
    )
    const budget = full * 0.985
    let perimeter = Math.max(largest * 1.02, 1)
    while (perimeter < largest * 4096 && spanAt(perimeter) > budget) perimeter *= 1.5
    let low = perimeter / 1.5
    let high = perimeter
    if (low >= largest * 1.02 && spanAt(low) > budget) {
      for (let step = 0; step < 44; step += 1) {
        const mid = (low + high) / 2
        if (spanAt(mid) <= budget) high = mid
        else low = mid
      }
    }
    perimeter = high
    const spans = clusters.map((cluster) => 2 * Math.asin(Math.min(0.999, cluster.radius / perimeter)) + ringGutter / perimeter)
    const used = spans.reduce((sum, value) => sum + value, 0)
    const extra = Math.max(0, full - used) / clusters.length
    let cursor = startAngle
    clusters.forEach((cluster, index) => {
      const slot = (spans[index] as number) + extra
      const angle = cursor + slot / 2
      const origin = { x: center.x + Math.cos(angle) * perimeter, y: center.y + Math.sin(angle) * perimeter }
      origins.set(cluster.rootId, origin)
      extent = Math.max(extent, perimeter + cluster.radius)
      cursor += slot
    })
  }

  for (const cluster of clusters) {
    const origin = origins.get(cluster.rootId) ?? center
    for (const [id, offset] of cluster.offsets) {
      const point = { x: origin.x + offset.x, y: origin.y + offset.y }
      coreTargets.set(id, point)
      positions.set(id, point)
    }
  }

  // ── 파일 고리 ──
  // 파일은 코어를 빙 둘러 360° 로 앉는다(그래야 방사형으로 읽힌다). 다만
  // 부모·자식으로 나가는 구조선이 지나는 방향만 비워 둔다 — 그 좁은 창만
  // 피하면 선이 파일을 뚫는 일이 사라지고, 퍼짐은 거의 그대로 남는다.
  for (const coreId of coreIds) {
    const files = orderedFiles.get(coreId) ?? []
    if (files.length === 0) continue
    const corePosition = coreTargets.get(coreId)
    if (!corePosition) continue
    const ring = fileRingRadius.get(coreId) ?? 60
    const blocked: number[] = []
    const push = (otherId: string | undefined): void => {
      if (!otherId) return
      const other = coreTargets.get(otherId)
      if (!other) return
      const dx = other.x - corePosition.x
      const dy = other.y - corePosition.y
      if (dx === 0 && dy === 0) return
      blocked.push(Math.atan2(dy, dx))
    }
    push(parentOf.get(coreId))
    for (const childId of children.get(coreId) ?? []) push(childId)

    const halfWindow = Math.asin(Math.min(0.6, (FILE_NODE_RADIUS + 4) / ring))
    const emit = (fileId: string, angle: number): void => {
      fileSlots.set(fileId, { coreId, angle, radius: ring })
      positions.set(fileId, {
        x: corePosition.x + Math.cos(angle) * ring,
        y: corePosition.y + Math.sin(angle) * ring
      })
    }

    // 1순위: 완전 균등한 고리를 통째로 **돌려서** 구조선이 지나는 방향을 비켜 간다.
    // 파일 간격을 손대지 않으므로 퍼짐이 1 에 가깝게 유지된다.
    const step = full / files.length
    let bestOffset = 0
    let bestClearance = -1
    const samples = 512
    for (let sample = 0; sample < samples; sample += 1) {
      const offset = (step * sample) / samples
      let worst = Math.PI
      for (const angle of blocked) {
        const rest = (((angle - startAngle - offset) % step) + step) % step
        const distance = Math.min(rest, step - rest)
        if (distance < worst) worst = distance
      }
      if (worst > bestClearance + 1e-9) {
        bestClearance = worst
        bestOffset = offset
      }
    }
    if (bestClearance >= halfWindow) {
      files.forEach((fileId, index) => emit(fileId, startAngle + bestOffset + step * index))
      continue
    }

    // 2순위: 균등 고리로는 창을 못 피한다. 막힌 창을 뺀 열린 호에 길이 비례로 나눈다.
    const normalized = blocked
      .map((angle) => ((angle - startAngle) % full + full) % full)
      .sort((left, right) => left - right)
    let arcs: Array<{ from: number; length: number }> = []
    if (normalized.length === 0) {
      arcs = [{ from: 0, length: full }]
    } else {
      for (let index = 0; index < normalized.length; index += 1) {
        const from = (normalized[index] as number) + halfWindow
        const next = (normalized[(index + 1) % normalized.length] as number) - halfWindow
        const length = ((next - from) % full + full) % full
        if (length > halfWindow * 0.5) arcs.push({ from, length })
      }
    }
    const openLength = arcs.reduce((sum, arc) => sum + arc.length, 0)
    if (arcs.length === 0 || openLength <= 0) arcs = [{ from: 0, length: full }]
    const total = arcs.reduce((sum, arc) => sum + arc.length, 0)

    // 최대 잉여법으로 정수 배분한다 — 같은 입력이면 언제나 같은 배분이다.
    const quota = arcs.map((arc) => (arc.length / total) * files.length)
    const counts = quota.map((value) => Math.floor(value))
    let remaining = files.length - counts.reduce((sum, value) => sum + value, 0)
    const byRemainder = quota
      .map((value, index) => ({ index, rest: value - Math.floor(value) }))
      .sort((left, right) => right.rest - left.rest || left.index - right.index)
    for (const entry of byRemainder) {
      if (remaining <= 0) break
      counts[entry.index] = (counts[entry.index] as number) + 1
      remaining -= 1
    }

    let cursor = 0
    arcs.forEach((arc, arcIndex) => {
      const count = counts[arcIndex] as number
      for (let slot = 0; slot < count; slot += 1) {
        const fileId = files[cursor] as string
        cursor += 1
        emit(fileId, startAngle + arc.from + (arc.length * (slot + 0.5)) / count)
      }
    })
  }

  // ── 아무 데도 매이지 않은 노드 ──
  // 배치의 바깥 테두리 너머에 둔다. 안쪽에 끼워 넣으면 남의 선을 가로지른다.
  const orphanIds = sorted(nodes.filter((node) => !positions.has(node.id)).map((node) => node.id))
  if (orphanIds.length > 0) {
    const orphanRadius = Math.max(96, extent + 78, (orphanIds.length * 38) / full)
    orphanIds.forEach((id, index) => {
      const angle = startAngle + (full * index) / orphanIds.length
      const point = { x: center.x + Math.cos(angle) * orphanRadius, y: center.y + Math.sin(angle) * orphanRadius }
      orphanTargets.set(id, point)
      positions.set(id, point)
    })
  }

  // ── 흩어진 덩어리를 하나의 둥근 뭉치로 모은다 ──
  packClustersIntoCircle(positions, coreTargets, orphanTargets, nodes, edges, coreRadii)

  const secondaryEdgeIds = new Set(
    edges
      .filter((edge) => (
        !structuralCoreEdgeIds.has(edge.id)
        // A file may use a legacy `related` edge as its radial visual owner,
        // but that does not turn a user-made connection into a structural
        // line. Only the selected `contains` attachment stays a primary line.
        && !(primaryCoreFileEdgeIds.has(edge.id) && edge.relation === 'contains')
      ))
      .map((edge) => edge.id)
  )
  return {
    positions,
    coreTargets,
    fileSlots,
    orphanTargets,
    structuralCoreEdgeIds,
    primaryCoreFileEdgeIds,
    secondaryEdgeIds,
    center
  }
}

interface GraphCanvasProps {
  nodes: MyDbNode[]
  edges: MyDbEdge[]
  layoutKey: string
  recenterToken: number
  focusCoreId: string | null
  focusZoomToken: number
  selectedId: string | null
  linkSourceId: string | null
  targetSelectionSourceId: string | null
  contextMenuNodeId: string | null
  dropActive: boolean
  onSelect: (id: string | null) => void
  onLinkTarget: (id: string) => void
  onOpenNode: (node: MyDbNode) => void
  onContextMenu: (nodeId: string | null, point: Point) => void
  onContextMenuAnchorChange: (nodeId: string, point: Point) => void
}

function MyDbGraphCanvas({
  nodes,
  edges,
  layoutKey,
  recenterToken,
  focusCoreId,
  focusZoomToken,
  selectedId,
  linkSourceId,
  targetSelectionSourceId,
  contextMenuNodeId,
  dropActive,
  onSelect,
  onLinkTarget,
  onOpenNode,
  onContextMenu,
  onContextMenuAnchorChange
}: GraphCanvasProps): React.JSX.Element {
  const hostRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const nodesRef = useRef<CanvasNode[]>([])
  const viewportRef = useRef<Viewport>({ ...savedViewport })
  const hoveredIdRef = useRef<string | null>(null)
  const sizeRef = useRef({ width: 1, height: 1, dpr: 1 })
  const frameRef = useRef<number | null>(null)
  const layoutKeyRef = useRef<string | null>(null)
  const layoutSignatureRef = useRef<string | null>(null)
  const linkPointerRef = useRef<Point | null>(null)
  const wakeRef = useRef<(() => void) | null>(null)
  const recenterRef = useRef<(() => void) | null>(null)
  const focusRef = useRef<(() => void) | null>(null)
  const runtimeRef = useRef<GraphRuntime>({
    selectedId,
    linkSourceId,
    targetSelectionSourceId,
    contextMenuNodeId,
    onSelect,
    onLinkTarget,
    onOpenNode,
    onContextMenu,
    onContextMenuAnchorChange
  })
  runtimeRef.current = {
    selectedId,
    linkSourceId,
    targetSelectionSourceId,
    contextMenuNodeId,
    onSelect,
    onLinkTarget,
    onOpenNode,
    onContextMenu,
    onContextMenuAnchorChange
  }
  const dragRef = useRef<GraphDragState>({
    nodeId: null,
    pan: false,
    pointerId: null,
    last: { x: 0, y: 0 },
    moved: 0
  })

  // UI selection and context menus should redraw the existing simulation, not
  // tear it down and build a new graph. The standalone My DB does the same.
  useEffect(() => {
    wakeRef.current?.()
  }, [contextMenuNodeId, linkSourceId, selectedId, targetSelectionSourceId])

  useEffect(() => {
    recenterRef.current?.()
  }, [recenterToken])

  useEffect(() => {
    focusRef.current?.()
  }, [focusZoomToken])

  useEffect(() => {
    const host = hostRef.current
    const canvas = canvasRef.current
    const context = canvas?.getContext('2d')
    if (!host || !canvas || !context) return

    // A focus change only changes the visible branch. Keep the world cache and
    // camera intact so Esc/back returns to the same living graph, like the
    // standalone My DB System.
    const focusChanged = layoutKeyRef.current !== null && layoutKeyRef.current !== layoutKey
    layoutKeyRef.current = layoutKey
    const layoutSignature = [
      ...nodes.map((node) => node.id).sort(),
      ...edges.map((edge) => `${edge.id}:${edge.sourceId}>${edge.targetId}:${edge.relation}`).sort()
    ].join('|')
    const topologyChanged = layoutSignatureRef.current !== null && layoutSignatureRef.current !== layoutSignature
    layoutSignatureRef.current = layoutSignature

    let disposed = false
    let initialized = false
    // '전체 보기' 요청. 배치가 준비된 첫 프레임에 한 번 수행한다.
    //
    // 처음부터 true 다. 화면에 들어올 때 recenter 를 부르는 효과가 이 그래프 효과보다
    // **먼저** 도는 경우가 있어서, 그때는 recenterRef 가 아직 비어 있어 요청이 통째로
    // 사라진다. 실측으로 배율이 맞춰지지 않고 1.0 인 채로 남아 배치가 화면 밖으로
    // 넘쳤다. 배치를 새로 만들었으면 어차피 전체를 보여 주는 게 맞으므로 여기서 켠다.
    let pendingFit = true
    let wake: () => void = () => undefined
    let alpha = savedNodePositions.size === 0
      ? 0.5
      : focusChanged
        ? 0.18
        : topologyChanged
          ? 0.12
          : 0.04
    let kineticEnergy = 1
    let layoutPlan: GraphLayoutPlan = {
      positions: new Map(),
      coreTargets: new Map(),
      fileSlots: new Map(),
      orphanTargets: new Map(),
      structuralCoreEdgeIds: new Set(),
      primaryCoreFileEdgeIds: new Set(),
      secondaryEdgeIds: new Set(),
      center: savedGraphCenter ?? { x: 0, y: 0 }
    }
      let palette = {
        surface: '#17191f',
        core: '#9ba3ad',
        file: '#5b6470',
        edge: '#7d8792',
        selected: '#4f5965',
        label: 'rgba(233, 236, 242, .86)',
        dim: 'rgba(233, 236, 242, .25)',
        linking: '#5fc69f'
    }

    const readPalette = (): void => {
      const styles = window.getComputedStyle(host)
      const read = (name: string, fallback: string): string => styles.getPropertyValue(name).trim() || fallback
      palette = {
        surface: read('--mydb-canvas-bg', palette.surface),
        core: read('--mydb-core', palette.core),
        file: read('--mydb-file', palette.file),
        edge: read('--mydb-edge', palette.edge),
        selected: read('--mydb-selected', palette.selected),
        label: read('--mydb-label', palette.label),
        dim: read('--mydb-dim', palette.dim),
        linking: read('--ok', palette.linking)
      }
    }

    const resize = (): void => {
      const rect = host.getBoundingClientRect()
      const dpr = window.devicePixelRatio || 1
      const width = Math.max(1, Math.floor(rect.width))
      const height = Math.max(1, Math.floor(rect.height))
      const previousSize = sizeRef.current
      sizeRef.current = { width, height, dpr }
      canvas.width = Math.floor(width * dpr)
      canvas.height = Math.floor(height * dpr)
      canvas.style.width = `${width}px`
      canvas.style.height = `${height}px`
      context.setTransform(dpr, 0, 0, dpr, 0, 0)
      if (!savedGraphCenter && width >= 64 && height >= 64) {
        savedGraphCenter = { x: width / 2, y: height / 2 }
      }
      layoutPlan = createInitialLayout(nodes, edges, width, height, savedGraphCenter ?? { x: width / 2, y: height / 2 })
      if (!initialized) {
        const layout = layoutPlan.positions
        const previous = new Map(nodesRef.current.map((node) => [node.node.id, node]))
        const { coreRadii } = buildCoreGraphStructure(nodes, edges)
        nodesRef.current = nodes.map((node) => {
          const existing = previous.get(node.id)
          const cached = savedNodePositions.get(node.id)
          const initial = existing ?? cached ?? layout.get(node.id) ?? { x: width / 2, y: height / 2 }
          return {
            node,
            x: initial.x,
            y: initial.y,
            vx: existing?.vx ?? cached?.vx ?? 0,
            vy: existing?.vy ?? cached?.vy ?? 0,
            press: existing?.press ?? 0,
            radius: node.kind === 'core' ? (coreRadii.get(node.id) ?? 10) : 6
          }
        })
        initialized = true
      } else if (previousSize.width > 1 && previousSize.height > 1) {
        // Keep the same world point under the canvas centre on resize. Replacing
        // every node here was the cause of the visible jump in windowed mode.
        viewportRef.current = {
          ...viewportRef.current,
          x: viewportRef.current.x + (width - previousSize.width) / 2,
          y: viewportRef.current.y + (height - previousSize.height) / 2
        }
        savedViewport = { ...viewportRef.current }
        alpha = Math.max(alpha, 0.08)
      }
      wake()
    }

    const toWorld = (point: Point): Point => {
      const viewport = viewportRef.current
      return { x: (point.x - viewport.x) / viewport.scale, y: (point.y - viewport.y) / viewport.scale }
    }

    const hitTest = (point: Point): CanvasNode | null => {
      const world = toWorld(point)
      const scale = viewportRef.current.scale
      const { targetSelectionSourceId } = runtimeRef.current
      let closest: CanvasNode | null = null
      let closestDistance = Number.POSITIVE_INFINITY
      for (const node of nodesRef.current) {
        const distance = Math.hypot(node.x - world.x, node.y - world.y)
        // During a relationship action, targets must be selectable by both
        // their dot and their visible filename. Small file nodes are otherwise
        // too easy to miss on a dense graph.
        const labelWidth = node.node.kind === 'core' ? 138 : 104
        const labelHit = targetSelectionSourceId !== null
          && world.x >= node.x - labelWidth / 2
          && world.x <= node.x + labelWidth / 2
          && world.y >= node.y + node.radius + 2
          && world.y <= node.y + node.radius + 24
        if ((distance <= node.radius + 8 / scale || labelHit) && distance < closestDistance) {
          closest = node
          closestDistance = distance
        }
      }
      return closest
    }

    const layoutTargetFor = (item: CanvasNode, nodeById: Map<string, CanvasNode>): Point | null => {
      const coreTarget = layoutPlan.coreTargets.get(item.node.id)
      if (coreTarget) return coreTarget
      const fileSlot = layoutPlan.fileSlots.get(item.node.id)
      if (fileSlot) {
        const owner = nodeById.get(fileSlot.coreId)
        if (owner) {
          return {
            x: owner.x + Math.cos(fileSlot.angle) * fileSlot.radius,
            y: owner.y + Math.sin(fileSlot.angle) * fileSlot.radius
          }
        }
      }
      return layoutPlan.orphanTargets.get(item.node.id) ?? null
    }

    let visualMotion = false

    const draw = (): void => {
      const { width, height } = sizeRef.current
      const viewport = viewportRef.current
      const runtime = runtimeRef.current
      const nodeById = new Map(nodesRef.current.map((node) => [node.node.id, node]))
      const contextMenuNode = runtime.contextMenuNodeId ? nodeById.get(runtime.contextMenuNodeId) : null
      if (contextMenuNode && runtime.contextMenuNodeId) {
        runtime.onContextMenuAnchorChange(runtime.contextMenuNodeId, {
          x: contextMenuNode.x * viewport.scale + viewport.x,
          y: contextMenuNode.y * viewport.scale + viewport.y
        })
      }
      const focusId = hoveredIdRef.current ?? runtime.selectedId ?? runtime.targetSelectionSourceId
      const focusSet = focusId ? descendantsOf(focusId, edges) : null
      context.clearRect(0, 0, width, height)
      context.fillStyle = palette.surface
      context.fillRect(0, 0, width, height)
      context.save()
      context.translate(viewport.x, viewport.y)
      context.scale(viewport.scale, viewport.scale)
      context.lineCap = 'round'
      context.lineJoin = 'round'
      context.lineWidth = 1 / viewport.scale
      visualMotion = false

      // Keep the structural tree readable first. Secondary relationships are
      // rendered afterwards through a stable, least-crossing route so they do
      // not cut through the centre of the hierarchy.
      const routes = buildGraphRoutes(
        edges,
        nodesRef.current.map((item) => ({
          id: item.node.id,
          x: item.x,
          y: item.y,
          radius: item.radius
        })),
        layoutPlan.secondaryEdgeIds,
        layoutPlan.center
      )
      const orderedEdges = [...edges].sort((left, right) => {
        const secondaryOrder = Number(!layoutPlan.secondaryEdgeIds.has(left.id)) - Number(!layoutPlan.secondaryEdgeIds.has(right.id))
        return secondaryOrder || left.id.localeCompare(right.id)
      })
      for (const edge of orderedEdges) {
        const source = nodeById.get(edge.sourceId)
        const target = nodeById.get(edge.targetId)
        const route = routes.get(edge.id)
        if (!source || !target || !route) continue
        const relevant = !focusSet || (focusSet.has(source.node.id) && focusSet.has(target.node.id))
        context.globalAlpha = relevant
          ? (route.secondary ? 0.44 : 0.92)
          : (route.secondary ? 0.08 : 0.16)
        context.strokeStyle = palette.edge
        context.lineWidth = (route.secondary ? 0.9 : 1.15) / viewport.scale
        context.beginPath()
        context.moveTo(route.start.x, route.start.y)
        if (route.control) context.quadraticCurveTo(route.control.x, route.control.y, route.end.x, route.end.y)
        else if (route.waypoints) {
          for (const waypoint of route.waypoints) context.lineTo(waypoint.x, waypoint.y)
          context.lineTo(route.end.x, route.end.y)
        }
        else context.lineTo(route.end.x, route.end.y)
        context.stroke()
        if (layoutPlan.structuralCoreEdgeIds.has(edge.id)) {
          // A small source marker makes the parent → child direction readable
          // without restoring arrowheads or adding visual weight to the graph.
          context.globalAlpha = relevant ? 0.94 : 0.2
          context.fillStyle = palette.edge
          context.beginPath()
          context.arc(route.start.x, route.start.y, 2.7 / viewport.scale, 0, Math.PI * 2)
          context.fill()
        }
      }

      const linkingSource = runtime.linkSourceId ? nodeById.get(runtime.linkSourceId) : null
      if (linkingSource && linkPointerRef.current) {
        context.globalAlpha = 0.82
        context.strokeStyle = palette.linking
        context.lineWidth = 1.8 / viewport.scale
        context.setLineDash([7 / viewport.scale, 6 / viewport.scale])
        context.beginPath()
        context.moveTo(linkingSource.x, linkingSource.y)
        context.lineTo(linkPointerRef.current.x, linkPointerRef.current.y)
        context.stroke()
        context.setLineDash([])
        context.fillStyle = palette.linking
        context.beginPath()
        context.arc(linkPointerRef.current.x, linkPointerRef.current.y, 3.4 / viewport.scale, 0, Math.PI * 2)
        context.fill()
      }

      for (const item of nodesRef.current) {
        const id = item.node.id
        const relevant = !focusSet || focusSet.has(id)
        const isSelected = runtime.selectedId === id
        const isLinkSource = runtime.targetSelectionSourceId === id
        const isHovered = hoveredIdRef.current === id
        const pressTarget = dragRef.current.nodeId === id ? 1 : 0
        item.press += (pressTarget - item.press) * 0.24
        if (Math.abs(pressTarget - item.press) > 0.01) visualMotion = true
        const drawRadius = item.radius * (1 + item.press * 0.055)
        context.globalAlpha = relevant ? 1 : 0.19
        if (isSelected) {
          // Keep the selected ring separate from the core so the gap remains
          // legible even when the core grows with its linked file count.
          context.fillStyle = palette.selected
          context.globalAlpha = relevant ? 0.1 : 0.03
          context.beginPath()
          context.arc(item.x, item.y, drawRadius + 18 / viewport.scale, 0, Math.PI * 2)
          context.fill()
          context.strokeStyle = palette.selected
          context.lineWidth = 3.5 / viewport.scale
          context.globalAlpha = relevant ? 0.72 : 0.18
          context.beginPath()
          context.arc(item.x, item.y, drawRadius + 8 / viewport.scale, 0, Math.PI * 2)
          context.stroke()
          context.globalAlpha = relevant ? 1 : 0.19
        } else if (isLinkSource || isHovered) {
          context.fillStyle = isLinkSource ? palette.linking : palette.edge
          context.globalAlpha = relevant ? 0.18 : 0.08
          context.beginPath()
          context.arc(item.x, item.y, drawRadius + 8 / viewport.scale, 0, Math.PI * 2)
          context.fill()
          context.globalAlpha = relevant ? 1 : 0.19
        }
        context.fillStyle = item.node.kind === 'core' ? palette.core : palette.file
        context.beginPath()
        context.arc(item.x, item.y, drawRadius, 0, Math.PI * 2)
        context.fill()
        if (item.node.kind === 'core') {
          context.lineWidth = 1 / viewport.scale
          context.strokeStyle = palette.edge
          context.stroke()
        }
        const showLabel = item.node.kind === 'core' || viewport.scale > 1.08 || isHovered || isSelected || runtime.targetSelectionSourceId !== null
        if (showLabel) {
          context.globalAlpha = relevant ? 1 : 0.16
          context.fillStyle = isSelected ? palette.core : palette.label
          context.font = `${item.node.kind === 'core' ? 12 : 10.5}px Pretendard, "Noto Sans KR", sans-serif`
          context.textAlign = 'center'
          context.textBaseline = 'top'
          const label = compactText(context, item.node.title, item.node.kind === 'core' ? 138 : 104)
          context.fillText(label, item.x, item.y + drawRadius + 6)
        }
      }
      context.restore()
      context.globalAlpha = 1
    }

    // These are the original My DB force values.  The graph has no manual
    // “return to old coordinates” step: fixed nodes are only fixed while the
    // pointer is held, then the same forces pull the structure home.
    const REPULSION = 2150
    const SPRING = 0.04
    const STRUCTURAL_SPRING = 0.012
    const STRUCTURAL_LENGTH = 240
    const RELATED_LENGTH = 95
    const GRAVITY = 0.009
    const CORE_PULL = 0.038
    const FILE_PULL = 0.047
    const DAMPING = 0.85
    const MAX_VELOCITY = 40
    const THETA_SQUARED = BARNES_HUT_THETA * BARNES_HUT_THETA
    // 보이는 원 바깥으로 확보하는 여유. 라벨이 붙는 노드가 서로 붙어 보이지 않게 한다.
    const COLLISION_PADDING = 7
    // 셋 이상이 뭉친 자리는 한 번에 안 풀린다. 프레임마다 도는 값이라 크게 두지 않는다.
    const COLLISION_ITERATIONS = 2
    // 1 이면 즉시 떨어지지만 튀어 보인다. 몇 프레임에 걸쳐 부드럽게 민다.
    const COLLISION_STRENGTH = 0.62

    const simulate = (fixedNodeId: string | null = null): number => {
      const graphNodes = nodesRef.current
      if (graphNodes.length === 0) return 0
      const nodeById = new Map(graphNodes.map((node) => [node.node.id, node]))
      const tree = buildQuadTree(graphNodes)
      if (tree) {
        for (const item of graphNodes) {
          applyRepulsion(tree, item, REPULSION, alpha, THETA_SQUARED)
        }
      }

      for (const edge of edges) {
        const source = nodeById.get(edge.sourceId)
        const target = nodeById.get(edge.targetId)
        if (!source || !target) continue
        const structuralCoreEdge = layoutPlan.structuralCoreEdgeIds.has(edge.id)
        const sourceIsCore = source.node.kind === 'core'
        const targetIsCore = target.node.kind === 'core'

        // Original My DB keeps every core ↔ file relationship in a radial
        // slot, regardless of its direction or relation label. A spring here
        // would pull files across sectors as soon as a file has a second
        // connection.
        if (sourceIsCore !== targetIsCore) continue

        // `contains` is the only structural core hierarchy. Other core
        // relationships remain visible as secondary curves but must not alter
        // the physical sector layout.
        if ((sourceIsCore || targetIsCore) && !structuralCoreEdge) continue
        const dx = target.x - source.x
        const dy = target.y - source.y
        const distance = Math.max(1, Math.hypot(dx, dy))
        // 구조 간선의 자연 길이는 **배치가 정한 거리**다.
        //
        // 예전에는 240px 고정이었다. 방사형 배치는 깊이마다 고리 반지름이 다른데
        // 모든 구조 간선을 같은 길이로 끌어당기면 고리가 무너진다. 실측으로,
        // 초기 배치는 관통 0·거의 겹치는 선 0 인데 물리를 거치면 각각 2 건이
        // 생겼다 — 1학기에서 나가는 두 선이 1.8° 로 붙어 2학년 원을 29.9px
        // 파고들었다. 용수철이 배치와 싸운 결과다. 계획 거리를 쓰면 용수철이
        // 배치를 무너뜨리는 대신 붙잡아 준다.
        const plannedSource = layoutPlan.coreTargets.get(edge.sourceId)
        const plannedTarget = layoutPlan.coreTargets.get(edge.targetId)
        const plannedDistance = plannedSource && plannedTarget
          ? Math.hypot(plannedTarget.x - plannedSource.x, plannedTarget.y - plannedSource.y)
          : null
        const restLength = structuralCoreEdge
          ? plannedDistance ?? STRUCTURAL_LENGTH + source.radius + target.radius
          : RELATED_LENGTH
        const spring = structuralCoreEdge ? STRUCTURAL_SPRING : SPRING
        const force = (distance - restLength) * spring * alpha
        const ux = dx / distance
        const uy = dy / distance
        source.vx += ux * force
        source.vy += uy * force
        target.vx -= ux * force
        target.vy -= uy * force
      }

      let energy = 0
      const center = { x: sizeRef.current.width / 2, y: sizeRef.current.height / 2 }
      for (const item of graphNodes) {
        if (item.node.id === fixedNodeId) {
          item.vx = 0
          item.vy = 0
          continue
        }
        const target = layoutTargetFor(item, nodeById)
        if (target) {
          const pull = item.node.kind === 'core' ? CORE_PULL : FILE_PULL
          item.vx += (target.x - item.x) * pull * alpha
          item.vy += (target.y - item.y) * pull * alpha
        } else {
          item.vx += (center.x - item.x) * GRAVITY * alpha
          item.vy += (center.y - item.y) * GRAVITY * alpha
        }
        item.vx *= DAMPING
        item.vy *= DAMPING
        item.vx = Math.max(-MAX_VELOCITY, Math.min(MAX_VELOCITY, item.vx))
        item.vy = Math.max(-MAX_VELOCITY, Math.min(MAX_VELOCITY, item.vy))
        item.x += item.vx
        item.y += item.vy
        energy += Math.abs(item.vx) + Math.abs(item.vy)
      }
      // 속도를 적분한 **뒤** 겹침을 좌표로 직접 푼다.
      //
      // 위의 Barnes-Hut 반발력은 1/r² 이고 alpha 로 감쇠하므로, 배치가 식으면
      // 사실상 0 이 되어 겹친 채 멈춘 화면이 남는다. 여기서 미는 것은 힘이 아니라
      // 위치라서 alpha 와 무관하게 항상 듣는다.
      //
      // 그리는 반지름보다 COLLISION_PADDING 만큼 큰 원을 쓴다 — 라벨이 차지하는
      // 자리를 감안하고, 여유가 있어야 군집이 동그랗게 뭉친다.
      resolveCollisions(graphNodes, {
        padding: COLLISION_PADDING,
        iterations: COLLISION_ITERATIONS,
        strength: COLLISION_STRENGTH,
        // 사용자가 끌고 있는 노드는 손에서 벗어나면 안 된다.
        isPinned: (body) => (body as CanvasNode).node.id === fixedNodeId
      })
      kineticEnergy = energy
      if (alpha > 0.03) alpha *= 0.998
      return energy
    }

    const SETTLE_ENERGY = Math.max(0.5, nodes.length * 0.03)
    let idleFrames = 0

    const tick = (): void => {
      frameRef.current = null
      if (disposed) return
      if (pendingFit && layoutPlan.positions.size > 0 && nodesRef.current.length > 0) {
        pendingFit = false
        fitToContent()
      }
      const fixedNodeId = dragRef.current.nodeId
      simulate(fixedNodeId)
      draw()
      const isActive = fixedNodeId !== null
        || dragRef.current.pan
        || visualMotion
        || kineticEnergy > SETTLE_ENERGY
      if (isActive) idleFrames = 45
      else idleFrames -= 1
      if (idleFrames > 0) frameRef.current = window.requestAnimationFrame(tick)
    }

    wake = (): void => {
      idleFrames = 45
      if (frameRef.current == null) frameRef.current = window.requestAnimationFrame(tick)
    }
    wakeRef.current = wake
    // 배치가 끝난 **뒤에** 재야 한다. 이 함수는 화면에 들어오는 순간 불리는데,
    // 그 시점에 노드가 아직 안 만들어졌거나 이전 그래프의 좌표가 남아 있을 수 있다.
    // 실제로 같은 코드가 실행마다 다른 배율을 내는 경쟁이 있었다. 한 프레임 미뤄
    // 항상 같은 순서로 재게 한다.
    const fitToContent = (): void => {
      const { width, height } = sizeRef.current
      // **배치 계획**을 잰다 — 살아 움직이는 좌표가 아니라.
      //
      // 물리는 화면에 들어온 뒤로도 몇 초간 노드를 바깥으로 퍼뜨린다. 그 좌표를
      // 재면 언제 재느냐에 따라 배율이 달라져, 같은 그래프가 실행마다 다르게
      // 잡히고 가장자리가 잘렸다(실측: 두 번 연속 실행이 서로 다른 화면).
      // 계획은 결정적이고 물리가 향하는 목표이므로, 이걸 재면 항상 같은 결과다.
      const radiusById = new Map(nodesRef.current.map((item) => [item.node.id, item.radius]))
      let minX = Infinity
      let minY = Infinity
      let maxX = -Infinity
      let maxY = -Infinity
      for (const [id, point] of layoutPlan.positions) {
        const r = radiusById.get(id) ?? 10
        minX = Math.min(minX, point.x - r)
        minY = Math.min(minY, point.y - r)
        maxX = Math.max(maxX, point.x + r)
        maxY = Math.max(maxY, point.y + r)
      }
      const hasBounds = Number.isFinite(minX) && maxX > minX && maxY > minY
      // 라벨이 원 밖으로 나가고, 물리가 자리를 잡는 동안 계획보다 조금 더 퍼진다.
      const MARGIN = 220
      const spanX = hasBounds ? maxX - minX + MARGIN : 1
      const spanY = hasBounds ? maxY - minY + MARGIN : 1
      // 작은 라이브러리를 확대해 띄우지는 않는다(0.58 = 예전의 차분한 기본 배율).
      const scale = hasBounds
        ? Math.max(0.12, Math.min(0.58, Math.min(width / spanX, height / spanY)))
        : 0.58
      const focusX = hasBounds ? (minX + maxX) / 2 : layoutPlan.center.x
      const focusY = hasBounds ? (minY + maxY) / 2 : layoutPlan.center.y
      viewportRef.current = {
        scale,
        x: width / 2 - focusX * scale,
        y: height / 2 - focusY * scale
      }
      savedViewport = { ...viewportRef.current }
      wake()
    }
    recenterRef.current = (): void => {
      // 여기서 바로 재면 안 된다 — 이 함수는 화면에 들어오는 순간 불리는데, 그때
      // layoutPlan 은 아직 빈 초기값이고 nodesRef 도 비어 있을 수 있다. 그 상태로
      // 재면 배율이 기본값으로 튀거나 실행마다 달라진다(실측으로 두 번 연속 실행이
      // 서로 다른 화면을 냈다). 배치가 준비된 첫 프레임에 tick 이 대신 맞춘다.
      pendingFit = true
      wake()
    }
    focusRef.current = (): void => {
      if (!focusCoreId) return
      const focusedNode = nodesRef.current.find((node) => node.node.id === focusCoreId)
      if (focusedNode) {
        focusedNode.x = layoutPlan.center.x
        focusedNode.y = layoutPlan.center.y
        focusedNode.vx = 0
        focusedNode.vy = 0
      }
      const { width, height } = sizeRef.current
      const scale = Math.max(viewportRef.current.scale, 1.16)
      viewportRef.current = {
        scale,
        x: width / 2 - layoutPlan.center.x * scale,
        y: height / 2 - layoutPlan.center.y * scale
      }
      savedViewport = { ...viewportRef.current }
      alpha = Math.max(alpha, 0.2)
      wake()
    }

    const redraw = (): void => {
      draw()
      if (visualMotion) wake()
    }

    const onPointerDown = (event: PointerEvent): void => {
      if (event.button !== 0) return
      const point = pointFromEvent(event, canvas)
      const hit = hitTest(point)
      const runtime = runtimeRef.current
      if (runtime.targetSelectionSourceId) {
        if (runtime.linkSourceId) linkPointerRef.current = toWorld(point)
        if (hit && hit.node.id !== runtime.targetSelectionSourceId) runtime.onLinkTarget(hit.node.id)
        wake()
        return
      }
      canvas.setPointerCapture(event.pointerId)
      // Do not focus a node merely because the user starts dragging it. The
      // selection is committed only after a click-sized pointer-up below.
      dragRef.current = {
        nodeId: hit?.node.id ?? null,
        pan: !hit,
        pointerId: event.pointerId,
        last: point,
        moved: 0
      }
      canvas.style.cursor = hit ? 'grabbing' : 'grabbing'
      // The grabbed node is held by the pointer only. Other nodes stay live
      // and react; after release its sector/slot force pulls it home.
      if (hit) {
        const item = nodesRef.current.find((node) => node.node.id === hit.node.id)
        if (item) {
          item.vx = 0
          item.vy = 0
        }
        alpha = Math.max(alpha, 0.3)
      }
      wake()
    }

    const onPointerMove = (event: PointerEvent): void => {
      const point = pointFromEvent(event, canvas)
      const runtime = runtimeRef.current
      if (runtime.targetSelectionSourceId) {
        if (runtime.linkSourceId) linkPointerRef.current = toWorld(point)
        const hit = hitTest(point)
        const nextHovered = hit?.node.id ?? null
        if (hoveredIdRef.current !== nextHovered) {
          hoveredIdRef.current = nextHovered
          canvas.style.cursor = hit && hit.node.id !== runtime.targetSelectionSourceId ? 'pointer' : 'crosshair'
        }
        wake()
        return
      }
      const drag = dragRef.current
      if (drag.pointerId === event.pointerId && (drag.nodeId || drag.pan)) {
        const dx = point.x - drag.last.x
        const dy = point.y - drag.last.y
        drag.moved += Math.abs(dx) + Math.abs(dy)
        if (drag.nodeId) {
          const item = nodesRef.current.find((node) => node.node.id === drag.nodeId)
          if (item) {
            const world = toWorld(point)
            item.x = world.x
            item.y = world.y
            item.vx = 0
            item.vy = 0
          }
        } else {
          viewportRef.current = {
            ...viewportRef.current,
            x: viewportRef.current.x + dx,
            y: viewportRef.current.y + dy
          }
          savedViewport = { ...viewportRef.current }
        }
        drag.last = point
        redraw()
        return
      }
      const hit = hitTest(point)
      const nextHovered = hit?.node.id ?? null
      if (hoveredIdRef.current !== nextHovered) {
        hoveredIdRef.current = nextHovered
        canvas.style.cursor = hit ? 'pointer' : 'grab'
        redraw()
      }
    }

    const finishPointer = (event: PointerEvent): void => {
      const drag = dragRef.current
      if (drag.pointerId !== event.pointerId) return
      if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId)
      const wasClick = event.type === 'pointerup' && drag.moved <= POINTER_CLICK_SLOP
      if (wasClick) runtimeRef.current.onSelect(drag.nodeId)
      const releasedNodeId = drag.nodeId
      dragRef.current = {
        nodeId: null,
        pan: false,
        pointerId: null,
        last: { x: 0, y: 0 },
        moved: 0
      }
      if (releasedNodeId) alpha = Math.max(alpha, 0.3)
      canvas.style.cursor = hoveredIdRef.current ? 'pointer' : 'grab'
      wake()
    }

    const onDoubleClick = (event: MouseEvent): void => {
      const hit = hitTest(pointFromEvent(event, canvas))
      if (hit) runtimeRef.current.onOpenNode(hit.node)
    }

    const onContext = (event: MouseEvent): void => {
      event.preventDefault()
      const point = pointFromEvent(event, canvas)
      runtimeRef.current.onContextMenu(hitTest(point)?.node.id ?? null, point)
      wake()
    }

    const onWheel = (event: WheelEvent): void => {
      event.preventDefault()
      const point = pointFromEvent(event, canvas)
      const before = viewportRef.current
      const multiplier = event.deltaY < 0 ? 1.12 : 1 / 1.12
      // Large core graphs need a wider overview range than the original
      // close-up editor view. Keep the upper limit unchanged for readable
      // node work, but allow the user to zoom out far enough to frame clusters.
      const scale = Math.max(0.18, Math.min(3.5, before.scale * multiplier))
      viewportRef.current = {
        scale,
        x: point.x - ((point.x - before.x) / before.scale) * scale,
        y: point.y - ((point.y - before.y) / before.scale) * scale
      }
      savedViewport = { ...viewportRef.current }
      wake()
    }

    const clearHover = (): void => {
      if (dragRef.current.nodeId || dragRef.current.pan || hoveredIdRef.current === null) return
      hoveredIdRef.current = null
      canvas.style.cursor = 'grab'
      wake()
    }

    readPalette()
    resize()
    const observer = new ResizeObserver(resize)
    observer.observe(host)
    const themeObserver = new MutationObserver(() => {
      readPalette()
      redraw()
    })
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    canvas.addEventListener('pointerdown', onPointerDown)
    canvas.addEventListener('pointermove', onPointerMove)
    canvas.addEventListener('pointerup', finishPointer)
    canvas.addEventListener('pointercancel', finishPointer)
    canvas.addEventListener('dblclick', onDoubleClick)
    canvas.addEventListener('contextmenu', onContext)
    canvas.addEventListener('wheel', onWheel, { passive: false })
    canvas.addEventListener('pointerleave', clearHover)
    window.addEventListener('blur', clearHover)
    return () => {
      disposed = true
      observer.disconnect()
      themeObserver.disconnect()
      if (frameRef.current != null) window.cancelAnimationFrame(frameRef.current)
      for (const item of nodesRef.current) {
        savedNodePositions.set(item.node.id, { x: item.x, y: item.y, vx: item.vx, vy: item.vy })
      }
      // Only the full graph knows which entries have truly been deleted; a
      // focused branch intentionally hides siblings that still need caching.
      if (layoutKey === 'all') {
        const liveIds = new Set(nodes.map((node) => node.id))
        for (const id of savedNodePositions.keys()) {
          if (!liveIds.has(id)) savedNodePositions.delete(id)
        }
      }
      savedViewport = { ...viewportRef.current }
      // A data update recreates this canvas effect. Clear the cancelled frame ID
      // so the new graph can schedule its first draw immediately.
      frameRef.current = null
      wakeRef.current = null
      recenterRef.current = null
      focusRef.current = null
      canvas.removeEventListener('pointerdown', onPointerDown)
      canvas.removeEventListener('pointermove', onPointerMove)
      canvas.removeEventListener('pointerup', finishPointer)
      canvas.removeEventListener('pointercancel', finishPointer)
      canvas.removeEventListener('dblclick', onDoubleClick)
      canvas.removeEventListener('contextmenu', onContext)
      canvas.removeEventListener('wheel', onWheel)
      canvas.removeEventListener('pointerleave', clearHover)
      window.removeEventListener('blur', clearHover)
    }
  }, [edges, layoutKey, nodes])

  return (
    <div ref={hostRef} className="mydb-graph-canvas">
      <canvas ref={canvasRef} className="mydb-graph-canvas__surface" aria-label="My DB 관계 그래프" />
      {nodes.length === 0 && !dropActive && (
        <div className="mydb-empty-state">
          <strong>아직 보관한 자료가 없습니다.</strong>
          <span>파일이나 폴더를 이곳에 끌어놓거나 오른쪽 위의 파일 추가를 선택하세요.</span>
        </div>
      )}
      {dropActive && (
        <div className="mydb-drop-target" aria-live="polite">
          <FolderIcon size={24} />
          <strong>My DB에 보관하기</strong>
          <span>놓으면 개인 저장소로 복사하고 관계 그래프에 추가합니다.</span>
        </div>
      )}
      <div className="mydb-legend" aria-label="그래프 범례">
        <span><i className="mydb-legend__dot mydb-legend__dot--core" />코어</span>
        <span><i className="mydb-legend__dot mydb-legend__dot--file" />파일</span>
      </div>
    </div>
  )
}

function MyDbView({ active, settings }: Props): React.JSX.Element {
  const listHostRef = useRef<HTMLDivElement>(null)
  const workspaceRef = useRef<HTMLElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const [snapshot, setSnapshot] = useState<MyDbSnapshot>(EMPTY_SNAPSHOT)
  const [history, setHistory] = useState<MyDbHistorySnapshot>(EMPTY_HISTORY)
  const [mode, setMode] = useState<MyDbViewMode>('graph')
  // 달력에서 고른 날. null 이면 오늘을 본다.
  const [historyDay, setHistoryDay] = useState<string | null>(null)
  const [historyMonth, setHistoryMonth] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [focusCoreId, setFocusCoreId] = useState<string | null>(null)
  const [linkSourceId, setLinkSourceId] = useState<string | null>(null)
  const [unlinkSourceId, setUnlinkSourceId] = useState<string | null>(null)
  const [unlinkPanelPosition, setUnlinkPanelPosition] = useState<UnlinkPanelPosition | null>(null)
  const [menu, setMenu] = useState<NodeMenuState | null>(null)
  const [canvasMenu, setCanvasMenu] = useState<CanvasMenuState | null>(null)
  const [createCore, setCreateCore] = useState<CreateCoreState | null>(null)
  const [renameState, setRenameState] = useState<RenameState | null>(null)
  const [deleteState, setDeleteState] = useState<DeleteState | null>(null)
  const [versionState, setVersionState] = useState<VersionState | null>(null)
  const [restoreRevisionState, setRestoreRevisionState] = useState<RestoreRevisionState | null>(null)
  const [restoreGraphState, setRestoreGraphState] = useState<RestoreGraphState | null>(null)
  const [trash, setTrash] = useState<MyDbTrashSnapshot | null>(null)
  const [showTrash, setShowTrash] = useState(false)
  const [dropActive, setDropActive] = useState(false)
  const [search, setSearch] = useState('')
  const [extensionFilter, setExtensionFilter] = useState<MyDbExtensionFilter>('all')
  const [collapsedCoreIds, setCollapsedCoreIds] = useState<Set<string>>(() => new Set())
  const [linkSearch, setLinkSearch] = useState('')
  const [graphRecenterToken, setGraphRecenterToken] = useState(0)
  const [focusZoomToken, setFocusZoomToken] = useState(0)

  useEffect(() => {
    if (active) setGraphRecenterToken((token) => token + 1)
  }, [active])

  useEffect(() => {
    if (active && focusCoreId) setFocusZoomToken((token) => token + 1)
  }, [active, focusCoreId])

  // ── 히스토리 달력 ──────────────────────────────────────────────
  const historyCounts = useMemo(() => countByDay(history.entries), [history.entries])
  const historyMonths = useMemo(() => monthsWithHistory(history.entries), [history.entries])
  // 고른 달이 사라지면(이력이 지워졌을 때) 가장 최근 달로 되돌린다.
  // 넘겨 볼 수 있는 범위. 빈 달도 넘길 수 있어야 달력답다 — 이력이 있는 달
  // 사이로만 건너뛰게 했더니 이력이 한 달뿐일 때 양쪽 버튼이 함께 잠겼다.
  const historyRange = useMemo(() => monthRange(history.entries), [history.entries])
  // 고른 달이 범위 안이면 그대로 존중한다. '이력이 있는 달'만 허용하면
  // 빈 달로 넘어가는 순간 곧바로 되돌아와 버튼이 먹통처럼 보인다.
  const activeMonth = historyMonth && historyMonth >= historyRange.min && historyMonth <= historyRange.max
    ? historyMonth
    : historyMonths[0] ?? localDayKey(new Date()).slice(0, 7)
  const calendarMonth = useMemo(() => buildMonth(activeMonth, historyCounts), [activeMonth, historyCounts])
  const monthPeak = useMemo(
    () => calendarMonth.days.reduce((max, day) => Math.max(max, day.count), 0),
    [calendarMonth]
  )
  const reportsByDate = useMemo(
    () => new Map(history.dailyReports.map((report) => [report.reportDate, report])),
    [history.dailyReports]
  )
  // 달력을 열면 오늘이 선택돼 있다 — 사람이 보고 싶은 건 '지금까지 뭘 했나'다.
  // 오늘 보고서는 아직 없지만, 아래 resolveReportDate 가 전날 것으로 물러나
  // 패널이 비지 않는다.
  const selectedDay = historyDay ?? localDayKey(new Date())
  // 그 날 보고서가 없으면 그 이전 가장 최근 보고서를 읽는다. 이력은 오늘 것이
  // 이미 쌓여 있는데 보고서만 없는 어긋남을 이렇게 메운다.
  const reportDate = resolveReportDate(selectedDay, reportsByDate.keys())
  const shownReport = reportsByDate.get(reportDate) ?? null
  // 아래 목록은 **고른 날** 그대로다. 보고서가 하루 뒤로 물러났다고 해서
  // 오늘 한 일까지 감추면 안 된다.
  const visibleHistory = useMemo(
    () => history.entries.filter((entry) => localDayKey(entry.createdAt) === selectedDay),
    [history.entries, selectedDay]
  )

  const nodesById = useMemo(() => new Map(snapshot.nodes.map((node) => [node.id, node])), [snapshot.nodes])
  const selected = selectedId ? nodesById.get(selectedId) ?? null : null
  const focusCore = focusCoreId ? nodesById.get(focusCoreId) ?? null : null
  const linkSource = linkSourceId ? nodesById.get(linkSourceId) ?? null : null
  const unlinkSource = unlinkSourceId ? nodesById.get(unlinkSourceId) ?? null : null
  const relationshipSource = linkSource ?? unlinkSource
  const menuNode = menu ? nodesById.get(menu.nodeId) ?? null : null
  const menuHasChildren = Boolean(
    menuNode?.kind === 'core'
    && snapshot.edges.some((edge) => edge.sourceId === menuNode.id && edge.relation === 'contains' && nodesById.has(edge.targetId))
  )
  const deleteHasChildren = Boolean(
    deleteState?.node.kind === 'core'
    && snapshot.edges.some((edge) => edge.sourceId === deleteState.node.id && edge.relation === 'contains' && nodesById.has(edge.targetId))
  )
  const currentCoreId = focusCore?.kind === 'core' ? focusCore.id : selected?.kind === 'core' ? selected.id : null

  const coreIdsWithChildren = useMemo(() => {
    const result = new Set<string>()
    for (const edge of snapshot.edges) {
      if (edge.relation !== 'contains') continue
      const source = nodesById.get(edge.sourceId)
      const target = nodesById.get(edge.targetId)
      if (source?.kind === 'core' && target) result.add(source.id)
    }
    return result
  }, [nodesById, snapshot.edges])

  useEffect(() => {
    if (mode !== 'list' || !selectedId) return
    const parentByChild = new Map<string, string>()
    for (const edge of snapshot.edges) {
      if (edge.relation !== 'contains') continue
      const source = nodesById.get(edge.sourceId)
      if (source?.kind === 'core' && nodesById.has(edge.targetId) && !parentByChild.has(edge.targetId)) {
        parentByChild.set(edge.targetId, source.id)
      }
    }
    setCollapsedCoreIds((current) => {
      const next = new Set(current)
      const seen = new Set<string>()
      let cursor = parentByChild.get(selectedId)
      while (cursor && !seen.has(cursor)) {
        seen.add(cursor)
        next.delete(cursor)
        cursor = parentByChild.get(cursor)
      }
      return next.size === current.size ? current : next
    })
  }, [mode, nodesById, selectedId, snapshot.edges])
  const visibleGraph = useMemo(() => {
    if (focusCore?.kind !== 'core') return snapshot
    const visibleIds = visibleCoreBranchIds(focusCore.id, snapshot.nodes, snapshot.edges)
    return {
      nodes: snapshot.nodes.filter((node) => visibleIds.has(node.id)),
      edges: snapshot.edges.filter((edge) => visibleIds.has(edge.sourceId) && visibleIds.has(edge.targetId))
    }
  }, [focusCore, snapshot])
  const canvasSearchResults = useMemo(() => {
    const query = canvasMenu?.query.trim().toLocaleLowerCase('ko-KR') ?? ''
    if (!query) return []
    return visibleGraph.nodes
      .filter((node) => `${node.title} ${(node.tags ?? []).join(' ')}`.toLocaleLowerCase('ko-KR').includes(query))
      .slice(0, 12)
  }, [canvasMenu?.query, visibleGraph.nodes])
  const linkSearchResults = useMemo(() => {
    if (!linkSource) return []
    const query = linkSearch.trim().toLocaleLowerCase('ko-KR')
    return snapshot.nodes
      .filter((node) => node.id !== linkSource.id)
      .filter((node) => !query || `${node.title} ${(node.tags ?? []).join(' ')}`.toLocaleLowerCase('ko-KR').includes(query))
      .sort((left, right) => left.title.localeCompare(right.title, 'ko-KR'))
      .slice(0, 10)
  }, [linkSearch, linkSource, snapshot.nodes])
  const unlinkTargets = useMemo(() => {
    if (!unlinkSource) return []
    const targetIds = new Set<string>()
    for (const edge of snapshot.edges) {
      if (edge.sourceId === unlinkSource.id) targetIds.add(edge.targetId)
      if (edge.targetId === unlinkSource.id) targetIds.add(edge.sourceId)
    }
    return [...targetIds]
      .map((id) => nodesById.get(id))
      .filter((node): node is MyDbNode => Boolean(node))
      .sort((left, right) => left.title.localeCompare(right.title, 'ko-KR'))
  }, [nodesById, snapshot.edges, unlinkSource])
  const canvasRadialPosition = useMemo(() => {
    if (!canvasMenu) return null
    const width = workspaceRef.current?.clientWidth ?? 800
    const height = workspaceRef.current?.clientHeight ?? 600
    const slotCount = 12
    const ringBase = 200
    const ringGap = 140
    const ringsUsed = Math.floor(Math.max(0, canvasSearchResults.length - 1) / slotCount)
    const reach = ringBase + ringsUsed * ringGap + 60
    return {
      x: width <= reach * 2 ? width / 2 : Math.max(reach, Math.min(canvasMenu.x, width - reach)),
      y: height <= reach * 2 ? height / 2 : Math.max(reach, Math.min(canvasMenu.y, height - reach))
    }
  }, [canvasMenu, canvasSearchResults.length])

  const selectCanvasSearchResult = (node: MyDbNode): void => {
    setSelectedId(node.id)
    if (node.kind === 'core') setFocusCoreId(node.id)
    setCanvasMenu(null)
  }

  const returnToAll = useCallback((): void => {
    setFocusCoreId(null)
    setSelectedId(null)
    setLinkSourceId(null)
    setUnlinkSourceId(null)
    setUnlinkPanelPosition(null)
    setMenu(null)
  }, [])

  useEffect(() => {
    if (!notice) return
    const timeoutId = window.setTimeout(() => setNotice(null), 3200)
    return () => window.clearTimeout(timeoutId)
  }, [notice])

  useEffect(() => {
    if (!active || mode !== 'graph' || focusCore?.kind !== 'core') return

    const leaveFocusedCore = (event: KeyboardEvent | MouseEvent): void => {
      const isEscape = event instanceof KeyboardEvent && event.key === 'Escape'
      const isMouseBack = event instanceof MouseEvent && event.button === 3
      if (!isEscape && !isMouseBack) return
      event.preventDefault()
      event.stopPropagation()
      returnToAll()
    }

    window.addEventListener('keydown', leaveFocusedCore)
    window.addEventListener('mousedown', leaveFocusedCore, true)
    window.addEventListener('auxclick', leaveFocusedCore, true)
    return () => {
      window.removeEventListener('keydown', leaveFocusedCore)
      window.removeEventListener('mousedown', leaveFocusedCore, true)
      window.removeEventListener('auxclick', leaveFocusedCore, true)
    }
  }, [active, focusCore?.kind, mode, returnToAll])

  const load = useCallback(async (): Promise<void> => {
    if (!window.api?.myDb) {
      setSnapshot(EMPTY_SNAPSHOT)
      setHistory(EMPTY_HISTORY)
      setError('My DB 저장소 연결을 준비하는 중입니다. Aiso를 다시 시작해 주세요.')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const bridge = getMyDbBridge()
      const [next, nextHistory] = await Promise.all([bridge.state(), bridge.history()])
      setSnapshot(next)
      setHistory(nextHistory)
      setSelectedId((current) => current && !next.nodes.some((node) => node.id === current) ? null : current)
      setFocusCoreId((current) => current && !next.nodes.some((node) => node.id === current && node.kind === 'core') ? null : current)
      setLinkSourceId((current) => current && !next.nodes.some((node) => node.id === current) ? null : current)
      setUnlinkSourceId((current) => current && !next.nodes.some((node) => node.id === current) ? null : current)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'My DB 자료를 불러오지 못했습니다.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (active) void load()
  }, [active, load])

  // 화면을 열어 둔 채 날이 바뀌면 새 보고서가 생긴다 — 그때 다시 읽는다.
  // (자정 직후 메인이 보고서를 쓰고 알려 준다.)
  useEffect(() => {
    const bridge = window.api?.myDb
    if (!bridge?.onDailyReport) return
    return bridge.onDailyReport(() => { void load() })
  }, [load])

  // 자동 비우기가 항목을 지웠으면 열어 둔 휴지통이 이미 사라진 것을 보여 주고 있다.
  useEffect(() => {
    const bridge = window.api?.myDb
    if (!bridge?.onTrashPurged) return
    return bridge.onTrashPurged((purged) => {
      void (async () => {
        setTrash(await getMyDbBridge().trash?.() ?? { nodes: [] })
        await load()
        setNotice(`보관 기한이 지난 휴지통 항목 ${purged}개를 자동으로 삭제했습니다.`)
      })()
    })
  }, [load])

  const importPaths = useCallback(async (paths: string[]): Promise<void> => {
    if (paths.length === 0) return
    setLoading(true)
    setError(null)
    try {
      const result = await getMyDbBridge().importDropped(paths, currentCoreId)
      const created = result.createdNodes.length
      const skipped = result.skippedPaths.length
      setNotice(skipped > 0 ? `${created}개 항목을 보관했고 ${skipped}개는 건너뛰었습니다.` : `${created}개 항목을 My DB에 보관했습니다.`)
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '파일을 My DB에 추가하지 못했습니다.')
    } finally {
      setLoading(false)
      setDropActive(false)
    }
  }, [currentCoreId, load])

  useEffect(() => {
    const listener = window.api?.myDb?.onDrop
    if (!active || !listener) return
    return listener((event) => {
      if (event.status === 'start') setDropActive(true)
      if (event.status === 'error') {
        setDropActive(false)
        setError(event.error)
      }
      if (event.status === 'done') {
        const created = event.result.createdNodes.length
        const skipped = event.result.skippedPaths.length
        setDropActive(false)
        setNotice(skipped > 0 ? `${created}개 항목을 보관했고 ${skipped}개는 건너뛰었습니다.` : `${created}개 항목을 My DB에 보관했습니다.`)
        void load()
      }
    })
  }, [active, load])

  const runAction = async (action: () => Promise<void>, success?: string): Promise<void> => {
    setError(null)
    try {
      await action()
      if (success) setNotice(success)
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '요청을 완료하지 못했습니다.')
    }
  }

  const compareVersionPair = async (item: MyDbNode, before: MyDbRevision, after: MyDbRevision): Promise<void> => {
    setVersionState((current) => current?.item.id === item.id ? { ...current, loading: true, error: null } : current)
    try {
      const diff = await getMyDbBridge().compareRevisions(item.id, before.id, after.id)
      setVersionState((current) => current?.item.id === item.id ? { ...current, diff, loading: false, error: null } : current)
    } catch (reason) {
      setVersionState((current) => current?.item.id === item.id
        ? { ...current, loading: false, error: reason instanceof Error ? reason.message : '버전 차이를 불러오지 못했습니다.' }
        : current)
    }
  }

  const openVersions = async (item: MyDbNode): Promise<void> => {
    if (item.kind !== 'file') return
    setVersionState({ item, history: null, diff: null, loading: true, error: null })
    try {
      const fileHistory = await getMyDbBridge().fileHistory(item.id)
      setVersionState((current) => current?.item.id === item.id
        ? { ...current, item: fileHistory.item, history: fileHistory, loading: false, error: null }
        : current)
      if (fileHistory.revisions.length >= 2) {
        await compareVersionPair(item, fileHistory.revisions[1]!, fileHistory.revisions[0]!)
      }
    } catch (reason) {
      setVersionState((current) => current?.item.id === item.id
        ? { ...current, loading: false, error: reason instanceof Error ? reason.message : '파일 버전을 불러오지 못했습니다.' }
        : current)
    }
  }

  const restoreRevision = (): void => {
    if (!restoreRevisionState) return
    const { item, revision } = restoreRevisionState
    void runAction(async () => {
      await getMyDbBridge().restoreRevision(item.id, revision.id)
      setRestoreRevisionState(null)
      setVersionState(null)
      setSelectedId(item.id)
    }, `v${revision.sequence} 버전으로 복원했습니다.`)
  }

  const restoreGraph = (): void => {
    const checkpointId = restoreGraphState?.entry.graphCheckpointId
    if (!checkpointId) return
    void runAction(async () => {
      await getMyDbBridge().restoreGraphCheckpoint(checkpointId)
      setRestoreGraphState(null)
      setSelectedId(null)
      setFocusCoreId(null)
      setLinkSourceId(null)
      setUnlinkSourceId(null)
      setMenu(null)
    }, '선택한 시점의 그래프로 복원했습니다.')
  }

  const openVersionFromHistory = (entry: MyDbHistoryEntry): void => {
    if (!entry.subjectId) return
    const item = nodesById.get(entry.subjectId)
    if (item?.kind === 'file') void openVersions(item)
  }

  const openFilePicker = (): void => {
    void runAction(async () => {
      const result = await getMyDbBridge().pickFiles(currentCoreId)
      if (result.createdNodes.length === 0 && result.skippedPaths.length === 0) return
      const skipped = result.skippedPaths.length
      setNotice(skipped > 0 ? `${result.createdNodes.length}개 항목을 보관했고 ${skipped}개는 건너뛰었습니다.` : `${result.createdNodes.length}개 항목을 My DB에 보관했습니다.`)
    })
  }

  const pickSourceForFile = (item: MyDbNode): void => {
    if (item.kind !== 'file') return
    void (async () => {
      setError(null)
      try {
        const linked = await getMyDbBridge().pickSourceForFile(item.id)
        if (!linked) return
        setSelectedId(linked.id)
        setNotice('외부 원본을 연결했습니다.')
        await load()
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : '외부 원본을 연결하지 못했습니다.')
      }
    })()
  }

  const exportFocusedCore = (): void => {
    if (focusCore?.kind !== 'core') return
    void (async () => {
      setLoading(true)
      setError(null)
      try {
        const bridge = getMyDbBridge()
        if (typeof bridge.exportCore !== 'function') {
          throw new Error('다운로드 기능을 적용하려면 Aiso를 완전히 종료한 뒤 다시 실행해 주세요.')
        }
        const result = await bridge.exportCore(focusCore.id)
        if (!result) return
        const skipped = result.skippedFiles > 0 ? ` · 누락 ${result.skippedFiles}개` : ''
        setNotice(`${result.folderName} 폴더로 코어 ${result.exportedCores}개 · 파일 ${result.exportedFiles}개를 내보냈습니다.${skipped}`)
        await load()
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : '코어를 폴더로 내보내지 못했습니다.')
      } finally {
        setLoading(false)
      }
    })()
  }

  const handleDragEnter = (event: ReactDragEvent<HTMLElement>): void => {
    if (!event.dataTransfer.types.includes('Files')) return
    event.preventDefault()
    setDropActive(true)
  }

  const handleDragOver = (event: ReactDragEvent<HTMLElement>): void => {
    if (!event.dataTransfer.types.includes('Files')) return
    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
  }

  const handleDragLeave = (event: ReactDragEvent<HTMLElement>): void => {
    if (event.currentTarget.contains(event.relatedTarget as Node)) return
    setDropActive(false)
  }

  const handleDrop = (event: ReactDragEvent<HTMLElement>): void => {
    event.preventDefault()
    const files = Array.from(event.dataTransfer.files) as Array<File & { path?: string }>
    const paths = files.map((file) => file.path).filter((path): path is string => Boolean(path))
    if (window.api?.myDb?.onDrop || paths.length === 0) {
      // Electron's preload owns native File paths and imports the normal drop.
      // Keep the visual target active until its `done` or `error` event arrives.
      setDropActive(true)
      return
    }
    void importPaths(paths)
  }

  const handleGraphContextMenu = (nodeId: string | null, point: Point): void => {
    setCreateCore(null)
    if (nodeId) {
      setCanvasMenu(null)
      setSelectedId(nodeId)
      setMenu({ nodeId, x: point.x, y: point.y })
      return
    }
    setMenu(null)
    setCanvasMenu({ x: point.x, y: point.y, query: '' })
  }

  const anchorGraphContextMenu = useCallback((nodeId: string, point: Point): void => {
    const element = menuRef.current
    if (!element || element.dataset.nodeId !== nodeId) return
    element.style.left = `${point.x}px`
    element.style.top = `${point.y}px`
  }, [])

  const handleListContextMenu = (event: React.MouseEvent<HTMLElement>, nodeId: string): void => {
    event.preventDefault()
    const host = listHostRef.current
    if (!host) return
    const rect = host.getBoundingClientRect()
    setSelectedId(nodeId)
    setMenu({ nodeId, x: event.clientX - rect.left, y: event.clientY - rect.top })
  }

  const createCoreSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault()
    if (!createCore?.title.trim()) return
    const draft = createCore
    void runAction(async () => {
      await getMyDbBridge().createCore(draft.title.trim(), draft.parentCoreId)
      setCreateCore(null)
    }, '새 코어를 만들었습니다.')
  }

  const renameSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault()
    if (!renameState?.title.trim()) return
    const draft = renameState
    void runAction(async () => {
      await getMyDbBridge().renameNode(draft.node.id, draft.title.trim())
      setRenameState(null)
    }, '이름을 변경했습니다.')
  }

  const deleteNode = (): void => {
    if (!deleteState) return
    const node = deleteState.node
    const cascade = deleteHasChildren && deleteState.cascade
    void runAction(async () => {
      await getMyDbBridge().deleteNode(node.id, { cascade })
      setDeleteState(null)
      setMenu(null)
      setSelectedId((current) => current === node.id ? null : current)
      setFocusCoreId((current) => current === node.id ? null : current)
      setLinkSourceId((current) => current === node.id ? null : current)
      setUnlinkSourceId((current) => current === node.id ? null : current)
    }, `${nodeTypeLabel(node.kind)}을 휴지통으로 옮겼습니다.`)
  }

  const openTrash = (): void => {
    const getTrash = window.api?.myDb?.trash
    if (!getTrash) {
      setError('휴지통을 준비하는 중입니다. Aiso를 다시 시작해 주세요.')
      return
    }
    void (async () => {
      try {
        setTrash(await getTrash())
        setShowTrash(true)
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : '휴지통을 불러오지 못했습니다.')
      }
    })()
  }

  /**
   * 완전 삭제는 My DB에서 유일하게 되돌릴 수 없는 동작이다. 그래서
   *  1) 휴지통 안에서만 가능하고(삭제 → 휴지통 → 완전 삭제, 두 단계),
   *  2) 항목 이름을 확인 문구에 넣어 무엇을 지우는지 못 보고 누르는 일을 막고,
   *  3) 파일이면 보관된 버전 기록까지 함께 사라진다는 사실을 미리 말한다.
   */
  const purgeFromTrash = async (node: MyDbNode): Promise<void> => {
    const purge = getMyDbBridge().purgeNode
    if (!purge) {
      setError('완전 삭제를 준비하는 중입니다. Aiso를 다시 시작해 주세요.')
      return
    }
    const ok = await confirmDialog({
      title: '완전 삭제',
      message: node.kind === 'file'
        ? `“${node.title}”을(를) 완전히 삭제합니다.
보관된 파일과 모든 버전 기록이 사라지며 되돌릴 수 없습니다.`
        : `“${node.title}” 코어를 완전히 삭제합니다.
연결이 함께 사라지며 되돌릴 수 없습니다.`,
      confirmLabel: '완전 삭제',
      danger: true
    })
    if (!ok) return
    await runAction(async () => {
      await purge(node.id)
      setTrash(await getMyDbBridge().trash?.() ?? { nodes: [] })
    }, '항목을 완전히 삭제했습니다.')
  }

  /**
   * 휴지통 전체 비우기.
   *
   * 단건 완전 삭제와 같은 규칙을 따르되, 무엇이 사라지는지 **개수로** 말한다 —
   * 이름을 하나씩 못 보고 누르는 대신 몇 개가 지워지는지는 알아야 한다.
   * 코어와 파일을 나눠 세는 이유는 파일 쪽에만 버전 기록이 딸려 있기 때문이다.
   */
  const emptyTrash = async (): Promise<void> => {
    const purgeAll = getMyDbBridge().purgeTrash
    if (!purgeAll) {
      setError('휴지통 비우기를 준비하는 중입니다. Aiso를 다시 시작해 주세요.')
      return
    }
    const nodes = trash?.nodes ?? []
    if (nodes.length === 0) return
    const cores = nodes.filter((node) => node.kind === 'core').length
    const files = nodes.length - cores
    const parts = [cores > 0 ? `코어 ${cores}개` : '', files > 0 ? `파일 ${files}개` : ''].filter(Boolean)
    const ok = await confirmDialog({
      title: '휴지통 비우기',
      message: `휴지통의 ${parts.join(' · ')}를 완전히 삭제합니다.
${files > 0 ? '보관된 파일과 모든 버전 기록이 함께 사라지며 ' : ''}되돌릴 수 없습니다.`,
      confirmLabel: '전부 삭제',
      danger: true
    })
    if (!ok) return
    await runAction(async () => {
      const result = await purgeAll()
      setTrash(await getMyDbBridge().trash?.() ?? { nodes: [] })
      if (result.failed > 0) {
        throw new Error(`${result.purged}개를 삭제했고 ${result.failed}개는 실패했습니다. 잠시 후 다시 시도해 주세요.`)
      }
    }, '휴지통을 비웠습니다.')
  }

  const connect = (targetId: string): void => {
    if (!linkSourceId || targetId === linkSourceId) return
    const source = nodesById.get(linkSourceId)
    const target = nodesById.get(targetId)
    if (!source || !target) return
    // A core is an organising node: connecting from it makes the selected
    // target its child, matching the standalone My DB System's pivot links.
    // File-originated links remain ordinary references.
    const relation = source.kind === 'core' ? 'contains' : 'related'
    const legacyRelations = relation === 'contains'
      ? snapshot.edges.filter((edge) => (
        edge.sourceId === source.id
        && edge.targetId === target.id
        && edge.relation === 'related'
      ))
      : []
    void runAction(async () => {
      await getMyDbBridge().link(source.id, target.id, relation)
      // A connection made before hierarchy semantics existed may still be
      // present beside the new structural edge. Remove that exact duplicate,
      // not any independent reverse or cross-reference relationship.
      await Promise.all(legacyRelations.map((edge) => getMyDbBridge().unlink(edge.id)))
      setLinkSourceId(null)
      setLinkSearch('')
      setMenu(null)
    }, relation === 'contains' ? '하위 항목으로 연결했습니다.' : '항목을 연결했습니다.')
  }

  const startLink = (nodeId: string): void => {
    // A previous completion toast must not cover the active linking guidance.
    setNotice(null)
    setError(null)
    setUnlinkSourceId(null)
    setLinkSearch('')
    setLinkSourceId(nodeId)
    setMenu(null)
  }

  const disconnect = (targetId: string): void => {
    if (!unlinkSourceId || targetId === unlinkSourceId) return
    const edgesToRemove = snapshot.edges.filter((edge) => (
      (edge.sourceId === unlinkSourceId && edge.targetId === targetId)
      || (edge.sourceId === targetId && edge.targetId === unlinkSourceId)
    ))
    if (edgesToRemove.length === 0) {
      setUnlinkSourceId(null)
      setUnlinkPanelPosition(null)
      setNotice('선택한 항목과 직접 연결된 관계가 없습니다.')
      return
    }
    void runAction(async () => {
      await Promise.all(edgesToRemove.map((edge) => getMyDbBridge().unlink(edge.id)))
      setUnlinkSourceId(null)
      setUnlinkPanelPosition(null)
      setMenu(null)
    }, '연결을 해제했습니다.')
  }

  const startDisconnect = (nodeId: string): void => {
    // Keep this as a target-selection flow so the context menu stays compact.
    setNotice(null)
    setError(null)
    setLinkSourceId(null)
    setUnlinkSourceId(nodeId)
    setUnlinkPanelPosition(menu ? { x: menu.x, y: menu.y } : null)
    setMenu(null)
  }

  const openNode = (node: MyDbNode): void => {
    if (node.kind === 'file' && window.api?.myDb?.openFile) {
      void runAction(async () => {
        await window.api.myDb?.openFile?.(node.id)
      })
      return
    }
    setSelectedId(node.id)
  }

  const availableExtensions = useMemo(() => [...new Set(snapshot.nodes
    .filter((node) => node.kind === 'file')
    .map(fileExtension))].sort((left, right) => left.localeCompare(right, 'en-US')), [snapshot.nodes])
  const libraryRows = useMemo(
    () => buildLibraryTreeRows(snapshot.nodes, snapshot.edges, search, extensionFilter, collapsedCoreIds),
    [collapsedCoreIds, extensionFilter, search, snapshot.edges, snapshot.nodes]
  )
  const visibleFileCount = libraryRows.filter((row) => row.node.kind === 'file').length
  const visibleCoreCount = libraryRows.filter((row) => row.node.kind === 'core').length
  const versionRevisions = versionState?.history?.revisions ?? []

  return (
    <section className="mydb-view" aria-label="My DB">
      <header className="mydb-toolbar">
        <div className={`mydb-toolbar__center${mode === 'graph' ? ' is-graph' : mode === 'list' ? ' is-list' : ' is-history'}`} role="group" aria-label="My DB 보기 방식">
          <button type="button" className={`mydb-segment${mode === 'graph' ? ' is-active' : ''}`} onClick={() => setMode('graph')} aria-pressed={mode === 'graph'}>
            <GraphIcon size={15} />
            그래프
          </button>
          <button type="button" className={`mydb-segment${mode === 'list' ? ' is-active' : ''}`} onClick={() => setMode('list')} aria-pressed={mode === 'list'}>
            <span className="mydb-list-icon" aria-hidden="true">☷</span>
            목록
          </button>
          <button type="button" className={`mydb-segment${mode === 'history' ? ' is-active' : ''}`} onClick={() => setMode('history')} aria-pressed={mode === 'history'} aria-label="히스토리">
            히스토리
          </button>
        </div>
        <div className="mydb-toolbar__actions">
          <button type="button" className="mydb-toolbar-button" onClick={() => void runAction(() => getMyDbBridge().openFolder())} title="저장 폴더 열기" aria-label="저장 폴더 열기">
            <FolderIcon size={16} />
          </button>
          {mode === 'graph' && focusCore?.kind === 'core' && (
            <button type="button" className="mydb-toolbar-button" onClick={exportFocusedCore} title="하위 자료 다운로드" aria-label="포커스한 코어의 하위 자료 다운로드">
              <DownloadIcon size={16} />
            </button>
          )}
          <button type="button" className="mydb-toolbar-button" onClick={openTrash} title="휴지통" aria-label="휴지통">
            <TrashIcon size={16} />
          </button>
          <button type="button" className="mydb-add-button" onClick={openFilePicker}>
            <span aria-hidden="true">＋</span> 파일 추가
          </button>
        </div>
      </header>

      <main
        ref={workspaceRef}
        className="mydb-workspace"
        data-aiso-mydb-drop-target={focusCore?.kind === 'core' ? `mydb:${focusCore.id}` : 'mydb'}
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        {loading && <div className={`mydb-toast${relationshipSource ? ' mydb-toast--with-link' : ''}`} role="status">My DB를 불러오는 중입니다.</div>}
        {error && <button type="button" className={`mydb-toast mydb-toast--error${relationshipSource ? ' mydb-toast--with-link' : ''}`} onClick={() => setError(null)}>{error}</button>}
        {notice && <button type="button" className={`mydb-toast${relationshipSource ? ' mydb-toast--with-link' : ''}`} onClick={() => setNotice(null)}>{notice}</button>}

        {linkSource && (
          <div className="mydb-link-panel" role="dialog" aria-label="My DB 연결 대상 선택">
            <div className="mydb-link-panel__header">
              <span><LinkIcon size={14} /><strong>{linkSource.title}</strong>과(와) 연결할 대상을 클릭하거나 검색하세요.</span>
              <button type="button" onClick={() => { setLinkSourceId(null); setLinkSearch('') }}>취소</button>
            </div>
            <label className="mydb-link-panel__search">
              <SearchIcon size={14} />
              <input
                autoFocus
                value={linkSearch}
                onChange={(event) => setLinkSearch(event.target.value)}
                placeholder="코어 / 파일 이름 검색"
                aria-label="연결할 코어 또는 파일 검색"
                onKeyDown={(event) => {
                  if (event.key === 'Escape') {
                    setLinkSourceId(null)
                    setLinkSearch('')
                  }
                  if (event.key === 'Enter' && linkSearchResults.length === 1) connect(linkSearchResults[0].id)
                }}
              />
            </label>
            <div className="mydb-link-panel__results" aria-label="연결 대상 검색 결과">
              {linkSearchResults.map((node) => (
                <button key={node.id} type="button" onClick={() => connect(node.id)}>
                  <i className={`mydb-legend__dot mydb-legend__dot--${node.kind}`} aria-hidden="true" />
                  <span>{node.title}</span>
                  <small>{nodeTypeLabel(node.kind)}</small>
                </button>
              ))}
              {linkSearchResults.length === 0 && <p>연결할 항목을 찾지 못했습니다.</p>}
            </div>
          </div>
        )}

        {unlinkSource && (
          <div
            className="mydb-unlink-panel"
            role="dialog"
            aria-label="해제할 연결 선택"
            style={unlinkPanelPosition ? { left: unlinkPanelPosition.x, top: unlinkPanelPosition.y } : undefined}
          >
            <div className="mydb-unlink-panel__title">
              <span><i className={`mydb-legend__dot mydb-legend__dot--${unlinkSource.kind}`} aria-hidden="true" />{unlinkSource.title}</span>
              <button type="button" onClick={() => { setUnlinkSourceId(null); setUnlinkPanelPosition(null) }}>취소</button>
            </div>
            <div className="mydb-unlink-panel__targets" aria-label="직접 연결된 항목">
              {unlinkTargets.map((node) => (
                <button key={node.id} type="button" onClick={() => disconnect(node.id)}>
                  <i className={`mydb-legend__dot mydb-legend__dot--${node.kind}`} aria-hidden="true" />
                  <span>{node.title}</span>
                </button>
              ))}
              {unlinkTargets.length === 0 && <p>직접 연결된 항목이 없습니다.</p>}
            </div>
          </div>
        )}

        {mode === 'graph' && focusCore?.kind === 'core' && (
          <div className="mydb-focus-banner" aria-label="코어 집중 보기">
            <strong title={focusCore.title}>{focusCore.title}</strong>
          </div>
        )}

        {mode === 'graph' ? (
          <MyDbGraphCanvas
            nodes={visibleGraph.nodes}
            edges={visibleGraph.edges}
            layoutKey={focusCore?.kind === 'core' ? focusCore.id : 'all'}
            recenterToken={graphRecenterToken}
            focusCoreId={focusCore?.kind === 'core' ? focusCore.id : null}
            focusZoomToken={focusZoomToken}
            selectedId={selectedId}
            linkSourceId={linkSourceId}
            targetSelectionSourceId={relationshipSource?.id ?? null}
            contextMenuNodeId={menu?.nodeId ?? null}
            dropActive={dropActive}
            onSelect={(id) => {
              setMenu(null)
              setCanvasMenu(null)
              setCreateCore(null)
              if (unlinkSourceId && id && id !== unlinkSourceId) {
                disconnect(id)
                return
              }
              if (linkSourceId && id && id !== linkSourceId) {
                connect(id)
                return
              }
              setSelectedId(id)
              const node = id ? nodesById.get(id) : null
              if (node?.kind === 'core') setFocusCoreId(node.id)
            }}
            onLinkTarget={(id) => {
              if (unlinkSourceId) {
                disconnect(id)
                return
              }
              connect(id)
            }}
            onOpenNode={openNode}
            onContextMenu={handleGraphContextMenu}
            onContextMenuAnchorChange={anchorGraphContextMenu}
          />
        ) : (
          mode === 'list' ? (
          <div ref={listHostRef} className="mydb-library">
            <aside className="mydb-library__list">
              <div className="mydb-library__search">
                <SearchIcon size={14} />
                <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="보관한 파일 검색" aria-label="보관한 파일 검색" />
              </div>
              <div className="mydb-library__filters" role="group" aria-label="파일 확장자 필터">
                <button type="button" className={extensionFilter === 'all' ? 'is-active' : ''} onClick={() => setExtensionFilter('all')}>전체</button>
                {availableExtensions.map((extension) => (
                  <button key={extension} type="button" className={extensionFilter === extension ? 'is-active' : ''} onClick={() => setExtensionFilter(extension)}>{extension}</button>
                ))}
              </div>
              <div className="mydb-library__summary">코어 {visibleCoreCount}개 · 파일 {visibleFileCount}개</div>
              <div className="mydb-library__items">
                {libraryRows.map(({ node, depth }) => {
                  const canCollapse = node.kind === 'core' && coreIdsWithChildren.has(node.id)
                  const collapsed = node.kind === 'core' && collapsedCoreIds.has(node.id)
                  return (
                    <div
                      key={`${node.id}-${depth}`}
                      className={`mydb-library-row mydb-library-row--${node.kind}${selectedId === node.id ? ' is-selected' : ''}`}
                      style={{ paddingInlineStart: `${8 + depth * 17}px` }}
                      role={node.kind === 'core' ? 'treeitem' : undefined}
                      aria-level={node.kind === 'core' ? depth + 1 : undefined}
                      aria-expanded={canCollapse ? !collapsed : undefined}
                    >
                      {node.kind === 'core' ? (
                        canCollapse ? <span className={`mydb-library-row__toggle${collapsed ? ' is-collapsed' : ''}`} aria-hidden="true" />
                          : <span className="mydb-library-row__toggle-spacer" aria-hidden="true" />
                      ) : <span className="mydb-library-row__toggle-spacer" aria-hidden="true" />}
                      <button
                        type="button"
                        className="mydb-library-row__select"
                        aria-expanded={canCollapse ? !collapsed : undefined}
                        onClick={() => {
                          if (node.kind === 'core' && canCollapse) {
                            setCollapsedCoreIds((current) => {
                              const next = new Set(current)
                              if (next.has(node.id)) next.delete(node.id)
                              else next.add(node.id)
                              return next
                            })
                            return
                          }
                          setSelectedId(node.id)
                        }}
                        onDoubleClick={node.kind === 'file' ? () => openNode(node) : undefined}
                        onContextMenu={(event) => handleListContextMenu(event, node.id)}
                      >
                        {node.kind === 'core' ? <FolderIcon size={15} /> : <FileIcon size={15} />}
                        <span>
                          <strong>{node.title}</strong>
                          <small>{node.kind === 'core' ? '코어 폴더' : `${fileExtension(node)} · ${fileTypeLabel(node)} · ${formatSize(node.size)}`}</small>
                        </span>
                      </button>
                    </div>
                  )
                })}
                {libraryRows.length === 0 && <p className="mydb-library__empty">조건에 맞는 코어 또는 파일이 없습니다.</p>}
              </div>
            </aside>
            <article className="mydb-library__detail">
              {selected ? (
                <>
                  <span className="mydb-detail__kind">{nodeTypeLabel(selected.kind)}</span>
                  <h2>{selected.title}</h2>
                  <dl>
                    <div><dt>종류</dt><dd>{fileTypeLabel(selected)}</dd></div>
                    {selected.kind === 'file' && <div><dt>크기</dt><dd>{formatSize(selected.size)}</dd></div>}
                    <div><dt>추가한 날</dt><dd>{formatDate(selected.createdAt)}</dd></div>
                    <div><dt>수정한 날</dt><dd>{formatDate(selected.updatedAt)}</dd></div>
                  </dl>
                  {selected.tags && selected.tags.length > 0 && (
                    <div className="mydb-tags">{selected.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
                  )}
                  <div className="mydb-detail__actions">
                    {selected.kind === 'file' && window.api?.myDb?.openFile && <button type="button" onClick={() => openNode(selected)}>열기</button>}
                    {selected.kind === 'file' && <button type="button" onClick={() => void openVersions(selected)}>버전</button>}
                    {selected.kind === 'file' && <button type="button" onClick={() => pickSourceForFile(selected)}>원본 연결</button>}
                    {selected.kind === 'file' && window.api?.myDb?.showInFolder && <button type="button" onClick={() => void runAction(() => window.api.myDb?.showInFolder?.(selected.id) ?? Promise.resolve())}>폴더에서 보기</button>}
                  </div>
                </>
              ) : (
                <div className="mydb-library__placeholder">
                  <div className="mydb-library__placeholder-content">
                    <FileIcon size={23} />
                    <strong>파일을 선택하세요.</strong>
                    <span>보관한 자료의 정보와 연결을 이곳에서 확인합니다.</span>
                  </div>
                </div>
              )}
            </article>
            {dropActive && (
              <div className="mydb-drop-target mydb-drop-target--library" aria-live="polite">
                <FolderIcon size={24} />
                <strong>My DB에 보관하기</strong>
                <span>놓으면 개인 저장소에 복사합니다.</span>
              </div>
            )}
          </div>
          ) : (
            <div className="mydb-history-view" aria-label="My DB 변경 이력">
              <div className="mydb-history">
                <header className="mydb-history__head">
                  <h2>변경 이력</h2>
                  <span>자동 보고 {history.dailyReports.length}개 · 변경 {history.entries.length}개</span>
                </header>
                <div className="mydb-cal-row">
                  <section className="mydb-cal" aria-label="변경 이력 달력">
                    <header className="mydb-cal__head">
                      <button
                        type="button"
                        className="mydb-cal__nav"
                        aria-label="이전 달"
                        disabled={activeMonth <= historyRange.min}
                        onClick={() => { setHistoryMonth(shiftMonth(activeMonth, -1)); setHistoryDay(null) }}
                      >‹</button>
                      <strong>{calendarMonth.label}</strong>
                      <button
                        type="button"
                        className="mydb-cal__nav"
                        aria-label="다음 달"
                        disabled={activeMonth >= historyRange.max}
                        onClick={() => { setHistoryMonth(shiftMonth(activeMonth, 1)); setHistoryDay(null) }}
                      >›</button>
                      <span className="mydb-cal__total">이 달 {calendarMonth.total}건</span>
                    </header>
                    <div className="mydb-cal__grid" role="grid">
                      {['일', '월', '화', '수', '목', '금', '토'].map((label) => (
                        <div key={label} className="mydb-cal__dow" role="columnheader">{label}</div>
                      ))}
                      {calendarMonth.days.map((day, index) => (
                        day.filler ? (
                          <div key={`filler-${index}`} className="mydb-cal__cell mydb-cal__cell--filler" aria-hidden="true" />
                        ) : (
                          <button
                            key={day.key}
                            type="button"
                            role="gridcell"
                            className={`mydb-cal__cell mydb-cal__cell--l${intensityOf(day.count, monthPeak)}${day.isToday ? ' is-today' : ''}${selectedDay === day.key ? ' is-picked' : ''}`}
                            aria-label={`${day.key} 변경 ${day.count}건${day.key && reportsByDate.has(day.key) ? ' · 보고서 있음' : ''}`}
                            aria-pressed={selectedDay === day.key}
                            {...(day.key && reportsByDate.has(day.key) ? { 'data-report': 'yes' } : {})}
                            /* 변경이 0건이어도 보고서가 있으면 눌러서 볼 수 있어야 한다. */
                            disabled={day.count === 0 && !(day.key && reportsByDate.has(day.key))}
                            onClick={() => setHistoryDay(historyDay === day.key ? null : day.key)}
                          >
                            <span className="mydb-cal__day">{day.day}</span>
                            {day.count > 0 && <span className="mydb-cal__count">{day.count}</span>}
                          </button>
                        )
                      ))}
                    </div>
                  </section>
                  <section className="mydb-report-panel" aria-label="My DB 일일 변경 보고서">
                    <header className="mydb-report-panel__head">
                      <h3>일일 변경 보고서</h3>
                      <strong>{formatReportDate(reportDate)}</strong>
                      {shownReport && reportDate !== selectedDay && (
                        <em className="mydb-report-panel__fallback">
                          {formatReportDate(selectedDay)} 보고서는 아직 없습니다
                        </em>
                      )}
                      {shownReport && (
                        <span className="mydb-report-panel__count">
                          {shownReport.totalChanges === 0 ? '변경 없음' : `${shownReport.totalChanges}건`}
                        </span>
                      )}
                    </header>
                    {shownReport ? (
                      <pre className="mydb-report-panel__body">{shownReport.body}</pre>
                    ) : (
                      <p className="mydb-report-panel__empty">
                        아직 작성된 보고서가 없습니다. 보고서는 하루가 끝난 뒤 작성됩니다.
                      </p>
                    )}
                  </section>
                </div>
                <h3 className="mydb-history__daylabel">
                  <strong>{formatReportDate(selectedDay)}</strong>
                  <span>변경 {visibleHistory.length}건</span>
                </h3>
                {visibleHistory.length > 0 ? (
                  <ol className="mydb-history__list">
                    {visibleHistory.map((entry) => (
                      <li key={entry.id} className={`mydb-history-entry mydb-history-entry--${entry.action}`}>
                        <span className="mydb-history-entry__mark" aria-hidden="true" />
                        <div className="mydb-history-entry__content">
                          {/* 무엇이 바뀌었는지(제목)가 주인공이다. 어떤 동작이었는지는
                              태그로 옆에 붙인다 — 예전에는 '자료 추가' 라벨과
                              '…추가했습니다' 문장이 같은 말을 두 번 했다. */}
                          <p className="mydb-history-entry__line">
                            <span className="mydb-history-entry__tag">{historyActionLabel(entry.action)}</span>
                            <b title={entry.subjectTitle}>{entry.subjectTitle}</b>
                            {entry.relatedTitle && (
                              <>
                                <span className="mydb-history-entry__arrow" aria-hidden="true">
                                  {entry.action === 'unlinked' ? '⇢' : '→'}
                                </span>
                                <b title={entry.relatedTitle}>{entry.relatedTitle}</b>
                              </>
                            )}
                          </p>
                          {(entry.detail || historyNote(entry)) && (
                            <small>
                              {[historyNote(entry), entry.detail].filter(Boolean).join(' · ')}
                            </small>
                          )}
                          <span className="mydb-history-entry__acts">
                            {entry.subjectKind === 'file'
                              && entry.subjectId
                              && (entry.action === 'content_changed' || entry.action === 'revision_restored' || entry.action === 'source_synced')
                              && nodesById.get(entry.subjectId)?.kind === 'file' && (
                                <button type="button" onClick={() => openVersionFromHistory(entry)}>변경 보기</button>
                              )}
                            {entry.graphCheckpointId && (
                              <button type="button" onClick={() => setRestoreGraphState({ entry })}>그래프로 복원</button>
                            )}
                          </span>
                        </div>
                        <time dateTime={entry.createdAt} title={new Date(entry.createdAt).toLocaleString('ko-KR')}>
                          {formatHistoryTime(entry.createdAt)}
                        </time>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <div className="mydb-history__empty">이 날짜에는 변경 이력이 없습니다.</div>
                )}
              </div>
            </div>
          )
        )}

        {mode === 'graph' && canvasMenu && canvasRadialPosition && (
          <>
            <div className="mydb-radial-backdrop" onMouseDown={() => setCanvasMenu(null)} />
            <div className="mydb-radial" style={{ left: canvasRadialPosition.x, top: canvasRadialPosition.y }} role="dialog" aria-label="코어 검색 또는 생성">
              {canvasSearchResults.map((node, index) => {
                const ring = Math.floor(index / 12)
                const slot = index % 12
                const radius = 200 + ring * 140
                const angle = (-90 + 30 * slot) * (Math.PI / 180)
                return (
                  <button
                    key={node.id}
                    type="button"
                    className="mydb-radial__bubble"
                    style={{ left: Math.cos(angle) * radius, top: Math.sin(angle) * radius, animationDelay: `${index * 60}ms` }}
                    onMouseDown={(event) => event.stopPropagation()}
                    onClick={() => selectCanvasSearchResult(node)}
                    title={node.title}
                  >
                    <span className={`mydb-radial__dot mydb-radial__dot--${node.kind}`} />
                    <span>{node.title}</span>
                  </button>
                )
              })}
              <div className="mydb-radial__center" onMouseDown={(event) => event.stopPropagation()}>
                <div className="mydb-radial__input">
                  <SearchIcon size={15} />
                  <input
                    autoFocus
                    value={canvasMenu.query}
                    placeholder="검색"
                    aria-label="My DB 검색"
                    onChange={(event) => setCanvasMenu((current) => current ? { ...current, query: event.target.value } : null)}
                    onKeyDown={(event) => {
                      if (event.key === 'Escape') setCanvasMenu(null)
                      if (event.key === 'Enter' && canvasSearchResults.length > 0) selectCanvasSearchResult(canvasSearchResults[0])
                    }}
                  />
                </div>
                {canvasMenu.query.trim() && canvasSearchResults.length === 0 && <p className="mydb-radial__empty">검색 결과가 없습니다.</p>}
                <button
                  type="button"
                  className="mydb-radial__create"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    setCreateCore({
                      x: canvasMenu.x,
                      y: canvasMenu.y,
                      parentCoreId: focusCore?.kind === 'core' ? focusCore.id : null,
                      title: ''
                    })
                    setCanvasMenu(null)
                  }}
                >
                  <span aria-hidden="true">＋</span>
                  새 코어 생성
                </button>
              </div>
            </div>
          </>
        )}

        {mode === 'graph' && createCore && (
          <form className="mydb-popover-form" style={{ left: createCore.x, top: createCore.y }} onSubmit={createCoreSubmit}>
            <label htmlFor="mydb-create-core">새 코어</label>
            <input id="mydb-create-core" autoFocus value={createCore.title} onChange={(event) => setCreateCore((current) => current ? { ...current, title: event.target.value } : null)} maxLength={120} />
            <div>
              <button type="button" onClick={() => setCreateCore(null)}>취소</button>
              <button type="submit" disabled={!createCore.title.trim()}>만들기</button>
            </div>
          </form>
        )}

        {menu && menuNode && (
          <div ref={menuRef} className="mydb-context-menu" data-node-id={menuNode.id} style={{ left: menu.x, top: menu.y }}>
            <div className="mydb-context-menu__title">
              <i className={`mydb-context-menu__dot mydb-context-menu__dot--${menuNode.kind}`} aria-hidden="true" />
              <strong>{menuNode.title}</strong>
            </div>
            <button type="button" onClick={() => { setRenameState({ node: menuNode, title: menuNode.title }); setMenu(null) }}><EditIcon size={14} />이름 변경</button>
            {menuNode.kind === 'file' && <button type="button" onClick={() => { setMenu(null); void openVersions(menuNode) }}><FileIcon size={14} />버전</button>}
            <button type="button" onClick={() => startLink(menuNode.id)}><LinkIcon size={14} />연결</button>
            <button type="button" onClick={() => startDisconnect(menuNode.id)}><UnlinkIcon size={14} />연결취소</button>
            {menuNode.kind === 'core' && menuHasChildren ? (
              <>
                <button type="button" className="mydb-context-menu__danger" onClick={() => { setDeleteState({ node: menuNode, cascade: false, scopeExplicit: true }); setMenu(null) }}><TrashIcon size={14} />코어만 삭제</button>
                <button type="button" className="mydb-context-menu__danger" onClick={() => { setDeleteState({ node: menuNode, cascade: true, scopeExplicit: true }); setMenu(null) }}><TrashIcon size={14} />하위 전체 삭제</button>
              </>
            ) : (
              <button type="button" className="mydb-context-menu__danger" onClick={() => { setDeleteState({ node: menuNode, cascade: false, scopeExplicit: true }); setMenu(null) }}><TrashIcon size={14} />삭제</button>
            )}
          </div>
        )}

      </main>

      {renameState && (
        <div className="mydb-modal-backdrop" role="presentation" onMouseDown={() => setRenameState(null)}>
          <form className="mydb-dialog" onSubmit={renameSubmit} onMouseDown={(event) => event.stopPropagation()}>
            <button type="button" className="mydb-dialog__close" onClick={() => setRenameState(null)} aria-label="닫기"><CloseIcon size={15} /></button>
            <span>이름 변경</span>
            <h2>{nodeTypeLabel(renameState.node.kind)} 이름</h2>
            <input autoFocus value={renameState.title} onChange={(event) => setRenameState((current) => current ? { ...current, title: event.target.value } : null)} maxLength={120} />
            <div className="mydb-dialog__actions">
              <button type="button" onClick={() => setRenameState(null)}>취소</button>
              <button type="submit" className="is-primary" disabled={!renameState.title.trim()}>저장</button>
            </div>
          </form>
        </div>
      )}

      {versionState && (
        <div className="mydb-modal-backdrop" role="presentation" onMouseDown={() => setVersionState(null)}>
          <section className="mydb-dialog mydb-dialog--versions" role="dialog" aria-modal="true" aria-label="파일 버전" onMouseDown={(event) => event.stopPropagation()}>
            <button type="button" className="mydb-dialog__close" onClick={() => setVersionState(null)} aria-label="닫기"><CloseIcon size={15} /></button>
            <span>파일 버전</span>
            <h2 title={versionState.item.title}>{versionState.item.title}</h2>
            <p>My DB가 보관한 원본과 변경본입니다. 복원하면 현재 내용도 새 버전으로 남습니다.</p>
            {versionState.loading && <div className="mydb-version-loading">버전을 확인하는 중입니다.</div>}
            {versionState.error && <div className="mydb-version-error">{versionState.error}</div>}
            {versionRevisions.length > 0 && (
              <div className="mydb-version-layout">
                <ol className="mydb-version-list" aria-label="파일 버전 목록">
                  {versionRevisions.map((revision, index) => (
                    <li key={revision.id} className={versionState.diff?.after.id === revision.id ? 'is-compared' : ''}>
                      <div>
                        <strong>v{revision.sequence}</strong>
                        <span>{formatRevisionReason(revision.reason)} · {formatSize(revision.size)}</span>
                        <time dateTime={revision.createdAt}>{formatHistoryTime(revision.createdAt)}</time>
                      </div>
                      <div className="mydb-version-list__actions">
                        {index < versionRevisions.length - 1 && (
                          <button type="button" onClick={() => void compareVersionPair(versionState.item, versionRevisions[index + 1]!, revision)}>비교</button>
                        )}
                        {index > 0 && (
                          <button type="button" onClick={() => setRestoreRevisionState({ item: versionState.item, revision })}>복원</button>
                        )}
                      </div>
                    </li>
                  ))}
                </ol>
                <section className="mydb-diff" aria-label="버전 차이">
                  {versionState.diff ? (
                    <>
                      <header>
                        <strong>v{versionState.diff.before.sequence} → v{versionState.diff.after.sequence}</strong>
                        {versionState.diff.available && <span><b>+{versionState.diff.addedLines}</b> <i>−{versionState.diff.removedLines}</i></span>}
                      </header>
                      {versionState.diff.available ? (
                        <div className="mydb-diff__lines">
                          {versionState.diff.lines.length > 0 ? versionState.diff.lines.map((line, index) => (
                            <div key={`${line.kind}-${line.oldLine ?? ''}-${line.newLine ?? ''}-${index}`} className={`mydb-diff__line mydb-diff__line--${line.kind}`}>
                              <span>{line.oldLine ?? ''}</span>
                              <span>{line.newLine ?? ''}</span>
                              <code>{line.kind === 'added' ? '+' : line.kind === 'removed' ? '−' : ' '}{line.text}</code>
                            </div>
                          )) : <p className="mydb-diff__empty">두 버전의 내용이 같습니다.</p>}
                          {versionState.diff.truncated && <p className="mydb-diff__truncated">변경 주변만 표시했습니다.</p>}
                        </div>
                      ) : <p className="mydb-diff__empty">{versionState.diff.reason}</p>}
                    </>
                  ) : <p className="mydb-diff__empty">버전 두 개를 선택하면 변경 내용을 비교합니다.</p>}
                </section>
              </div>
            )}
            {!versionState.loading && versionRevisions.length === 0 && !versionState.error && <p>보관된 버전이 없습니다.</p>}
            <div className="mydb-dialog__actions">
              <button type="button" onClick={() => setVersionState(null)}>닫기</button>
            </div>
          </section>
        </div>
      )}

      {restoreRevisionState && (
        <div className="mydb-modal-backdrop" role="presentation" onMouseDown={() => setRestoreRevisionState(null)}>
          <section className="mydb-dialog mydb-dialog--restore" role="dialog" aria-modal="true" aria-label="파일 버전 복원" onMouseDown={(event) => event.stopPropagation()}>
            <button type="button" className="mydb-dialog__close" onClick={() => setRestoreRevisionState(null)} aria-label="닫기"><CloseIcon size={15} /></button>
            <span>버전 복원</span>
            <h2>v{restoreRevisionState.revision.sequence}으로 복원할까요?</h2>
            <p>현재 내용도 새 버전으로 보관되므로, 복원 후에도 다시 되돌릴 수 있습니다.</p>
            <div className="mydb-dialog__actions">
              <button type="button" onClick={() => setRestoreRevisionState(null)}>취소</button>
              <button type="button" className="is-primary" onClick={restoreRevision}>복원</button>
            </div>
          </section>
        </div>
      )}

      {restoreGraphState && (
        <div className="mydb-modal-backdrop" role="presentation" onMouseDown={() => setRestoreGraphState(null)}>
          <section className="mydb-dialog mydb-dialog--restore" role="dialog" aria-modal="true" aria-label="그래프 시점 복원" onMouseDown={(event) => event.stopPropagation()}>
            <button type="button" className="mydb-dialog__close" onClick={() => setRestoreGraphState(null)} aria-label="닫기"><CloseIcon size={15} /></button>
            <span>그래프 시점 복원</span>
            <h2>이 시점의 그래프로 돌아갈까요?</h2>
            <p>코어, 파일 이름, 연결과 휴지통 상태를 이 시점으로 복원합니다. 파일 내용은 각 파일의 버전 복원에서 별도로 되돌릴 수 있습니다.</p>
            <div className="mydb-dialog__actions">
              <button type="button" onClick={() => setRestoreGraphState(null)}>취소</button>
              <button type="button" className="is-primary" onClick={restoreGraph}>그래프로 복원</button>
            </div>
          </section>
        </div>
      )}

      {deleteState && (
        <div className="mydb-modal-backdrop" role="presentation" onMouseDown={() => setDeleteState(null)}>
          <section className="mydb-dialog mydb-dialog--delete" role="dialog" aria-modal="true" aria-label="My DB 항목 삭제" onMouseDown={(event) => event.stopPropagation()}>
            <button type="button" className="mydb-dialog__close" onClick={() => setDeleteState(null)} aria-label="닫기"><CloseIcon size={15} /></button>
            <span>휴지통으로 이동</span>
            <h2>{deleteState.node.title}</h2>
            {deleteHasChildren && !deleteState.scopeExplicit ? (
              <>
                <p>이 코어에는 하위 항목이 있습니다. 삭제 범위를 선택하세요.</p>
                <div className="mydb-delete-scope" role="group" aria-label="코어 삭제 범위">
                  <button
                    type="button"
                    className={!deleteState.cascade ? 'is-active' : ''}
                    onClick={() => setDeleteState((current) => current ? { ...current, cascade: false } : null)}
                  >
                    코어만 삭제
                    <small>하위 항목은 유지</small>
                  </button>
                  <button
                    type="button"
                    className={deleteState.cascade ? 'is-active' : ''}
                    onClick={() => setDeleteState((current) => current ? { ...current, cascade: true } : null)}
                  >
                    하위 항목 포함
                    <small>이 트리의 자료도 휴지통으로 이동</small>
                  </button>
                </div>
              </>
            ) : (
              <p>{deleteState.cascade ? '하위 항목을 포함해 휴지통으로 이동합니다.' : '삭제하면 즉시 휴지통으로 이동하며, 필요하면 나중에 복원할 수 있습니다.'}</p>
            )}
            <div className="mydb-dialog__actions">
              <button type="button" onClick={() => setDeleteState(null)}>취소</button>
              <button type="button" className="is-danger" onClick={deleteNode}>삭제</button>
            </div>
          </section>
        </div>
      )}

      {showTrash && (
        <div className="mydb-modal-backdrop" role="presentation" onMouseDown={() => setShowTrash(false)}>
          <section className="mydb-dialog mydb-dialog--trash" role="dialog" aria-modal="true" aria-label="My DB 휴지통" onMouseDown={(event) => event.stopPropagation()}>
            <button type="button" className="mydb-dialog__close" onClick={() => setShowTrash(false)} aria-label="닫기"><CloseIcon size={15} /></button>
            <span>휴지통</span>
            <h2>삭제한 항목</h2>
            <p className="mydb-trash-policy">
              {settings.myDbTrashRetentionDays > 0
                ? `버린 지 ${settings.myDbTrashRetentionDays}일이 지나면 자동으로 완전 삭제됩니다. 기한은 설정 → DB에서 바꿉니다.`
                : '자동 비우기가 꺼져 있습니다. 설정 → DB에서 보관 기한을 정할 수 있습니다.'}
            </p>
            <div className="mydb-trash-list">
              {(trash?.nodes ?? []).map((node) => (
                <div key={node.id}>
                  <span><i className={node.kind === 'core' ? 'mydb-legend__dot mydb-legend__dot--core' : 'mydb-legend__dot mydb-legend__dot--file'} />{node.title}</span>
                  <span className="mydb-trash-left">{trashLeftLabel(node, settings.myDbTrashRetentionDays)}</span>
                  <span className="mydb-trash-actions">
                    <button type="button" onClick={() => void runAction(async () => {
                      await getMyDbBridge().restoreNode(node.id)
                      setTrash(await getMyDbBridge().trash?.() ?? { nodes: [] })
                    }, '항목을 복원했습니다.')}>복원</button>
                    <button
                      type="button"
                      className="mydb-trash-purge"
                      onClick={() => void purgeFromTrash(node)}
                    >완전 삭제</button>
                  </span>
                </div>
              ))}
              {(trash?.nodes.length ?? 0) === 0 && <p>휴지통이 비어 있습니다.</p>}
            </div>
            <div className="mydb-dialog__actions">
              <button
                type="button"
                className="mydb-trash-empty"
                disabled={(trash?.nodes.length ?? 0) === 0}
                onClick={() => void emptyTrash()}
              >휴지통 비우기</button>
              <button type="button" onClick={() => setShowTrash(false)}>닫기</button>
            </div>
          </section>
        </div>
      )}
    </section>
  )
}

export default MyDbView
