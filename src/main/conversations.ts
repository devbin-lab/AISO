import { app } from 'electron'
import { join } from 'path'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync } from 'fs'
import { DatabaseSync } from 'node:sqlite'
import type {
  Conversation,
  ConversationKind,
  ConversationMeta,
  ConversationSave
} from '../shared/conversation'
import { appDataFrozen } from './appdata-guard'

// 대화방 영속화 — userData/conversations.db (SQLite, node:sqlite 내장 모듈).
// 이전엔 conversations.json 한 파일을 매번 통째로 읽고 다시 썼는데, 쓰는 중 크래시 시
// 전체 손실 위험이 있었다. SQLite는 각 쓰기가 원자적이고, 대화가 늘어도 인덱스 조회라 빠르다.
// (에이전트 스크린샷 등 큰 데이터는 여전히 렌더러가 저장 전 제거한다.)

interface Row {
  id: string
  kind: string
  title: string
  created_at: number
  updated_at: number
  pinned: number
  data?: string
}

let _db: DatabaseSync | null = null

function dbPath(): string {
  return join(app.getPath('userData'), 'conversations.db')
}
function jsonPath(): string {
  return join(app.getPath('userData'), 'conversations.json')
}

function db(): DatabaseSync {
  if (_db) return _db
  mkdirSync(app.getPath('userData'), { recursive: true })
  const d = new DatabaseSync(dbPath())
  d.exec(
    `CREATE TABLE IF NOT EXISTS conversations (
       id         TEXT PRIMARY KEY,
       kind       TEXT NOT NULL,
       title      TEXT NOT NULL,
       created_at INTEGER NOT NULL,
       updated_at INTEGER NOT NULL,
       pinned     INTEGER NOT NULL DEFAULT 0,
       data       TEXT NOT NULL
     );
     CREATE INDEX IF NOT EXISTS idx_conv_kind ON conversations(kind, pinned, updated_at);`
  )
  _db = d
  migrateFromJson(d) // 구버전 conversations.json 이 있으면 1회 이관
  return _db
}

/** 원본 파일을 접미사 붙여 보관(.bak/.corrupt). 기존 대상은 지워 크로스플랫폼 rename을 보장. */
function archiveJson(jp: string, suffix: string): void {
  try {
    const dest = `${jp}${suffix}`
    if (existsSync(dest)) rmSync(dest, { force: true })
    renameSync(jp, dest)
  } catch (err) {
    console.error('[conversations] 원본 파일 정리 실패:', err)
  }
}

/** 구버전 JSON({id: Conversation})을 SQLite로 1회 이관. 원자적(트랜잭션)이며 커밋 성공 후에만
 *  원본을 .bak로 옮긴다 — 이관 도중 크래시 시 커밋되지 않아(롤백) 다음 실행에서 통째로 재시도된다. */
function migrateFromJson(d: DatabaseSync): void {
  const jp = jsonPath()
  if (!existsSync(jp)) return

  // 1) 파싱 — 손상 파일은 무한 재시도 대신 .corrupt로 격리(데이터는 보존)
  let raw: Record<string, Conversation>
  try {
    raw = JSON.parse(readFileSync(jp, 'utf-8')) as Record<string, Conversation>
  } catch (err) {
    console.error('[conversations] conversations.json 파싱 실패 — .corrupt로 격리:', err)
    archiveJson(jp, '.corrupt')
    return
  }

  // 2) DB에 이미 데이터가 있으면(부분 이관 후 크래시·다운그레이드로 json 재유입 등) 손대지 않는다.
  //    import도 rename도 안 함 → 재유입 파일을 소실·중복 없이 그대로 보존.
  const count = (d.prepare('SELECT COUNT(*) AS n FROM conversations').get() as { n: number }).n
  if (count > 0) return

  // 3) 트랜잭션으로 전체 이관 — 중간에 죽으면 커밋 전이라 자동 롤백(부분 이관 없음)
  const ins = d.prepare(
    `INSERT OR IGNORE INTO conversations (id, kind, title, created_at, updated_at, pinned, data)
     VALUES (?, ?, ?, ?, ?, ?, ?)`
  )
  const now = Date.now()
  let n = 0
  try {
    d.exec('BEGIN')
    for (const c of Object.values(raw)) {
      if (!c || typeof c.id !== 'string' || (c.kind !== 'chat' && c.kind !== 'agent')) continue
      // 값 타입을 전부 보정 → ins.run이 던지지 않게(트랜잭션 중단 방지). data 직렬화만 방어.
      let dataStr = 'null'
      try {
        dataStr = JSON.stringify(c.data ?? null)
      } catch {
        dataStr = 'null'
      }
      ins.run(
        c.id,
        c.kind,
        c.title ?? '새 대화',
        typeof c.createdAt === 'number' ? c.createdAt : now,
        typeof c.updatedAt === 'number' ? c.updatedAt : now,
        c.pinned ? 1 : 0,
        dataStr
      )
      n++
    }
    d.exec('COMMIT')
  } catch (err) {
    try {
      d.exec('ROLLBACK')
    } catch {
      /* 롤백 실패는 무시 — 커밋 안 됐으니 다음 실행이 빈 테이블에서 재시도 */
    }
    console.error('[conversations] JSON 이관 실패(롤백) — 다음 실행에서 재시도:', err)
    return // 원본은 그대로 → 재시도 가능
  }

  // 4) 커밋 성공 후에만 원본을 .bak로 이동(재이관 방지)
  archiveJson(jp, '.bak')
  console.log(`[conversations] conversations.json → SQLite 이관 완료 (${n}건)`)
}

function rowToMeta(r: Row): ConversationMeta {
  return {
    id: r.id,
    kind: r.kind as ConversationKind,
    title: r.title,
    createdAt: r.created_at,
    updatedAt: r.updated_at,
    pinned: r.pinned === 1
  }
}

/** 한 종류의 대화 목록(메타만). 고정 우선, 그다음 최근 갱신순. */
export function listConversations(kind: ConversationKind): ConversationMeta[] {
  try {
    const rows = db()
      .prepare(
        `SELECT id, kind, title, created_at, updated_at, pinned FROM conversations
         WHERE kind = ? ORDER BY pinned DESC, updated_at DESC`
      )
      .all(kind) as unknown as Row[]
    return rows.map(rowToMeta)
  } catch (err) {
    console.error('[conversations] 목록 실패:', err)
    return []
  }
}

/** 대화 전체(data 포함). */
export function getConversation(id: string): Conversation | null {
  try {
    const r = db().prepare('SELECT * FROM conversations WHERE id = ?').get(id) as unknown as
      | Row
      | undefined
    if (!r) return null
    return { ...rowToMeta(r), data: JSON.parse(r.data ?? 'null') }
  } catch (err) {
    console.error('[conversations] 조회 실패:', err)
    return null
  }
}

/** 신규/갱신 upsert. updatedAt은 항상 지금, createdAt·pinned은 기존값 보존(신규는 미고정). */
export function saveConversation(input: ConversationSave): Conversation {
  const now = Date.now()
  let createdAt = now
  let pinned = 0
  try {
    const prev = db()
      .prepare('SELECT created_at, pinned FROM conversations WHERE id = ?')
      .get(input.id) as unknown as Pick<Row, 'created_at' | 'pinned'> | undefined
    if (prev) {
      createdAt = prev.created_at
      pinned = prev.pinned
    }
  } catch {
    /* 조회 실패 시 신규로 취급 */
  }
  const conv: Conversation = {
    id: input.id,
    kind: input.kind,
    title: input.title,
    createdAt,
    updatedAt: now,
    pinned: pinned === 1,
    data: input.data
  }
  if (appDataFrozen()) return conv // 공장초기화 직후 지연 저장 차단(삭제된 DB 되살림 방지)
  try {
    // created_at·pinned·kind는 기존값 유지(갱신 시 title/updated_at/data만 변경)
    db()
      .prepare(
        `INSERT INTO conversations (id, kind, title, created_at, updated_at, pinned, data)
         VALUES (?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET
           title = excluded.title,
           updated_at = excluded.updated_at,
           data = excluded.data`
      )
      .run(input.id, input.kind, input.title, createdAt, now, pinned, JSON.stringify(input.data ?? null))
  } catch (err) {
    console.error('[conversations] 저장 실패:', err)
  }
  return conv
}

/** 고정 토글 — updatedAt은 건드리지 않는다(고정이 갱신시각을 왜곡하지 않게). */
export function setConversationPinned(id: string, pinned: boolean): ConversationMeta | null {
  if (appDataFrozen()) return null
  try {
    const info = db()
      .prepare('UPDATE conversations SET pinned = ? WHERE id = ?')
      .run(pinned ? 1 : 0, id)
    if (info.changes === 0) return null
    const r = db()
      .prepare('SELECT id, kind, title, created_at, updated_at, pinned FROM conversations WHERE id = ?')
      .get(id) as unknown as Row | undefined
    return r ? rowToMeta(r) : null
  } catch (err) {
    console.error('[conversations] 고정 실패:', err)
    return null
  }
}

export function deleteConversation(id: string): void {
  try {
    db().prepare('DELETE FROM conversations WHERE id = ?').run(id)
  } catch (err) {
    console.error('[conversations] 삭제 실패:', err)
  }
}

/** 모든 대화를 지운다(공장초기화). */
export function clearAllConversations(): void {
  try {
    db().prepare('DELETE FROM conversations').run()
  } catch (err) {
    console.error('[conversations] 초기화 실패:', err)
  }
}
