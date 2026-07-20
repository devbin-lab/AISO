export type ApprovalMode = 'manual' | 'read' | 'auto'

export const APPROVAL_MODES: { v: ApprovalMode; label: string; hint: string }[] = [
  { v: 'manual', label: '수동', hint: '읽기·쓰기·편집·삭제 모두 승인' },
  { v: 'read', label: '읽기', hint: '읽기는 자동, 쓰기·편집·삭제는 승인' },
  { v: 'auto', label: '자동', hint: '모든 작업 승인 없이 실행' }
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
  effectivePrompt?: string
  effectiveNegativePrompt?: string
}

export type AgentEvent =
  | { type: 'thinking'; text: string }
  | { type: 'content'; text: string }
  | { type: 'tool_call'; id: string; name: string; args: Record<string, unknown> }
  | { type: 'approval_request'; id: string; name: string; args: Record<string, unknown> }
  | { type: 'tool_result'; id: string; ok: boolean; output: string; rejected?: boolean }
  | { type: 'screenshot'; id: string; data: string }
  | { type: 'image_result'; id: string; image: ComfyGeneratedImage }
  | { type: 'plan'; steps: PlanStep[] }
  | { type: 'notice'; text: string }
  | { type: 'usage'; total: number }
  | { type: 'done' }
  | { type: 'error'; error: string }

export const TOOL_LABEL: Record<string, string> = {
  list_dir: '폴더 목록',
  list_tree: '폴더 구조',
  grep: '내용 검색',
  glob: '파일 찾기',
  read_file: '파일 읽기',
  create_dir: '폴더 생성',
  write_file: '파일 쓰기',
  edit_file: '파일 편집',
  multi_edit: '다중 편집',
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
  update_plan: '계획 갱신',
  generate_image: '이미지 생성'
}
