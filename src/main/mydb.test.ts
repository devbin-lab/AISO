import assert from 'node:assert/strict'
import test from 'node:test'
import { existsSync } from 'fs'
import { mkdtemp, mkdir, readFile, readdir, rename, rm, writeFile } from 'fs/promises'
import { DatabaseSync } from 'node:sqlite'
import { tmpdir } from 'os'
import { join } from 'path'
import { closeMyDbStorage, configureMyDbStorageRoot, getMyDbStore, myDbClearAll, MyDbStore } from './mydb.ts'

async function temporaryLibrary(): Promise<{ root: string; source: string; store: MyDbStore; dispose: () => Promise<void> }> {
  const root = await mkdtemp(join(tmpdir(), 'aiso-mydb-library-'))
  const source = await mkdtemp(join(tmpdir(), 'aiso-mydb-source-'))
  const store = new MyDbStore(root)
  return {
    root,
    source,
    store,
    dispose: async () => {
      store.close()
      await Promise.all([rm(root, { recursive: true, force: true }), rm(source, { recursive: true, force: true })])
    }
  }
}

test('My DB copies a selected file into its private library and links it to the selected core', async () => {
  const library = await temporaryLibrary()
  try {
    const sourceFile = join(library.source, 'brief.md')
    await writeFile(sourceFile, '# Brief\n', 'utf8')
    const core = library.store.createCore('프로젝트 자료')

    const imported = await library.store.importPaths([sourceFile], core.id)
    assert.equal(imported.createdNodes.length, 1)
    assert.equal(imported.createdEdges.length, 1)

    const file = imported.createdNodes[0]
    assert.equal(file?.kind, 'file')
    assert.equal(file?.title, 'brief.md')
    assert.equal(file?.relativePath, 'files/프로젝트 자료/brief.md')
    assert.equal(JSON.stringify(file).includes(library.source), false)
    assert.equal(await readFile(library.store.resolveItemPath(file!.id), 'utf8'), '# Brief\n')

    const state = library.store.snapshot()
    assert.deepEqual(new Set(state.nodes.map((node) => node.id)), new Set([core.id, file!.id]))
    assert.equal(state.edges[0]?.sourceId, core.id)
    assert.equal(state.edges[0]?.targetId, file!.id)
    assert.equal(state.edges[0]?.relation, 'contains')
  } finally {
    await library.dispose()
  }
})

test('My DB imports folders as a core tree and keeps a child core connected to its parent', async () => {
  const library = await temporaryLibrary()
  try {
    const folder = join(library.source, '수업 자료')
    const child = join(folder, '1주차')
    await mkdir(child, { recursive: true })
    await writeFile(join(child, 'memo.txt'), 'hello', 'utf8')

    const imported = await library.store.importPaths([folder])
    const rootCore = imported.createdNodes.find((node) => node.kind === 'core' && node.title === '수업 자료')
    const childCore = imported.createdNodes.find((node) => node.kind === 'core' && node.title === '1주차')
    const item = imported.createdNodes.find((node) => node.kind === 'file')
    assert.ok(rootCore)
    assert.ok(childCore)
    assert.ok(item)
    assert.ok(imported.createdEdges.some((edge) => edge.sourceId === rootCore!.id && edge.targetId === childCore!.id))
    assert.ok(imported.createdEdges.some((edge) => edge.sourceId === childCore!.id && edge.targetId === item!.id))
  } finally {
    await library.dispose()
  }
})

test('My DB writes one previous-day report and groups changes by their top-level core', async () => {
  const root = await mkdtemp(join(tmpdir(), 'aiso-mydb-daily-report-'))
  let initial: MyDbStore | null = null
  let store: MyDbStore | null = null
  try {
    initial = new MyDbStore(root)
    initial.close()
    initial = null
    const database = new DatabaseSync(join(root, 'library.sqlite3'))
    database.prepare('INSERT INTO mydb_cores (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)')
      .run('root', '게임데이터의설계', '2026-08-16T01:00:00.000Z', '2026-08-16T01:00:00.000Z')
    database.prepare('INSERT INTO mydb_cores (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)')
      .run('child', '수업 중 실습', '2026-08-16T01:00:00.000Z', '2026-08-16T01:00:00.000Z')
    database.prepare(`INSERT INTO mydb_items
      (id, title, extension, file_type, tags_json, size, relative_path, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`)
      .run('file', '6강 실습.xlsx', '.xlsx', 'spreadsheet', '[]', 12, 'files/미분류/6강 실습.xlsx', '2026-08-16T01:00:00.000Z', '2026-08-16T01:00:00.000Z')
    database.prepare(`INSERT INTO mydb_edges (id, source_id, target_id, relation, created_at, updated_at)
      VALUES (?, ?, ?, 'contains', ?, ?)`)
      .run('root-child', 'root', 'child', '2026-08-16T01:00:00.000Z', '2026-08-16T01:00:00.000Z')
    database.prepare(`INSERT INTO mydb_edges (id, source_id, target_id, relation, created_at, updated_at)
      VALUES (?, ?, ?, 'contains', ?, ?)`)
      .run('child-file', 'child', 'file', '2026-08-16T01:00:00.000Z', '2026-08-16T01:00:00.000Z')
    database.prepare(`INSERT INTO mydb_history
      (id, action, subject_id, subject_kind, subject_title, created_at)
      VALUES (?, ?, ?, ?, ?, ?)`)
      .run('history-1', 'imported', 'file', 'file', '6강 실습.xlsx', '2026-08-16T01:00:00.000Z')
    database.close()

    store = new MyDbStore(root)
    const report = store.ensurePreviousDayReport(new Date('2026-08-17T10:00:00+09:00'))
    assert.ok(report)
    assert.equal(report.reportDate, '2026-08-16')
    assert.equal(report.totalChanges, 1)
    assert.match(report.body, /\[게임데이터의설계\]/)
    assert.match(report.body, /\[새로 생성된 코어와 파일\]/)
    assert.match(report.body, /생성된 코어/)
    assert.match(report.body, /수업 중 실습/)
    assert.match(report.body, /생성된 파일/)
    assert.match(report.body, /6강 실습\.xlsx/)
    assert.match(report.body, /게임데이터의설계 > 수업 중 실습 > 6강 실습\.xlsx/)
    assert.equal(store.ensurePreviousDayReport(new Date('2026-08-17T23:00:00+09:00')), null)
    assert.equal(store.history().dailyReports.length, 1)
  } finally {
    initial?.close()
    store?.close()
    await rm(root, { recursive: true, force: true })
  }
})

test('My DB supports rename, reversible deletion, and prevents containment cycles', async () => {
  const library = await temporaryLibrary()
  try {
    const parent = library.store.createCore('상위')
    const child = library.store.createCore('하위', parent.id)
    assert.throws(() => library.store.link(child.id, parent.id, 'contains'), /순환/)

    const sourceFile = join(library.source, 'before.txt')
    await writeFile(sourceFile, 'before', 'utf8')
    const imported = await library.store.importPaths([sourceFile], child.id)
    const item = imported.createdNodes[0]!

    const renamed = await library.store.renameNode(item.id, 'after')
    assert.equal(renamed.title, 'after.txt')
    assert.equal(await readFile(library.store.resolveItemPath(item.id), 'utf8'), 'before')

    library.store.deleteNode(item.id)
    assert.equal(library.store.snapshot().nodes.some((node) => node.id === item.id), false)
    assert.equal(library.store.trash().nodes.some((node) => node.id === item.id), true)

    const restored = library.store.restoreNode(item.id)
    assert.equal(restored.title, 'after.txt')
    assert.equal(library.store.snapshot().nodes.some((node) => node.id === item.id), true)
  } finally {
    await library.dispose()
  }
})

test('My DB promotes legacy core-to-core connections to parent-child containment once', async () => {
  const root = await mkdtemp(join(tmpdir(), 'aiso-mydb-legacy-links-'))
  let first: MyDbStore | null = null
  let reopened: MyDbStore | null = null
  try {
    first = new MyDbStore(root)
    const parent = first.createCore('유니티')
    const child = first.createCore('유니티 학습')
    first.link(parent.id, child.id, 'related')
    first.close()
    first = null
    // Simulate a library made by the version before this migration existed.
    const legacyDatabase = new DatabaseSync(join(root, 'library.sqlite3'))
    legacyDatabase.prepare('DELETE FROM mydb_meta WHERE key = ?').run('legacy_core_links_to_contains_v1')
    legacyDatabase.close()

    reopened = new MyDbStore(root)
    const edge = reopened.snapshot().edges.find((entry) => entry.sourceId === parent.id && entry.targetId === child.id)
    assert.equal(edge?.relation, 'contains')
  } finally {
    reopened?.close()
    first?.close()
    await rm(root, { recursive: true, force: true })
  }
})

test('My DB rejects an empty import path instead of resolving it to the process working directory', async () => {
  const library = await temporaryLibrary()
  try {
    await assert.rejects(library.store.importPaths(['']), /경로/)
  } finally {
    await library.dispose()
  }
})

test('My DB records only its own user-facing changes without retaining source paths', async () => {
  const library = await temporaryLibrary()
  try {
    const first = library.store.createCore('초안')
    const second = library.store.createCore('참조')
    const sourceFile = join(library.source, 'source-only.txt')
    await writeFile(sourceFile, 'private source', 'utf8')

    const imported = await library.store.importPaths([sourceFile], first.id)
    const file = imported.createdNodes[0]!
    const renamed = await library.store.renameNode(first.id, '완성본')
    const edge = library.store.link(renamed.id, second.id)
    library.store.link(renamed.id, second.id)
    library.store.unlink(edge.id)
    library.store.deleteNode(file.id)
    library.store.restoreNode(file.id)

    const entries = library.store.history().entries
    const actions = new Set(entries.map((entry) => entry.action))
    assert.deepEqual(actions, new Set([
      'core_created',
      'imported',
      'renamed',
      'linked',
      'unlinked',
      'moved_to_trash',
      'restored'
    ]))
    assert.equal(entries.filter((entry) => entry.action === 'linked').length, 1)
    assert.equal(entries.some((entry) => entry.action === 'renamed' && entry.subjectTitle === '완성본' && entry.detail === '이전 이름: 초안'), true)
    assert.equal(JSON.stringify(entries).includes(library.source), false)
  } finally {
    await library.dispose()
  }
})

test('My DB preserves file revisions, compares text changes, and can restore an earlier version', async () => {
  const library = await temporaryLibrary()
  try {
    const sourceFile = join(library.source, 'notes.md')
    await writeFile(sourceFile, '# Notes\n- first\n', 'utf8')
    const imported = await library.store.importPaths([sourceFile])
    const file = imported.createdNodes[0]!

    const initialHistory = await library.store.fileHistory(file.id)
    assert.equal(initialHistory.revisions.length, 1)
    assert.equal(initialHistory.revisions[0]?.reason, 'initial')

    await writeFile(library.store.resolveItemPath(file.id), '# Notes\n- revised\n', 'utf8')
    const changedHistory = await library.store.fileHistory(file.id)
    assert.equal(changedHistory.revisions.length, 2)
    assert.equal(changedHistory.revisions[0]?.reason, 'content_changed')

    const diff = await library.store.compareRevisions(
      file.id,
      changedHistory.revisions[1]!.id,
      changedHistory.revisions[0]!.id
    )
    assert.equal(diff.available, true)
    assert.equal(diff.addedLines, 1)
    assert.equal(diff.removedLines, 1)
    assert.ok(diff.lines.some((line) => line.kind === 'added' && line.text === '- revised'))
    assert.ok(diff.lines.some((line) => line.kind === 'removed' && line.text === '- first'))

    await library.store.restoreRevision(file.id, changedHistory.revisions[1]!.id)
    assert.equal(await readFile(library.store.resolveItemPath(file.id), 'utf8'), '# Notes\n- first\n')

    const restoredHistory = await library.store.fileHistory(file.id)
    assert.equal(restoredHistory.revisions.length, 3)
    assert.equal(restoredHistory.revisions[0]?.reason, 'restored')
    const actions = library.store.history().entries.map((entry) => entry.action)
    assert.ok(actions.includes('content_changed'))
    assert.ok(actions.includes('revision_restored'))
    assert.equal(JSON.stringify(restoredHistory).includes(library.source), false)
  } finally {
    await library.dispose()
  }
})

test('My DB records an external editor save without requiring the version screen to be opened', async () => {
  const library = await temporaryLibrary()
  try {
    const sourceFile = join(library.source, 'watched.txt')
    await writeFile(sourceFile, 'original', 'utf8')
    const imported = await library.store.importPaths([sourceFile])
    const file = imported.createdNodes[0]!

    await writeFile(library.store.resolveItemPath(file.id), 'saved outside the app', 'utf8')
    await new Promise<void>((done) => setTimeout(done, 1_900))

    const change = library.store.history().entries.find((entry) => (
      entry.action === 'content_changed' && entry.subjectId === file.id
    ))
    assert.ok(change)
    const revisions = await library.store.fileHistory(file.id)
    assert.equal(revisions.revisions.length, 2)
  } finally {
    await library.dispose()
  }
})

test('My DB copies upstream source changes one way into the managed file and preserves revisions', async () => {
  const library = await temporaryLibrary()
  try {
    const sourceFile = join(library.source, 'upstream.txt')
    await writeFile(sourceFile, 'source v1', 'utf8')
    const imported = await library.store.importPaths([sourceFile])
    const file = imported.createdNodes[0]!

    await writeFile(sourceFile, 'source v2', 'utf8')
    await new Promise<void>((done) => setTimeout(done, 2_100))

    assert.equal(await readFile(library.store.resolveItemPath(file.id), 'utf8'), 'source v2')
    const revisions = await library.store.fileHistory(file.id)
    assert.equal(revisions.revisions[0]?.reason, 'source_synced')
    assert.ok(library.store.history().entries.some((entry) => entry.action === 'source_synced' && entry.subjectId === file.id))

    await writeFile(library.store.resolveItemPath(file.id), 'managed only', 'utf8')
    await new Promise<void>((done) => setTimeout(done, 1_900))
    assert.equal(await readFile(sourceFile, 'utf8'), 'source v2')
  } finally {
    await library.dispose()
  }
})

test('My DB restores a structural checkpoint without deleting newer data permanently', async () => {
  const library = await temporaryLibrary()
  try {
    const root = library.store.createCore('원래 코어')
    const checkpoint = library.store.history().entries.find((entry) => entry.subjectId === root.id)?.graphCheckpointId
    assert.ok(checkpoint)

    const child = library.store.createCore('나중 코어', root.id)
    const sourceFile = join(library.source, 'later.txt')
    await writeFile(sourceFile, 'later', 'utf8')
    const imported = await library.store.importPaths([sourceFile], child.id)
    assert.equal(library.store.snapshot().nodes.length, 3)

    library.store.restoreGraphCheckpoint(checkpoint!)
    const restored = library.store.snapshot()
    assert.deepEqual(restored.nodes.map((node) => node.id), [root.id])
    assert.equal(restored.edges.length, 0)
    const trashIds = new Set(library.store.trash().nodes.map((node) => node.id))
    assert.ok(trashIds.has(child.id))
    assert.ok(trashIds.has(imported.createdNodes[0]!.id))
    assert.ok(library.store.history().entries.some((entry) => entry.action === 'graph_restored'))
  } finally {
    await library.dispose()
  }
})

test('My DB exports a focused core as a nested folder tree without touching source files', async () => {
  const library = await temporaryLibrary()
  const destination = await mkdtemp(join(tmpdir(), 'aiso-mydb-export-'))
  try {
    const root = library.store.createCore('Unity 자료')
    const child = library.store.createCore('학습', root.id)
    const rootFile = join(library.source, 'readme.txt')
    const childFile = join(library.source, 'notes.md')
    await writeFile(rootFile, 'root file', 'utf8')
    await writeFile(childFile, 'child file', 'utf8')
    await library.store.importPaths([rootFile], root.id)
    await library.store.importPaths([childFile], child.id)

    const exported = await library.store.exportCore(root.id, destination)
    assert.deepEqual(exported, {
      folderName: 'Unity 자료',
      exportedCores: 2,
      exportedFiles: 2,
      skippedFiles: 0
    })
    assert.equal(await readFile(join(destination, 'Unity 자료', 'readme.txt'), 'utf8'), 'root file')
    assert.equal(await readFile(join(destination, 'Unity 자료', '학습', 'notes.md'), 'utf8'), 'child file')
    assert.deepEqual(new Set(await readdir(join(destination, 'Unity 자료'))), new Set(['readme.txt', '학습']))
    assert.equal(await readFile(rootFile, 'utf8'), 'root file')
    assert.ok(library.store.history().entries.some((entry) => entry.action === 'exported'))
  } finally {
    await Promise.all([library.dispose(), rm(destination, { recursive: true, force: true })])
  }
})

test('My DB exports files linked with the legacy direct connection relation', async () => {
  const library = await temporaryLibrary()
  const destination = await mkdtemp(join(tmpdir(), 'aiso-mydb-export-related-'))
  try {
    const core = library.store.createCore('연결 자료')
    const sourceFile = join(library.source, 'legacy-linked.txt')
    await writeFile(sourceFile, 'legacy link', 'utf8')
    const imported = await library.store.importPaths([sourceFile])
    const file = imported.createdNodes[0]!
    library.store.link(core.id, file.id, 'related')

    const exported = await library.store.exportCore(core.id, destination)
    assert.equal(exported.exportedFiles, 1)
    assert.equal(exported.skippedFiles, 0)
    assert.equal(await readFile(join(destination, '연결 자료', 'legacy-linked.txt'), 'utf8'), 'legacy link')
  } finally {
    await Promise.all([library.dispose(), rm(destination, { recursive: true, force: true })])
  }
})

test('My DB exports a file when an older connection points from the file to the core', async () => {
  const library = await temporaryLibrary()
  const destination = await mkdtemp(join(tmpdir(), 'aiso-mydb-export-reversed-'))
  try {
    const core = library.store.createCore('역방향 연결')
    const sourceFile = join(library.source, 'reversed.txt')
    await writeFile(sourceFile, 'reversed link', 'utf8')
    const imported = await library.store.importPaths([sourceFile])
    const file = imported.createdNodes[0]!
    library.store.link(file.id, core.id, 'related')

    const exported = await library.store.exportCore(core.id, destination)
    assert.equal(exported.exportedFiles, 1)
    assert.equal(await readFile(join(destination, '역방향 연결', 'reversed.txt'), 'utf8'), 'reversed link')
  } finally {
    await Promise.all([library.dispose(), rm(destination, { recursive: true, force: true })])
  }
})

test('My DB stores imports beneath the selected core and reorganizes an existing direct connection', async () => {
  const library = await temporaryLibrary()
  try {
    const parent = library.store.createCore('게임 개발')
    const child = library.store.createCore('유니티', parent.id)
    const nestedSource = join(library.source, 'scene.unity')
    await writeFile(nestedSource, 'scene', 'utf8')
    const nested = (await library.store.importPaths([nestedSource], child.id)).createdNodes[0]!
    assert.equal(nested.relativePath, 'files/게임 개발/유니티/scene.unity')
    assert.equal(await readFile(library.store.resolveItemPath(nested.id), 'utf8'), 'scene')

    const detachedSource = join(library.source, 'notes.txt')
    await writeFile(detachedSource, 'notes', 'utf8')
    const detached = (await library.store.importPaths([detachedSource])).createdNodes[0]!
    assert.equal(detached.relativePath, 'files/미분류/notes.txt')
    library.store.link(child.id, detached.id, 'related')
    const moved = library.store.snapshot().nodes.find((node) => node.id === detached.id)!
    assert.equal(moved.relativePath, 'files/게임 개발/유니티/notes.txt')
    assert.equal(await readFile(library.store.resolveItemPath(detached.id), 'utf8'), 'notes')
  } finally {
    await library.dispose()
  }
})

test('My DB removes the retired core directory beneath files during startup', async () => {
  const library = await temporaryLibrary()
  let reloaded: MyDbStore | null = null
  let originalClosed = false
  try {
    const core = library.store.createCore('레거시 자료')
    const sourceFile = join(library.source, 'brief.md')
    await writeFile(sourceFile, 'legacy', 'utf8')
    const item = (await library.store.importPaths([sourceFile], core.id)).createdNodes[0]!
    const formerPath = library.store.resolveItemPath(item.id)
    library.store.close()
    originalClosed = true

    const legacyPath = join(library.root, 'files', '코어', '레거시 자료', 'brief.md')
    await mkdir(join(library.root, 'files', '코어', '레거시 자료'), { recursive: true })
    await rename(formerPath, legacyPath)
    const database = new DatabaseSync(join(library.root, 'library.sqlite3'))
    database.prepare('UPDATE mydb_items SET relative_path = ? WHERE id = ?').run('files/코어/레거시 자료/brief.md', item.id)
    database.close()

    reloaded = new MyDbStore(library.root)
    const migrated = reloaded.snapshot().nodes.find((node) => node.id === item.id)!
    assert.equal(migrated.relativePath, 'files/레거시 자료/brief.md')
    assert.equal(await readFile(reloaded.resolveItemPath(item.id), 'utf8'), 'legacy')
    assert.equal(existsSync(join(library.root, 'files', '코어')), false)
  } finally {
    reloaded?.close()
    if (!originalClosed) library.store.close()
    await Promise.all([
      rm(library.root, { recursive: true, force: true }),
      rm(library.source, { recursive: true, force: true })
    ])
  }
})

test('My DB does not report an empty core as a successful file export', async () => {
  const library = await temporaryLibrary()
  const destination = await mkdtemp(join(tmpdir(), 'aiso-mydb-export-empty-'))
  try {
    const core = library.store.createCore('빈 코어')
    await assert.rejects(() => library.store.exportCore(core.id, destination), /내보낼 활성 파일이 없습니다/)
    assert.deepEqual(await readdir(destination), [])
  } finally {
    await Promise.all([library.dispose(), rm(destination, { recursive: true, force: true })])
  }
})

test('My DB upgrades a pre-checkpoint library before creating the checkpoint index', async () => {
  const root = await mkdtemp(join(tmpdir(), 'aiso-mydb-history-migration-'))
  let store: MyDbStore | null = null
  try {
    const legacy = new DatabaseSync(join(root, 'library.sqlite3'))
    legacy.exec(`CREATE TABLE mydb_history (
      id TEXT PRIMARY KEY,
      action TEXT NOT NULL,
      subject_id TEXT,
      subject_kind TEXT,
      subject_title TEXT NOT NULL,
      related_id TEXT,
      related_kind TEXT,
      related_title TEXT,
      detail TEXT,
      created_at TEXT NOT NULL
    );`)
    legacy.close()

    store = new MyDbStore(root)
    const core = store.createCore('마이그레이션 확인')
    const entry = store.history().entries.find((history) => history.subjectId === core.id)
    assert.ok(entry?.graphCheckpointId)
  } finally {
    store?.close()
    await rm(root, { recursive: true, force: true })
  }
})

test('My DB clear removes only library-owned data and starts a fresh library', async () => {
  const root = await mkdtemp(join(tmpdir(), 'aiso-mydb-clear-'))
  const sourceRoot = await mkdtemp(join(tmpdir(), 'aiso-mydb-clear-source-'))
  try {
    await writeFile(join(root, 'keep-me.txt'), 'outside My DB', 'utf8')
    const store = configureMyDbStorageRoot(root)
    const core = store.createCore('삭제 대상')
    const source = join(sourceRoot, 'source.txt')
    await writeFile(source, 'managed copy', 'utf8')
    const imported = await store.importPaths([source], core.id)
    const managedPath = store.resolveItemPath(imported.createdNodes[0]!.id)

    await myDbClearAll()

    assert.equal(getMyDbStore().snapshot().nodes.length, 0)
    assert.equal(await readFile(join(root, 'keep-me.txt'), 'utf8'), 'outside My DB')
    assert.equal(await readFile(source, 'utf8'), 'managed copy')
    await assert.rejects(() => readFile(managedPath, 'utf8'))
  } finally {
    closeMyDbStorage()
    await Promise.all([
      rm(root, { recursive: true, force: true }),
      rm(sourceRoot, { recursive: true, force: true })
    ])
  }
})

// ── 보존 정책 — 무한 누적 차단 ─────────────────────────────────────────

test('My DB keeps graph checkpoints bounded and drops the restore button for pruned ones', async () => {
  // 구조 변경마다 전체 그래프 JSON을 저장하므로 정리가 없으면 라이브러리가 커질수록
  // 저장량이 2차로 증가한다. 복원 버튼은 히스토리 목록에서만 노출되므로 한도 밖
  // 시점은 UI에서 도달할 수 없다 — 지워도 잃는 기능이 없다.
  const library = await temporaryLibrary()
  try {
    const core = library.store.createCore('보존 테스트')
    for (let index = 0; index < 240; index += 1) {
      await library.store.renameNode(core.id, `이름 ${index}`)
    }

    const db = new DatabaseSync(join(library.root, 'library.sqlite3'), { readOnly: true })
    try {
      const total = db.prepare('SELECT COUNT(*) AS n FROM mydb_graph_checkpoints').get() as { n: number }
      assert.ok(total.n <= 200, `체크포인트가 한도를 넘었다: ${total.n}`)
      assert.ok(total.n > 0, '전부 지워졌다 — 최신 시점은 남아야 한다')

      // 잘려나간 시점을 가리키던 히스토리 행은 버튼이 사라져야 한다(id가 NULL).
      const dangling = db.prepare(
        `SELECT COUNT(*) AS n FROM mydb_history
          WHERE graph_checkpoint_id IS NOT NULL
            AND graph_checkpoint_id NOT IN (SELECT id FROM mydb_graph_checkpoints)`
      ).get() as { n: number }
      assert.equal(dangling.n, 0, '없는 시점을 가리키는 복원 버튼이 남았다')
    } finally {
      db.close()
    }
  } finally {
    await library.dispose()
  }
})

test('My DB keeps every item first revision while bounding the rest', async () => {
  // "수정되면 어떻게 수정되었는지 기록이 남아서 복구할 수 있으면 좋겠다"가 요구사항이다.
  // 초기본으로 되돌리기는 한도와 무관하게 언제나 보장돼야 한다.
  const library = await temporaryLibrary()
  try {
    const sourceFile = join(library.source, 'note.md')
    await writeFile(sourceFile, 'v0\n', 'utf8')
    const imported = await library.store.importPaths([sourceFile])
    const file = imported.createdNodes[0]!

    for (let index = 1; index <= 40; index += 1) {
      await writeFile(library.store.resolveItemPath(file.id), `v${index}\n`, 'utf8')
      await library.store.fileHistory(file.id)   // 변경을 감지해 리비전을 남긴다
    }

    const db = new DatabaseSync(join(library.root, 'library.sqlite3'), { readOnly: true })
    try {
      const rows = db.prepare(
        'SELECT sequence FROM mydb_revisions WHERE item_id = ? ORDER BY sequence'
      ).all(file.id) as Array<{ sequence: number }>
      assert.ok(rows.length <= 31, `리비전이 한도를 넘었다: ${rows.length}`)
      assert.equal(rows[0]?.sequence, 1, '초기본이 사라졌다 — 처음 상태로 복구할 수 없다')

      // 남은 리비전의 스냅샷 파일이 실제로 존재해야 한다(행만 남고 파일이 없으면
      // 복구 버튼이 있는데 열 수 없다).
      const kept = db.prepare(
        'SELECT snapshot_relative_path FROM mydb_revisions WHERE item_id = ?'
      ).all(file.id) as Array<{ snapshot_relative_path: string }>
      for (const row of kept) {
        assert.ok(
          existsSync(join(library.root, row.snapshot_relative_path)),
          `남은 리비전의 파일이 없다: ${row.snapshot_relative_path}`
        )
      }
    } finally {
      db.close()
    }
  } finally {
    await library.dispose()
  }
})
