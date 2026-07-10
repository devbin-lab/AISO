// 토큰 사용량 집계 — main(영속화) ↔ renderer(홈 통계·그래프) 공용 타입.
export interface DailyUsage {
  /** 로컬 날짜 YYYY-MM-DD */
  day: string
  /** 그날 사용(생성) 토큰 */
  tokens: number
}

export interface UsageSummary {
  /** 오늘(로컬 날짜) 사용 토큰 */
  today: number
  /** 최근 7일(오늘 포함) 누적 */
  week: number
  /** 최근 30일(오늘 포함) 누적 */
  month: number
  /** 전체 누적 */
  total: number
  /** 최근 30일 일별 시계열(빠진 날은 0으로 채움, 과거→오늘 순) — 그래프용 */
  daily: DailyUsage[]
}
