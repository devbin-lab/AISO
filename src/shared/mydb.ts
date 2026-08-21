/**
 * Renderer-safe contracts for Aiso's private My DB library.
 *
 * The renderer never receives an absolute source path.  Files are copied into
 * a user-owned library root by Electron's main process and are represented
 * here only by stable IDs and library-relative paths.
 */

export type MyDbNodeKind = 'core' | 'file'

export type MyDbFileType =
  | 'markdown'
  | 'document'
  | 'slides'
  | 'spreadsheet'
  | 'code'
  | 'image'
  | 'archive'
  | 'other'

export type MyDbRelation = 'contains' | 'related' | 'references' | 'depends_on'

export interface MyDbNode {
  id: string
  kind: MyDbNodeKind
  title: string
  /** Present only for file nodes. */
  fileType?: MyDbFileType
  /** Present only for file nodes.  It is relative to the private library root. */
  relativePath?: string
  size?: number
  tags?: string[]
  createdAt: string
  updatedAt: string
}

export interface MyDbEdge {
  id: string
  sourceId: string
  targetId: string
  relation: MyDbRelation
  createdAt: string
  updatedAt: string
}

export interface MyDbSnapshot {
  nodes: MyDbNode[]
  edges: MyDbEdge[]
}

export interface MyDbTrashSnapshot {
  nodes: MyDbNode[]
}

/** A user-visible change made inside the private My DB library. */
export type MyDbHistoryAction =
  | 'core_created'
  | 'imported'
  | 'renamed'
  | 'moved_to_trash'
  | 'restored'
  | 'purged'
  | 'linked'
  | 'unlinked'
  | 'content_changed'
  | 'revision_restored'
  | 'source_synced'
  | 'source_linked'
  | 'graph_restored'
  | 'exported'

/**
 * Immutable audit data for the My DB history view.
 *
 * Titles are captured when the action happens so history remains meaningful
 * even after a node is renamed, moved to the trash, or permanently absent.
 * Source paths are intentionally never exposed here.
 */
export interface MyDbHistoryEntry {
  id: string
  action: MyDbHistoryAction
  subjectId?: string
  subjectKind?: MyDbNodeKind
  subjectTitle: string
  relatedId?: string
  relatedKind?: MyDbNodeKind
  relatedTitle?: string
  detail?: string
  /** Present for structural changes that can restore the whole My DB graph. */
  graphCheckpointId?: string
  createdAt: string
}

/** One immutable, automatically generated report for the preceding local day. */
export interface MyDbDailyReport {
  /** Local calendar day covered by this report, formatted as YYYY-MM-DD. */
  reportDate: string
  generatedAt: string
  totalChanges: number
  /** Evidence-only report body, grouped by top-level core where possible. */
  body: string
}

export interface MyDbHistorySnapshot {
  entries: MyDbHistoryEntry[]
  dailyReports: MyDbDailyReport[]
}

export type MyDbRevisionReason = 'initial' | 'content_changed' | 'restored' | 'source_synced'

/** An immutable copy of a My DB file at a particular point in time. */
export interface MyDbRevision {
  id: string
  itemId: string
  sequence: number
  contentHash: string
  size: number
  reason: MyDbRevisionReason
  createdAt: string
}

export interface MyDbFileHistory {
  item: MyDbNode
  revisions: MyDbRevision[]
}

/** A restorable My DB structure checkpoint. File contents stay managed by their own revisions. */
export interface MyDbGraphCheckpoint {
  id: string
  reason: string
  nodeCount: number
  edgeCount: number
  createdAt: string
}

export type MyDbDiffLineKind = 'context' | 'added' | 'removed'

export interface MyDbDiffLine {
  kind: MyDbDiffLineKind
  oldLine?: number
  newLine?: number
  text: string
}

/** Text comparison between two immutable My DB revisions. */
export interface MyDbTextDiff {
  itemId: string
  before: MyDbRevision
  after: MyDbRevision
  available: boolean
  reason?: string
  addedLines: number
  removedLines: number
  lines: MyDbDiffLine[]
  truncated: boolean
}

export interface MyDbImportResult {
  createdNodes: MyDbNode[]
  createdEdges: MyDbEdge[]
  skippedPaths: string[]
}

/** Result of exporting one focused core as a regular folder hierarchy. */
export interface MyDbCoreExportResult {
  folderName: string
  exportedCores: number
  exportedFiles: number
  skippedFiles: number
}

export interface MyDbDeleteOptions {
  /** Delete child cores and files that are only linked inside the child tree. */
  cascade?: boolean
}

/** Renderer-visible progress for an explicit My DB file/folder drop. */
export type MyDbDropEvent =
  | { targetId: string; status: 'start' }
  | { targetId: string; status: 'done'; result: MyDbImportResult }
  | { targetId: string; status: 'error'; error: string }

/**
 * Private-library IPC boundary for My DB.
 *
 * My DB is deliberately independent from agent conversations and tool history.
 * The main process owns source paths, library copying, and persistence; this
 * contract exposes only library nodes, relations, and explicit user actions.
 */
export interface MyDbBridge {
  state: () => Promise<MyDbSnapshot>
  history: () => Promise<MyDbHistorySnapshot>
  restoreGraphCheckpoint: (checkpointId: string) => Promise<MyDbGraphCheckpoint>
  /** Selects an upstream file. Only that external file may update My DB. */
  pickSourceForFile: (itemId: string) => Promise<MyDbNode | null>
  /** Opens a save-location picker and exports a core's contains subtree. */
  exportCore: (coreId: string) => Promise<MyDbCoreExportResult | null>
  fileHistory: (itemId: string) => Promise<MyDbFileHistory>
  compareRevisions: (itemId: string, beforeRevisionId: string, afterRevisionId: string) => Promise<MyDbTextDiff>
  restoreRevision: (itemId: string, revisionId: string) => Promise<MyDbNode>
  /** Actual active root, including the default Documents\\Aiso My DB location. */
  storageRoot: () => Promise<string>
  /** Opens the native folder picker for a future storage root. */
  pickStorageRoot: () => Promise<string | null>
  /** Removes only the current My DB database, managed files, and revisions. */
  clearAll: () => Promise<void>
  trash?: () => Promise<MyDbTrashSnapshot>
  createCore: (title: string, parentCoreId?: string | null) => Promise<MyDbNode>
  renameNode: (id: string, title: string) => Promise<MyDbNode>
  deleteNode: (id: string, options?: MyDbDeleteOptions) => Promise<void>
  restoreNode: (id: string) => Promise<MyDbNode>
  /** 휴지통의 항목을 되돌릴 수 없게 지운다. 사용자 전용 — 에이전트에는 노출되지 않는다. */
  purgeNode?: (id: string) => Promise<void>
  link: (sourceId: string, targetId: string, relation?: MyDbRelation) => Promise<MyDbEdge>
  unlink: (edgeId: string) => Promise<void>
  pickFiles: (parentCoreId?: string | null) => Promise<MyDbImportResult>
  pickFolder?: (parentCoreId?: string | null) => Promise<MyDbImportResult>
  importDropped: (paths: string[], parentCoreId?: string | null) => Promise<MyDbImportResult>
  onDrop?: (callback: (event: MyDbDropEvent) => void) => () => void
  openFolder: () => Promise<void>
  openFile?: (id: string) => Promise<void>
  showInFolder?: (id: string) => Promise<void>
}
