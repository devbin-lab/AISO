export type ApprovalMode = 'require' | 'partial' | 'auto'

export const APPROVAL_MODES: { v: ApprovalMode; label: string; hint: string }[] = [
  { v: 'require', label: '승인요구', hint: '모든 쓰기·편집·삭제 승인' },
  { v: 'partial', label: '일부승인', hint: '삭제만 승인, 쓰기·편집은 자동' },
  { v: 'auto', label: '자동승인', hint: '전부 자동 실행' }
]

export type ToolName =
  | 'list_dir'
  | 'list_tree'
  | 'grep'
  | 'glob'
  | 'read_file'
  | 'create_dir'
  | 'write_file'
  | 'edit_file'
  | 'multi_edit'
  | 'delete_file'
  | 'delete_dir'
  | 'move'
  | 'run_command'
  | 'web_fetch'

export type PlanStatus = 'pending' | 'in_progress' | 'completed'
export interface PlanStep {
  content: string
  status: PlanStatus
}

export type AgentEvent =
  | { type: 'thinking'; text: string }
  | { type: 'content'; text: string }
  | { type: 'tool_call'; id: string; name: string; args: Record<string, unknown> }
  | { type: 'approval_request'; id: string; name: string; args: Record<string, unknown> }
  | { type: 'tool_result'; id: string; ok: boolean; output: string; rejected?: boolean }
  | { type: 'screenshot'; id: string; data: string }
  | { type: 'plan'; steps: PlanStep[] }
  | { type: 'notice'; text: string }
  | { type: 'rag_reindex'; state: 'start' | 'done'; count?: number; files?: number; ok?: boolean }
  | { type: 'done' }
  | { type: 'error'; error: string }

export const DESTRUCTIVE_TOOLS: ToolName[] = [
  'write_file',
  'edit_file',
  'multi_edit',
  'delete_file',
  'delete_dir',
  'move',
  'run_command'
]

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
  search_docs: '문서 검색 (RAG)',
  update_plan: '계획 갱신'
}
