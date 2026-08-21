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

// ─── 휴지통 완전 삭제 ────────────────────────────────────────────────────

test('purge refuses a node that is not in the trash', async () => {
  const library = await temporaryLibrary()
  try {
    const core = library.store.createCore('살아 있는 코어')
    await assert.rejects(
      () => library.store.purgeNode(core.id),
      /휴지통에 있는 항목만/,
      '휴지통을 거치지 않고 지울 수 있으면 두 단계 삭제가 아니다'
    )
    assert.equal(library.store.snapshot().nodes.some((n) => n.id === core.id), true)
  } finally {
    await library.dispose()
  }
})

test('purging a file removes its row, its managed copy and every revision snapshot', async () => {
  const library = await temporaryLibrary()
  try {
    const sourceFile = join(library.source, 'notes.md')
    await writeFile(sourceFile, '# v1\n', 'utf8')
    const imported = await library.store.importPaths([sourceFile], null)
    const file = imported.createdNodes[0]!

    // 두 번째 버전을 만들어 리비전 스냅숏이 실제로 존재하게 한다.
    const managed = join(library.root, file.relativePath!)
    await writeFile(managed, '# v2\n', 'utf8')
    const history = await library.store.fileHistory(file.id)
    assert.ok(history.revisions.length >= 2, '리비전이 만들어져야 이 테스트가 의미를 갖는다')

    const revisionDir = join(library.root, 'revisions', file.id)
    assert.equal(existsSync(managed), true)
    assert.equal(existsSync(revisionDir), true)

    library.store.deleteNode(file.id)
    await library.store.purgeNode(file.id)

    assert.equal(existsSync(managed), false, '보관 파일이 남았다')
    assert.equal(existsSync(revisionDir), false, '버전 스냅숏이 남았다')
    assert.equal(library.store.trash().nodes.some((n) => n.id === file.id), false)
    assert.equal(library.store.snapshot().nodes.some((n) => n.id === file.id), false)
  } finally {
    await library.dispose()
  }
})

test('purging never touches the user original outside My DB', async () => {
  // My DB는 원본의 복사본만 관리한다. 완전 삭제가 사용자의 원본까지 지우면 재앙이다.
  const library = await temporaryLibrary()
  try {
    const sourceFile = join(library.source, 'original.md')
    await writeFile(sourceFile, '# 원본\n', 'utf8')
    const file = (await library.store.importPaths([sourceFile], null)).createdNodes[0]!

    library.store.deleteNode(file.id)
    await library.store.purgeNode(file.id)

    assert.equal(existsSync(sourceFile), true, '사용자 원본을 지웠다')
    assert.equal(await readFile(sourceFile, 'utf8'), '# 원본\n')
  } finally {
    await library.dispose()
  }
})

test('purge keeps the history trail and records the deletion', async () => {
  // "수정되면 어떻게 수정되었는지 기록이 남아서" — 완전 삭제는 대상만 지우고 기록은 남긴다.
  const library = await temporaryLibrary()
  try {
    const sourceFile = join(library.source, 'kept.md')
    await writeFile(sourceFile, 'x', 'utf8')
    const file = (await library.store.importPaths([sourceFile], null)).createdNodes[0]!

    library.store.deleteNode(file.id)
    await library.store.purgeNode(file.id)

    const actions = library.store.history().entries.map((e) => e.action)
    assert.equal(actions.includes('imported'), true, '과거 기록이 사라졌다')
    assert.equal(actions.includes('moved_to_trash'), true)
    assert.equal(actions.includes('purged'), true)
    const purged = library.store.history().entries.find((e) => e.action === 'purged')
    assert.equal(purged?.subjectTitle, 'kept.md', '무엇을 지웠는지 남아야 한다')
  } finally {
    await library.dispose()
  }
})

test('restoring a checkpoint taken before a purge does not resurrect the purged node', async () => {
  // 이것이 완전 삭제를 막고 있던 이유다. 체크포인트 복원은 스냅숏의 모든 행을
  // 다시 INSERT 하므로, 묘비가 없으면 파일 없는 노드가 그래프에 되살아난다.
  const library = await temporaryLibrary()
  try {
    const sourceFile = join(library.source, 'doomed.md')
    await writeFile(sourceFile, 'x', 'utf8')
    const file = (await library.store.importPaths([sourceFile], null)).createdNodes[0]!
    const core = library.store.createCore('시점 확보용')  // 체크포인트를 만드는 동작

    const checkpointId = library.store.history().entries
      .find((e) => e.graphCheckpointId)?.graphCheckpointId
    assert.ok(checkpointId, '체크포인트가 있어야 이 테스트가 의미를 갖는다')

    library.store.deleteNode(file.id)
    await library.store.purgeNode(file.id)
    library.store.restoreGraphCheckpoint(checkpointId!)

    const all = [...library.store.snapshot().nodes, ...library.store.trash().nodes]
    assert.equal(all.some((n) => n.id === file.id), false, '완전 삭제한 노드가 되살아났다')
    // 같은 시점의 다른 노드는 정상 복원되어야 한다 — 가드가 과하게 작동하면 안 된다.
    assert.equal(
      [...library.store.snapshot().nodes, ...library.store.trash().nodes].some((n) => n.id === core.id),
      true
    )
  } finally {
    await library.dispose()
  }
})

test('checkpoint restore skips a file whose bytes vanished outside My DB', async () => {
  // 묘비와 별개의 안전망. 어떤 이유로든 보관 파일이 사라졌다면 되살리지 않는다 —
  // 그래프에는 보이지만 아무것도 열 수 없는 노드가 생기기 때문이다.
  const root = await mkdtemp(join(tmpdir(), 'aiso-mydb-library-'))
  const source = await mkdtemp(join(tmpdir(), 'aiso-mydb-source-'))
  const first = new MyDbStore(root)
  let reopened: MyDbStore | null = null
  try {
    const sourceFile = join(source, 'lost.md')
    await writeFile(sourceFile, 'x', 'utf8')
    const file = (await first.importPaths([sourceFile], null)).createdNodes[0]!
    first.createCore('시점 확보용')
    const checkpointId = first.history().entries.find((e) => e.graphCheckpointId)?.graphCheckpointId
    assert.ok(checkpointId, '체크포인트가 있어야 이 테스트가 의미를 갖는다')

    first.close()
    await rm(join(root, file.relativePath!), { force: true })

    reopened = new MyDbStore(root)
    reopened.restoreGraphCheckpoint(checkpointId!)
    // 살아 있는 그래프에는 올라오지 않는다 — 열 수 없는 노드를 보여주면 안 된다.
    assert.equal(
      reopened.snapshot().nodes.some((n) => n.id === file.id), false,
      '바이트가 없는 파일을 살아 있는 그래프에 되살렸다'
    )
    // 다만 행 자체를 지우지는 않는다. 휴지통에 남겨 사용자가 판단하게 한다 —
    // 파일이 없어졌다는 이유로 시스템이 기록을 소멸시키면 복구 여지가 사라진다.
    assert.equal(
      reopened.trash().nodes.some((n) => n.id === file.id), true,
      '휴지통에도 남지 않아 사용자가 상황을 알 수 없다'
    )
  } finally {
    reopened?.close()
    await Promise.all([
      rm(root, { recursive: true, force: true }),
      rm(source, { recursive: true, force: true })
    ])
  }
})

test('purging a core drops its edges so no dangling link survives a restore', async () => {
  const library = await temporaryLibrary()
  try {
    const core = library.store.createCore('지울 코어')
    const sourceFile = join(library.source, 'child.md')
    await writeFile(sourceFile, 'x', 'utf8')
    const child = (await library.store.importPaths([sourceFile], core.id)).createdNodes[0]!
    assert.equal(library.store.snapshot().edges.length, 1)

    library.store.deleteNode(core.id)
    await library.store.purgeNode(core.id)

    assert.equal(library.store.snapshot().edges.length, 0, '끊긴 코어를 가리키는 엣지가 남았다')
    // 자식은 그대로 살아 있어야 한다 — 코어 하나를 지웠다고 자료가 사라지면 안 된다.
    const all = [...library.store.snapshot().nodes, ...library.store.trash().nodes]
    assert.equal(all.some((n) => n.id === child.id), true, '코어를 지우면서 자식 자료까지 없앴다')
  } finally {
    await library.dispose()
  }
})

test('purge is idempotent enough to survive a double click', async () => {
  const library = await temporaryLibrary()
  try {
    const core = library.store.createCore('두 번 눌림')
    library.store.deleteNode(core.id)
    await library.store.purgeNode(core.id)
    await assert.rejects(() => library.store.purgeNode(core.id), /찾을 수 없습니다|없습니다/)
  } finally {
    await library.dispose()
  }
})


test('일일 보고서가 추가된 자료를 형식·크기·출처까지 알아볼 수 있게 적는다', async () => {
  const library = await temporaryLibrary()
  try {
    // 외부에서 가져온 자료 두 개 — 이름만으로는 무엇인지 알기 어렵다.
    const plan = join(library.source, '강의계획서.pdf')
    const sheet = join(library.source, '예산표.xlsx')
    await writeFile(plan, Buffer.alloc(240_000, 1))
    await writeFile(sheet, Buffer.alloc(18_000, 1))
    const core = library.store.createCore('2026-2학기')
    await library.store.importPaths([plan, sheet], core.id)

    const tomorrow = new Date(Date.now() + 24 * 60 * 60 * 1000)
    const report = library.store.ensurePreviousDayReport(tomorrow)
    assert.ok(report)

    // 무엇이 들어왔는지 — 이름
    assert.match(report.body, /강의계획서\.pdf/)
    assert.match(report.body, /예산표\.xlsx/)
    // 어떤 형식인지
    assert.match(report.body, /PDF|XLSX|SPREADSHEET|DOCUMENT/i)
    // 얼마나 큰지 — 크기 표기가 붙는다
    assert.match(report.body, /\d+(\.\d+)?(KB|MB)/)
    // 어디서 온 자료인지 — 외부 원본 경로
    assert.match(report.body, /원본: /)
    assert.ok(report.body.includes(plan), '외부 원본 경로가 보고서에 남아야 한다')
    // 요약 줄이 코어와 자료를 구분한다
    assert.match(report.body, /새 자료 2개/)
    assert.match(report.body, /추가 용량 /)
  } finally {
    await library.dispose()
  }
})

test('자료 추가 이력이 어떤 파일이 들어왔는지 이름으로 남긴다', async () => {
  const library = await temporaryLibrary()
  try {
    const a = join(library.source, '회의록.md')
    const b = join(library.source, '설문결과.csv')
    await writeFile(a, '# 회의록', 'utf8')
    await writeFile(b, 'a,b', 'utf8')
    const core = library.store.createCore('연구')
    await library.store.importPaths([a, b], core.id)

    // 예전에는 "2개 항목 (파일 2개)" 뿐이라 무엇이 들어왔는지 알 수 없었다.
    const imported = library.store.history().entries.find((entry) => entry.action === 'imported')
    assert.ok(imported)
    assert.match(imported.detail ?? '', /회의록\.md/)
    assert.match(imported.detail ?? '', /설문결과\.csv/)
  } finally {
    await library.dispose()
  }
})

test('이미 저장된 보고서는 생성 로직이 바뀌어도 그대로 남는다', async () => {
  // 보고서는 본문을 텍스트로 굳혀 저장한다. 새 형식이 과거 보고서를 덮어쓰거나
  // 다시 만들면 사용자가 쌓아 온 기록이 조용히 바뀐다 — 그러면 안 된다.
  // temporaryLibrary 의 dispose 는 스토어를 닫는데, 이 테스트는 스토어를 두 번
  // 열었다 닫으므로 직접 정리한다(이중 close 는 'database is not open' 이다).
  const root = await mkdtemp(join(tmpdir(), 'aiso-mydb-legacy-'))
  const seed = new MyDbStore(root)   // 스키마를 만든 뒤 닫는다
  seed.close()
  try {
    const database = new DatabaseSync(join(root, 'library.sqlite3'))
    const legacyBody = '2026-08-10 My DB 변경 보고\n총 3건의 변경 · 새 항목 2개\n\n[예전 형식 본문]'
    database
      .prepare(`INSERT INTO mydb_daily_reports (report_date, generated_at, total_changes, body)
       VALUES (?, ?, ?, ?)`)
      .run('2026-08-10', '2026-08-11T00:10:00.000Z', 3, legacyBody)
    database.close()

    const reopened = new MyDbStore(root)
    try {
      const stored = reopened.history().dailyReports.find((r) => r.reportDate === '2026-08-10')
      assert.ok(stored, '예전 보고서가 그대로 조회되어야 한다')
      assert.equal(stored.body, legacyBody, '예전 본문이 한 글자도 바뀌면 안 된다')
      assert.equal(stored.totalChanges, 3)

      // 같은 날짜를 다시 만들려 해도 덮어쓰지 않는다.
      const again = reopened.ensurePreviousDayReport(new Date('2026-08-11T10:00:00+09:00'))
      assert.equal(again, null, '이미 있는 날짜는 다시 만들지 않는다')
      const after = reopened.history().dailyReports.find((r) => r.reportDate === '2026-08-10')
      assert.equal(after?.body, legacyBody)
    } finally {
      reopened.close()
    }
  } finally {
    // Windows 는 방금 닫은 sqlite 핸들을 잠시 붙들고 있어 unlink 가 EBUSY 로 실패한다.
    // 검증은 이미 끝났고 남는 건 OS 임시 폴더뿐이므로 정리 실패로 테스트를 깨뜨리지 않는다.
    await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 }).catch(() => {})
  }
})
