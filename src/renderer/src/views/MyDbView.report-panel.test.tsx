import { describe, expect, it } from 'vitest'
import { localDayKey } from '../lib/history-calendar'
import type { MyDbDailyReport, MyDbHistoryEntry } from '../../../shared/mydb'

/**
 * 달력 옆 보고서 패널이 어느 날짜를 보여 주는가.
 *
 * MyDbView 는 통째로 렌더하기엔 무겁고(그래프 캔버스·브리지) 이 규칙은 순수
 * 계산이라, 화면이 쓰는 것과 같은 식을 여기서 고정한다.
 *
 * 규칙: 달력에서 고른 날(historyDay)이 있으면 그 날, 없으면 오늘.
 * 보고서는 **전날 것**을 다음 날 만들기 때문에 오늘 보고서는 대개 없다 —
 * 그때 빈 화면 대신 왜 없는지 알려 줘야 한다.
 */

function report(reportDate: string, totalChanges = 0): MyDbDailyReport {
  return { reportDate, generatedAt: `${reportDate}T23:00:00.000Z`, totalChanges, body: `${reportDate} 본문` }
}

/** 화면과 같은 선택 규칙. */
function pick(historyDay: string | null, reports: MyDbDailyReport[], today: Date) {
  const byDate = new Map(reports.map((r) => [r.reportDate, r]))
  const todayKey = localDayKey(today)
  const reportDate = historyDay ?? todayKey
  return { reportDate, shown: byDate.get(reportDate) ?? null, isToday: reportDate === todayKey }
}

const TODAY = new Date(2026, 7, 22)
const REPORTS = [report('2026-08-21', 5), report('2026-08-20'), report('2026-08-16', 5)]

describe('보고서 패널이 보여 줄 날짜', () => {
  it('기본은 오늘이다', () => {
    const { reportDate, isToday } = pick(null, REPORTS, TODAY)
    expect(reportDate).toBe('2026-08-22')
    expect(isToday).toBe(true)
  })

  it('오늘 보고서가 아직 없으면 비어 있다 — 전날 것을 다음 날 만들기 때문', () => {
    const { shown, isToday } = pick(null, REPORTS, TODAY)
    expect(shown).toBeNull()
    // 화면은 이때 '오늘 보고서는 내일 작성됩니다' 를 보여 준다.
    expect(isToday).toBe(true)
  })

  it('달력에서 고른 날의 보고서를 보여 준다', () => {
    const { reportDate, shown } = pick('2026-08-16', REPORTS, TODAY)
    expect(reportDate).toBe('2026-08-16')
    expect(shown?.body).toBe('2026-08-16 본문')
    expect(shown?.totalChanges).toBe(5)
  })

  it('변경이 0건인 날에도 보고서가 있으면 보여 준다', () => {
    // 이런 날은 예전에 달력에서 누를 수조차 없었다(count 0 이면 disabled).
    const { shown } = pick('2026-08-20', REPORTS, TODAY)
    expect(shown).not.toBeNull()
    expect(shown?.totalChanges).toBe(0)
  })

  it('보고서가 없는 날을 고르면 그 사실을 알린다', () => {
    const { shown, isToday } = pick('2026-08-19', REPORTS, TODAY)
    expect(shown).toBeNull()
    expect(isToday).toBe(false) // 오늘이 아니므로 '이 날짜의 보고서가 없습니다'
  })
})

describe('달력에서 누를 수 있는 날', () => {
  /** 화면과 같은 활성 조건: 변경이 있거나, 보고서가 있으면 누를 수 있다. */
  function clickable(dayKey: string, entries: MyDbHistoryEntry[], reports: MyDbDailyReport[]): boolean {
    const count = entries.filter((e) => localDayKey(e.createdAt) === dayKey).length
    const hasReport = reports.some((r) => r.reportDate === dayKey)
    return count > 0 || hasReport
  }

  const entries: MyDbHistoryEntry[] = [
    { id: 'a', action: 'imported', subjectTitle: '자료', createdAt: new Date(2026, 7, 16, 12).toISOString() }
  ]

  it('변경이 있는 날은 누를 수 있다', () => {
    expect(clickable('2026-08-16', entries, REPORTS)).toBe(true)
  })

  it('변경은 없지만 보고서가 있는 날도 누를 수 있다', () => {
    // 보고서를 날짜로 찾아보게 하려면 이게 열려 있어야 한다.
    expect(clickable('2026-08-20', entries, REPORTS)).toBe(true)
  })

  it('둘 다 없는 날은 누를 수 없다', () => {
    expect(clickable('2026-08-19', entries, REPORTS)).toBe(false)
  })
})
