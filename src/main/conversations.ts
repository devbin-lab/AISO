import { app } from 'electron'
import { join } from 'path'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs'
import type {
  Conversation,
  ConversationKind,
  ConversationMeta,
  ConversationSave
} from '../shared/conversation'

// 대화방 영속화 — userData/conversations.json 에 { [id]: Conversation } 로 저장.
// (개인 앱 규모라 단일 파일로 충분. 에이전트 스크린샷 등 큰 데이터는 렌더러가 저장 전 제거.)
type Store = Record<string, Conversation>

function file(): string {
  return join(app.getPath('userData'), 'conversations.json')
}

function load(): Store {
  try {
    const f = file()
    if (existsSync(f)) return JSON.parse(readFileSync(f, 'utf-8')) as Store
  } catch (err) {
    console.error('[conversations] 불러오기 실패:', err)
  }
  return {}
}

function persist(s: Store): void {
  try {
    const f = file()
    mkdirSync(join(f, '..'), { recursive: true })
    writeFileSync(f, JSON.stringify(s), 'utf-8')
  } catch (err) {
    console.error('[conversations] 저장 실패:', err)
  }
}

function toMeta(c: Conversation): ConversationMeta {
  return {
    id: c.id,
    kind: c.kind,
    title: c.title,
    createdAt: c.createdAt,
    updatedAt: c.updatedAt,
    pinned: c.pinned ?? false // 이전 버전 저장분 호환
  }
}

/** 한 종류의 대화 목록(메타만). 고정 우선, 그다음 최근 갱신순. */
export function listConversations(kind: ConversationKind): ConversationMeta[] {
  return Object.values(load())
    .filter((c) => c.kind === kind)
    .map(toMeta)
    .sort((a, b) => (a.pinned === b.pinned ? b.updatedAt - a.updatedAt : a.pinned ? -1 : 1))
}

/** 대화 전체(data 포함). */
export function getConversation(id: string): Conversation | null {
  return load()[id] ?? null
}

/** 신규/갱신 upsert. updatedAt은 항상 지금, createdAt·pinned은 기존값 보존(신규는 미고정). */
export function saveConversation(input: ConversationSave): Conversation {
  const s = load()
  const now = Date.now()
  const prev = s[input.id]
  const conv: Conversation = {
    id: input.id,
    kind: input.kind,
    title: input.title,
    createdAt: prev?.createdAt ?? now,
    updatedAt: now,
    pinned: prev?.pinned ?? false,
    data: input.data
  }
  s[input.id] = conv
  persist(s)
  return conv
}

/** 고정 토글 — updatedAt은 건드리지 않는다(고정이 갱신시각을 왜곡하지 않게). */
export function setConversationPinned(id: string, pinned: boolean): ConversationMeta | null {
  const s = load()
  const conv = s[id]
  if (!conv) return null
  conv.pinned = pinned
  persist(s)
  return toMeta(conv)
}

export function deleteConversation(id: string): void {
  const s = load()
  if (s[id]) {
    delete s[id]
    persist(s)
  }
}
