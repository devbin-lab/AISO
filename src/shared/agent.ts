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

export type AgentEvent =
  | { type: 'thinking'; text: string }
  | { type: 'content'; text: string }
  | { type: 'tool_call'; id: string; name: string; args: Record<string, unknown> }
  | { type: 'approval_request'; id: string; name: string; args: Record<string, unknown> }
  | { type: 'tool_result'; id: string; ok: boolean; output: string; rejected?: boolean }
  | { type: 'screenshot'; id: string; data: string }
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
  update_plan: '계획 갱신'
}
