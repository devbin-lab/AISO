export type ApprovalMode = 'manual' | 'read' | 'auto'

/** 백엔드의 실제 툴 레지스트리에서 읽어 오는 내장 Agent 툴 목록 항목. */
export type AgentToolCategory =
  | 'plan'
  | 'files'
  | 'programming'
  | 'execution'
  | 'research'
  | 'automation'
  | 'rag'
  | 'mydb'
  | 'discord'
  | 'image'

/** 해당 툴이 Agent에 노출되기 위한 기본 조건. */
export type AgentToolAvailability = 'always' | 'workspace' | 'rag' | 'discord' | 'image'

export interface AgentToolParameter {
  name: string
  description: string
}

export interface AgentToolCatalogEntry {
  name: string
  description: string
  category: AgentToolCategory
  parameters: AgentToolParameter[]
  /** 작업 폴더를 변경할 수 있는지. ComfyUI 출력처럼 작업 폴더 밖 결과는 포함하지 않는다. */
  mutates: boolean
  /** 각 승인 모드에서 실행 전 사용자 승인이 필요한지. */
  approval: Record<ApprovalMode, boolean>
  availability: AgentToolAvailability
  /** 현재 세션에서 노출되기 위한 추가 조건 또는 주의 사항. */
  requirements: string[]
}

export interface AgentToolCatalog {
  tools: AgentToolCatalogEntry[]
}

export const APPROVAL_MODES: { v: ApprovalMode; label: string; hint: string }[] = [
  { v: 'manual', label: '수동', hint: '읽기·쓰기·편집·삭제 모두 승인' },
  { v: 'read', label: '읽기', hint: '읽기는 자동, 쓰기·편집·삭제는 승인' },
  { v: 'auto', label: '자동', hint: '허용한 모든 도구를 승인 없이 실행' }
]

export type PlanStatus = 'pending' | 'in_progress' | 'completed'
export interface PlanStep {
  content: string
  status: PlanStatus
}

export interface ComfyWorkflowNodeSnapshot {
  class_type: string
  inputs: Record<string, unknown>
}

export type ComfyWorkflowSnapshot = Record<string, ComfyWorkflowNodeSnapshot>

export interface ComfyPromptPolicySnapshot {
  id: string
  label: string
  description: string
  addedPositive: string[]
  addedNegative: string[]
}

export interface ComfyPipelineSnapshot {
  source: 'aiso-built-in' | 'user-workflow'
  /** 표시된 결과 이미지의 출력 노드에서 역추적한 경로 노드 수. */
  nodeCount: number
  vaeDecode: boolean
  negativeMode: 'conditioning' | 'positive-constraints' | 'connected-empty' | 'not-connected'
  /** 확대 여부가 아니라 스케일 처리 노드가 결과 경로에 포함되는지만 나타낸다. */
  scaleProcess: boolean
  processingNodes: string[]
}

export interface ComfyGeneratedImage {
  jobId: string
  filename: string
  subfolder: string
  storageType: 'output' | 'temp'
  baseUrl: string
  profileId: string
  profileName: string
  modelName: string
  selectionReason: string
  prompt: string
  negativePrompt: string
  seed: string
  /** Initial latent dimensions before any verified Aiso refinement pass. */
  baseWidth?: number
  baseHeight?: number
  width: number
  height: number
  steps: number
  cfg: number
  sampler: string
  scheduler: string
  /** ComfyUI /prompt에 실제로 제출한 API 그래프. 구형 결과에는 없다. */
  workflow?: ComfyWorkflowSnapshot
  /** 사용자 요청을 생성 프롬프트로 바꿀 때 적용한 결정론적 정책 기록. */
  promptPolicy?: ComfyPromptPolicySnapshot
  originalPrompt?: string
  originalNegativePrompt?: string
  effectivePrompt?: string
  effectiveNegativePrompt?: string
  /** 실제 제출 그래프에서 계산한 파이프라인 기능 요약. */
  pipeline?: ComfyPipelineSnapshot
}

export type AgentEvent =
  | { type: 'thinking'; text: string }
  | { type: 'content'; text: string }
  | ({ type: 'tool_call'; name: string; args: Record<string, unknown> } & AgentToolIdentity)
  | ({ type: 'approval_request'; name: string; args: Record<string, unknown> } & AgentToolIdentity)
  | ({
      type: 'tool_result'
      ok: boolean
      output: string
      /** 사용자가 명시적으로 거부했다. */
      rejected?: boolean
      /**
       * 승인 요청에 응답이 오지 않았다. `rejected`와 반드시 구분한다 — 사용자는
       * 거부한 적이 없고, 자리를 비웠거나 창을 닫았을 뿐이다.
       */
      expired?: boolean
      reused?: boolean
    } & AgentToolIdentity)
  | { type: 'screenshot'; id: string; assistantTurnId: string; data: string }
  | {
      type: 'image_result'
      id: string
      /** The Agent execution that produced this image. Never render a stale turn's result. */
      assistantTurnId: string
      image: ComfyGeneratedImage
    }
  | { type: 'plan'; steps: PlanStep[] }
  | {
      type: 'notice'
      text: string
      /** Progress feedback for the current stream only; clear it at the next tool/terminal event. */
      transient?: boolean
    }
  | { type: 'usage'; total: number }
  | { type: 'done' }
  | { type: 'error'; error: string }

export interface AgentToolIdentity {
  /** Legacy rendering correlation; equal to executionId. */
  id: string
  executionId: string
  approvalId: string
  providerToolCallId: string
  assistantTurnId: string
}

export const TOOL_LABEL: Record<string, string> = {
  get_system_time: '현재 시각 확인',
  list_calendar_events: '캘린더 일정 목록',
  create_calendar_event: '캘린더 일정 등록',
  manage_calendar_event: '캘린더 일정 수정·완료·삭제',
  list_mydb_library: 'My DB 내용 조회',
  list_mydb_history: 'My DB 변경 이력 조회',
  list_mydb_trash: 'My DB 휴지통 조회',
  restore_mydb_trash_node: 'My DB 휴지통 항목 복구',
  list_dir: '폴더 목록',
  list_tree: '폴더 구조',
  grep: '내용 검색',
  glob: '파일 찾기',
  read_file: '파일 읽기',
  convert_document: '문서 HTML 변환',
  analyze_document_calendar: '문서 일정 만들기',
  create_dir: '폴더 생성',
  write_file: '파일 쓰기',
  edit_file: '파일 편집',
  multi_edit: '다중 편집',
  write_code_file: '코드 파일 작성',
  edit_code_file: '코드 파일 편집',
  multi_edit_code_file: '코드 일괄 편집',
  delete_file: '파일 삭제',
  delete_dir: '폴더 삭제',
  move: '이동·이름변경',
  run_web: '웹 실행·검증',
  run_code: '코드 실행·검증',
  run_command: '명령 실행',
  web_fetch: '웹 문서 가져오기',
  web_search: '웹 검색',
  create_skill: '스킬 만들기',
  run_skill: '스킬 실행',
  search_docs: '문서 검색 (RAG)',
  discord_server_map: '디스코드 서버 구조 확인',
  discord_server_apply: '디스코드 서버 구성 적용',
  discord_send: '디스코드 메시지 전송',
  discord_schedule_add: '디스코드 예약 추가',
  discord_channel_report_add: '디스코드 채널 대화 보고 예약',
  discord_schedule_list: '디스코드 예약 목록',
  discord_schedule_remove: '디스코드 예약 삭제',
  update_plan: '계획 갱신',
  generate_image: '이미지 생성'
}
