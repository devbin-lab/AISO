import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
import { buildMonth, countByDay, intensityOf, localDayKey, monthRange, monthsWithHistory, shiftMonth } from '../lib/history-calendar'
import { applyRepulsion, BARNES_HUT_THETA, buildQuadTree } from './mydb-graph/quadtree'
import { buildGraphRoutes } from './mydb-graph/routing'

interface Props {
  active: boolean
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

function historyDescription(entry: MyDbHistoryEntry): string {
  const subject = `“${entry.subjectTitle}”`
  switch (entry.action) {
    case 'core_created':
      return `${subject} 코어를 만들었습니다.`
    case 'imported':
      return `${subject}을(를) My DB에 추가했습니다.`
    case 'renamed':
      return `${subject}의 이름을 변경했습니다.`
    case 'moved_to_trash':
      return `${subject}을(를) 휴지통으로 옮겼습니다.`
    case 'restored':
      return `${subject}을(를) 복원했습니다.`
    case 'purged':
      return `${subject}을(를) 완전히 삭제했습니다. 되돌릴 수 없습니다.`
    case 'linked':
      return `${subject}과(와) “${entry.relatedTitle ?? '항목'}”을(를) 연결했습니다.`
    case 'unlinked':
      return `${subject}과(와) “${entry.relatedTitle ?? '항목'}”의 연결을 해제했습니다.`
    case 'content_changed':
      return `${subject}의 변경 내용을 새 버전으로 보관했습니다.`
    case 'revision_restored':
      return `${subject}을(를) 이전 버전으로 복원했습니다.`
    case 'source_synced':
      return `${subject}의 외부 원본 변경을 My DB에 반영했습니다.`
    case 'source_linked':
      return `${subject}에 외부 원본을 연결했습니다.`
    case 'graph_restored':
      return 'My DB의 코어와 연결 구조를 선택한 시점으로 복원했습니다.'
    case 'exported':
      return `${subject}의 하위 자료를 폴더로 내보냈습니다.`
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

interface CoreGraphStructure {
  children: Map<string, string[]>
  attachedFiles: Map<string, string[]>
  fileCounts: Map<string, number>
  subtreeSizes: Map<string, number>
  heights: Map<string, number>
  coreRadii: Map<string, number>
  structuralCoreEdgeIds: Set<string>
  primaryCoreFileEdgeIds: Set<string>
}

function buildCoreGraphStructure(nodes: MyDbNode[], edges: MyDbEdge[]): CoreGraphStructure {
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

interface GraphLayoutPlan {
  positions: Map<string, Point>
  coreTargets: Map<string, Point>
  fileSlots: Map<string, { coreId: string; angle: number; radius: number }>
  orphanTargets: Map<string, Point>
  structuralCoreEdgeIds: Set<string>
  primaryCoreFileEdgeIds: Set<string>
  secondaryEdgeIds: Set<string>
  center: Point
}

function createInitialLayout(
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

  const subtreeCounts = new Map<string, number>()
  const heights = new Map<string, number>()
  const visiting = new Set<string>()
  const subtreeOf = (id: string): number => {
    const cached = subtreeCounts.get(id)
    if (cached != null) return cached
    if (visiting.has(id)) return 0
    visiting.add(id)
    let count = (attachedFiles.get(id) ?? []).length
    for (const childId of children.get(id) ?? []) count += 1 + subtreeOf(childId)
    visiting.delete(id)
    subtreeCounts.set(id, count)
    return count
  }
  const heightOf = (id: string): number => {
    const cached = heights.get(id)
    if (cached != null) return cached
    let height = 0
    for (const childId of children.get(id) ?? []) height = Math.max(height, 1 + heightOf(childId))
    heights.set(id, height)
    return height
  }
  for (const id of coreIds) {
    subtreeOf(id)
    heightOf(id)
  }

  const spanOf = (id: string): number => Math.max(1, subtreeOf(id))
  // Keep small libraries visually cohesive. Larger subtrees still receive
  // proportionally more arc length, but no longer start from a sparse ring.
  const arcOf = (id: string): number => 54 + Math.sqrt(spanOf(id)) * 24
  const full = Math.PI * 2
  const startAngle = -Math.PI / 2
  const ring = 134
  let centralExtent = 0
  let primaryExtent = 0

  /**
   * A one-child chain inherits a 360° sector in a naïve radial tree. If a
   * lower node later gains siblings, those siblings can fan back through its
   * ancestors and visually knot the hierarchy. Keep every non-root branch in
   * its outward-facing cone instead, while roots remain free to use a full
   * circle.
   */
  const MAX_DESCENDANT_SECTOR = Math.PI * 1.3
  const assignSector = (
    id: string,
    from: number,
    to: number,
    radius: number,
    origin: Point,
    depth: number
  ): void => {
    const angle = (from + to) / 2
    const point = { x: origin.x + Math.cos(angle) * radius, y: origin.y + Math.sin(angle) * radius }
    coreTargets.set(id, point)
    positions.set(id, point)
    centralExtent = Math.max(centralExtent, Math.hypot(point.x - center.x, point.y - center.y))
    const descendants = sorted(children.get(id) ?? [])
    if (descendants.length === 0) return
    const totalArc = descendants.reduce((sum, childId) => sum + arcOf(childId), 0) || 1
    const inheritedWidth = Math.max(0.001, to - from)
    const usableWidth = depth === 0 ? inheritedWidth : Math.min(inheritedWidth, MAX_DESCENDANT_SECTOR)
    const sectorFrom = angle - usableWidth / 2
    // A small angular gutter stays visible even when sibling titles or core
    // radii differ. The ring expands as needed, rather than letting nodes or
    // their structural lines occupy the same ray.
    const gap = descendants.length > 1
      ? Math.min(0.14, usableWidth / Math.max(12, descendants.length * 5))
      : 0
    const contentWidth = Math.max(0.001, usableWidth - gap * Math.max(0, descendants.length - 1))
    const childRadius = Math.max(radius + ring, totalArc / contentWidth)
    let cursor = sectorFrom
    for (const childId of descendants) {
      const slice = (arcOf(childId) / totalArc) * contentWidth
      assignSector(childId, cursor, cursor + slice, childRadius, origin, depth + 1)
      cursor += slice + gap
    }
  }

  const structuralRoots = sorted((roots.length > 0 ? roots : coreIds).filter((id) => subtreeOf(id) > 0))
  if (structuralRoots.length === 1) {
    assignSector(structuralRoots[0]!, startAngle, startAngle + full, 0, center, 0)
    primaryExtent = centralExtent
  } else if (structuralRoots.length > 1) {
    const clusterRadius = (id: string): number => ring * (heightOf(id) + 1) + Math.sqrt(spanOf(id)) * 22
    const clusters = [...structuralRoots].sort((left, right) => clusterRadius(right) - clusterRadius(left) || left.localeCompare(right))
    assignSector(clusters[0]!, startAngle, startAngle + full, 0, center, 0)
    primaryExtent = centralExtent
    const satellites = clusters.slice(1)
    const satelliteArc = satellites.reduce((sum, id) => sum + 2 * clusterRadius(id), 0) || 1
    const largestSatellite = Math.max(...satellites.map(clusterRadius), 0)
    const satelliteRing = 1.2 * Math.max(
      // Keep separate root clusters visibly distinct, but do not let the
      // largest hierarchy push every smaller cluster to the far perimeter.
      largestSatellite + 32,
      primaryExtent * 0.46 + largestSatellite * 0.55 + 30,
      satelliteArc / full
    )
    let cursor = startAngle
    for (const id of satellites) {
      const slice = ((2 * clusterRadius(id)) / satelliteArc) * full
      const angle = cursor + slice / 2
      assignSector(id, startAngle, startAngle + full, 0, {
        x: center.x + Math.cos(angle) * satelliteRing,
        y: center.y + Math.sin(angle) * satelliteRing
      }, 0)
      cursor += slice
    }
  }

  for (const [coreId, fileIds] of attachedFiles) {
    const corePosition = coreTargets.get(coreId)
    if (!corePosition) continue
    const orderedFiles = sorted(fileIds)
    const count = orderedFiles.length
    const coreRadius = coreRadii.get(coreId) ?? 10
    const radius = Math.max(coreRadius + 52, (count * 21) / full)
    orderedFiles.forEach((fileId, index) => {
      const angle = startAngle + (full * index) / Math.max(1, count)
      fileSlots.set(fileId, { coreId, angle, radius })
      positions.set(fileId, {
        x: corePosition.x + Math.cos(angle) * radius,
        y: corePosition.y + Math.sin(angle) * radius
      })
    })
  }

  const orphanIds = sorted(nodes.filter((node) => !positions.has(node.id)).map((node) => node.id))
  const orphanRadius = 1.2 * Math.max(76, primaryExtent * 0.5 + 62)
  orphanIds.forEach((id, index) => {
    const angle = startAngle + (full * index) / Math.max(1, orphanIds.length)
    const point = { x: center.x + Math.cos(angle) * orphanRadius, y: center.y + Math.sin(angle) * orphanRadius }
    orphanTargets.set(id, point)
    positions.set(id, point)
  })
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
        const restLength = structuralCoreEdge
          ? STRUCTURAL_LENGTH + source.radius + target.radius
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
      kineticEnergy = energy
      if (alpha > 0.03) alpha *= 0.998
      return energy
    }

    const SETTLE_ENERGY = Math.max(0.5, nodes.length * 0.03)
    let idleFrames = 0

    const tick = (): void => {
      frameRef.current = null
      if (disposed) return
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
    recenterRef.current = (): void => {
      const { width, height } = sizeRef.current
      // Returning to My DB should show the whole working area at a calm,
      // slightly zoomed-out overview rather than preserving a tight camera.
      // Focus mode immediately applies its own readable minimum afterward.
      const scale = 0.58
      viewportRef.current = {
        scale,
        x: width / 2 - layoutPlan.center.x * scale,
        y: height / 2 - layoutPlan.center.y * scale
      }
      savedViewport = { ...viewportRef.current }
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

function MyDbView({ active }: Props): React.JSX.Element {
  const listHostRef = useRef<HTMLDivElement>(null)
  const workspaceRef = useRef<HTMLElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const [snapshot, setSnapshot] = useState<MyDbSnapshot>(EMPTY_SNAPSHOT)
  const [history, setHistory] = useState<MyDbHistorySnapshot>(EMPTY_HISTORY)
  const [mode, setMode] = useState<MyDbViewMode>('graph')
  // 히스토리 안에서 목록/달력 전환. 달력은 '언제 많이 했나'를, 목록은 '무엇을 했나'를 본다.
  const [historyView, setHistoryView] = useState<'list' | 'calendar'>('list')
  // 달력에서 고른 날. null 이면 전체를 본다.
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
  // 날짜를 고르면 그 날 것만 보여 준다. 목록 보기에도 그대로 적용된다.
  const visibleHistory = useMemo(
    () => (historyDay ? history.entries.filter((entry) => localDayKey(entry.createdAt) === historyDay) : history.entries),
    [history.entries, historyDay]
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
                  <div className="mydb-history__headright">
                    <span>자동 보고 {history.dailyReports.length}개 · 변경 {history.entries.length}개</span>
                    <div className="mydb-history__viewtabs" role="group" aria-label="이력 보기 방식">
                      <button
                        type="button"
                        className={`mydb-history__viewtab${historyView === 'list' ? ' is-active' : ''}`}
                        aria-pressed={historyView === 'list'}
                        onClick={() => setHistoryView('list')}
                      >목록</button>
                      <button
                        type="button"
                        className={`mydb-history__viewtab${historyView === 'calendar' ? ' is-active' : ''}`}
                        aria-pressed={historyView === 'calendar'}
                        onClick={() => setHistoryView('calendar')}
                      >달력</button>
                    </div>
                  </div>
                </header>
                {historyView === 'calendar' && (
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
                            className={`mydb-cal__cell mydb-cal__cell--l${intensityOf(day.count, monthPeak)}${day.isToday ? ' is-today' : ''}${historyDay === day.key ? ' is-picked' : ''}`}
                            aria-label={`${day.key} 변경 ${day.count}건`}
                            aria-pressed={historyDay === day.key}
                            disabled={day.count === 0}
                            onClick={() => setHistoryDay(historyDay === day.key ? null : day.key)}
                          >
                            <span className="mydb-cal__day">{day.day}</span>
                            {day.count > 0 && <span className="mydb-cal__count">{day.count}</span>}
                          </button>
                        )
                      ))}
                    </div>
                  </section>
                  {history.dailyReports.length > 0 && (
                    <section className="mydb-daily-reports mydb-daily-reports--side" aria-label="My DB 일일 변경 보고서">
                      <h3>일일 변경 보고서</h3>
                      {history.dailyReports.map((report) => (
                        <article
                          key={report.reportDate}
                          className={`mydb-daily-report${report.totalChanges === 0 ? ' mydb-daily-report--quiet' : ''}`}
                        >
                          <header>
                            <strong>{formatReportDate(report.reportDate)}</strong>
                            <span>{report.totalChanges === 0 ? '변경 없음' : `${report.totalChanges}건`}</span>
                          </header>
                          <pre>{report.body}</pre>
                        </article>
                      ))}
                    </section>
                  )}
                  </div>
                )}
                {historyView === 'list' && history.dailyReports.length > 0 && (
                  <section className="mydb-daily-reports" aria-label="My DB 일일 변경 보고서">
                    <h3>일일 변경 보고서</h3>
                    {history.dailyReports.map((report) => (
                      <article
                        key={report.reportDate}
                        className={`mydb-daily-report${report.totalChanges === 0 ? ' mydb-daily-report--quiet' : ''}`}
                      >
                        <header>
                          <strong>{formatReportDate(report.reportDate)}</strong>
                          <span>{report.totalChanges === 0 ? '변경 없음' : `${report.totalChanges}건`}</span>
                        </header>
                        <pre>{report.body}</pre>
                      </article>
                    ))}
                  </section>
                )}
                {historyDay && (
                  <div className="mydb-history__filter">
                    <span><strong>{historyDay ? formatReportDate(historyDay) : ''}</strong> 의 이력 {visibleHistory.length}건</span>
                    <button type="button" onClick={() => setHistoryDay(null)}>전체 보기</button>
                  </div>
                )}
                {visibleHistory.length > 0 ? (
                  <ol className="mydb-history__list">
                    {visibleHistory.map((entry) => (
                      <li key={entry.id} className={`mydb-history-entry mydb-history-entry--${entry.action}`}>
                        <span className="mydb-history-entry__mark" aria-hidden="true" />
                        <div className="mydb-history-entry__content">
                          <strong>{historyActionLabel(entry.action)}</strong>
                          <p>{historyDescription(entry)}</p>
                          {entry.detail && <small>{entry.detail}</small>}
                          {entry.subjectKind === 'file'
                            && entry.subjectId
                            && (entry.action === 'content_changed' || entry.action === 'revision_restored' || entry.action === 'source_synced')
                            && nodesById.get(entry.subjectId)?.kind === 'file' && (
                              <button type="button" onClick={() => openVersionFromHistory(entry)}>변경 보기</button>
                            )}
                          {entry.graphCheckpointId && (
                            <button type="button" onClick={() => setRestoreGraphState({ entry })}>그래프로 복원</button>
                          )}
                        </div>
                        <time dateTime={entry.createdAt} title={new Date(entry.createdAt).toLocaleString('ko-KR')}>
                          {formatHistoryTime(entry.createdAt)}
                        </time>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <div className="mydb-history__empty">
                    {historyDay ? '이 날짜에는 변경 이력이 없습니다.' : '아직 변경 이력이 없습니다.'}
                  </div>
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
            <div className="mydb-trash-list">
              {(trash?.nodes ?? []).map((node) => (
                <div key={node.id}>
                  <span><i className={node.kind === 'core' ? 'mydb-legend__dot mydb-legend__dot--core' : 'mydb-legend__dot mydb-legend__dot--file'} />{node.title}</span>
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
              <button type="button" onClick={() => setShowTrash(false)}>닫기</button>
            </div>
          </section>
        </div>
      )}
    </section>
  )
}

export default MyDbView
