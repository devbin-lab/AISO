// 대화방(여러 대화) — main(영속화) ↔ renderer 공용 타입.
// data는 렌더러가 정의하는 kind별 페이로드(JSON 직렬화 가능): 채팅=메시지 배열,
// 에이전트=타임라인/히스토리/계획/작업폴더. main은 이를 불투명하게 저장·CRUD만 한다.
export type ConversationKind = 'chat' | 'agent'

export interface ConversationMeta {
  id: string
  kind: ConversationKind
  title: string
  createdAt: number
  updatedAt: number
  pinned: boolean // 고정 — 목록 맨 위에 유지
}

export interface Conversation extends ConversationMeta {
  data: unknown
}

/** 저장 요청(신규/갱신) — 타임스탬프는 main이 채운다. */
export interface ConversationSave {
  id: string
  kind: ConversationKind
  title: string
  data: unknown
}

/** 에이전트 대화들을 묶는 프로젝트. 작업 폴더는 프로젝트가 아닌 각 대화가 선택한다. */
export interface AgentProject {
  id: string
  title: string
  createdAt: number
  updatedAt: number
  /** 이전 1:1 프로젝트 구조와의 읽기 호환용. 새 UI는 conversations를 사용한다. */
  conversationId: string | null
  conversations: ConversationMeta[]
}
