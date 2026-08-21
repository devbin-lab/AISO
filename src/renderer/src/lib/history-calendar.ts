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

/** `YYYY-MM` 에서 delta 개월 이동. 연도 경계를 넘어간다. */
export function shiftMonth(yearMonth: string, delta: number): string {
  const [year, month] = yearMonth.split('-').map(Number)
  const at = new Date(year, month - 1 + delta, 1)
  return `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, '0')}`
}

/** 어제(로컬). 보고서는 하루가 끝나야 쓸 수 있으므로 '가장 최근에 완성된 날'이다. */
export function previousDayKey(today: Date = new Date()): string {
  const at = new Date(today)
  at.setHours(0, 0, 0, 0)
  at.setDate(at.getDate() - 1)
  return localDayKey(at)
}

/**
 * 고른 날짜에 대해 **실제로 읽을 수 있는** 보고서 날짜를 정한다.
 *
 * 보고서는 하루가 끝난 뒤에 쓰인다(ensurePreviousDayReport). 그래서 오늘을
 * 고르면 그 날 보고서는 아직 없고, 패널이 늘 비어 보였다. 이력은 오늘 것이
 * 이미 쌓여 있는데 보고서만 없는 어긋남이다.
 *
 * 규칙: 고른 날 보고서가 있으면 그것, 없으면 **그 이전 가장 최근** 보고서.
 * 하나도 없으면 고른 날을 그대로 돌려준다(빈 상태 문구가 판단할 수 있게).
 *
 * 미래로는 절대 가지 않는다 — 8월 16일을 골랐는데 8월 21일 보고서를 보여
 * 주면 무엇을 읽고 있는지 알 수 없다.
 */
export function resolveReportDate(selected: string, reportDates: Iterable<string>): string {
  const dates = [...reportDates]
  if (dates.includes(selected)) return selected
  const past = dates.filter((date) => date < selected).sort()
  return past.length > 0 ? past[past.length - 1]! : selected
}


/** 넘겨 볼 수 있는 최소 창(개월). 이력이 없거나 한 달뿐이어도 달력은 움직여야 한다. */
export const MIN_BROWSE_MONTHS = 12

/**
 * 넘겨 볼 수 있는 달의 범위.
 *
 * 이력이 있는 달 사이로만 건너뛰게 했더니, 이력이 한 달뿐인 사용자는 양쪽
 * 버튼이 동시에 비활성돼 달력이 멈춘 것처럼 보였다. 달력은 빈 달도 넘길 수
 * 있어야 한다. 그래서 이력 유무와 무관하게 최소 12개월 창을 보장한다.
 *
 * 위쪽은 이번 달(또는 그보다 뒤에 기록이 있으면 그 달)까지다 — 미래를 끝없이
 * 넘겨 봐야 빈 달만 나온다.
 */
export function monthRange(entries: MyDbHistoryEntry[], today: Date = new Date()): { min: string; max: string } {
  const current = localDayKey(today).slice(0, 7)
  const months = monthsWithHistory(entries)
  const newest = months.length > 0 && months[0]! > current ? months[0]! : current
  const oldest = months.length > 0 ? months[months.length - 1]! : current
  const window = shiftMonth(newest, -(MIN_BROWSE_MONTHS - 1))
  return { min: oldest < window ? oldest : window, max: newest }
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
