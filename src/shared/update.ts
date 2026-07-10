// 자동 업데이트(electron-updater ↔ GitHub 릴리스) 상태 — main ↔ renderer 공용 타입.
export type UpdateState =
  | 'idle' // 아직 확인 안 함
  | 'dev-disabled' // 개발 모드(비패키징) — 업데이트 비활성
  | 'checking' // 릴리스 확인 중
  | 'up-to-date' // 최신 버전
  | 'available' // 새 버전 있음(다운로드 대기)
  | 'downloading' // 다운로드 중
  | 'downloaded' // 다운로드 완료(재시작하여 설치 대기)
  | 'error' // 오류

export interface UpdateStatus {
  state: UpdateState
  /** 사용 가능한/다운로드된 새 버전 */
  version?: string
  /** 릴리스 노트(문자열일 때만) */
  notes?: string
  /** 다운로드 진행률(0~100) */
  percent?: number
  /** 오류 메시지 */
  message?: string
}
