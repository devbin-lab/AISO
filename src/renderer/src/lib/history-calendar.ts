import type { MyDbHistoryEntry } from '../../../shared/mydb'

/**
 * 변경 이력을 달력으로 훑기 위한 순수 계산.
 *
 * 목록은 "가장 최근에 무엇을 했나"에 강하지만, "지난달 언제 자료를 많이 넣었나"
 * 같은 질문에는 약하다. 달력은 그 반대다. 그래서 둘 다 둔다.
 *
 * 날짜 계산은 **로컬 시각** 기준이다 — 사용자가 보는 달력과 어긋나면 안 된다.
 * (ISO 문자열을 그대로 잘라 쓰면 UTC 기준이라 자정 근처 항목이 하루 밀린다.)
 */

export interface CalendarDay {
  /** YYYY-MM-DD (로컬 기준). 빈 칸이면 null. */
  key: string | null
  day: number | null
  /** 이 날짜에 기록된 이력 수. */
  count: number
  /** 이 달에 속하지 않는 앞뒤 여백인가. */
  filler: boolean
  isToday: boolean
}

export interface CalendarMonth {
  year: number
  /** 1-12 */
  month: number
  label: string
  /** 일요일 시작 7열 격자. 항상 7의 배수. */
  days: CalendarDay[]
  total: number
}

/** 로컬 기준 YYYY-MM-DD. */
export function localDayKey(value: string | Date): string {
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

/** 날짜별 이력 건수. 파싱할 수 없는 항목은 조용히 버린다. */
export function countByDay(entries: MyDbHistoryEntry[]): Map<string, number> {
  const counts = new Map<string, number>()
  for (const entry of entries) {
    const key = localDayKey(entry.createdAt)
    if (!key) continue
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  return counts
}

/** 이력이 있는 달을 최신순으로. 이력이 없으면 빈 배열이다. */
export function monthsWithHistory(entries: MyDbHistoryEntry[]): string[] {
  const months = new Set<string>()
  for (const entry of entries) {
    const key = localDayKey(entry.createdAt)
    if (key) months.add(key.slice(0, 7))
  }
  return [...months].sort().reverse()
}

/**
 * 한 달치 격자를 만든다. 일요일 시작, 앞뒤는 빈 칸으로 채워 항상 7의 배수.
 * `today` 를 주입받는 이유는 테스트에서 오늘을 고정하기 위해서다.
 */
export function buildMonth(
  yearMonth: string,
  counts: Map<string, number>,
  today: Date = new Date()
): CalendarMonth {
  const [yearText, monthText] = yearMonth.split('-')
  const year = Number(yearText)
  const month = Number(monthText)
  const first = new Date(year, month - 1, 1)
  const lead = first.getDay()
  const length = new Date(year, month, 0).getDate()
  const todayKey = localDayKey(today)

  const days: CalendarDay[] = []
  for (let i = 0; i < lead; i++) days.push({ key: null, day: null, count: 0, filler: true, isToday: false })
  let total = 0
  for (let day = 1; day <= length; day++) {
    const key = `${yearText}-${monthText}-${String(day).padStart(2, '0')}`
    const count = counts.get(key) ?? 0
    total += count
    days.push({ key, day, count, filler: false, isToday: key === todayKey })
  }
  while (days.length % 7 !== 0) days.push({ key: null, day: null, count: 0, filler: true, isToday: false })

  return { year, month, label: `${year}년 ${month}월`, days, total }
}

/**
 * 건수를 0-4 단계로 바꾼다. 색 농도에 쓴다.
 * 절대 기준이 아니라 그 달의 최대치 대비 상대값이다 — 활동량은 사람마다 다르다.
 */
export function intensityOf(count: number, max: number): number {
  if (count <= 0) return 0
  if (max <= 1) return 4
  return Math.min(4, Math.max(1, Math.ceil((count / max) * 4)))
}
