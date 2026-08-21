import { describe, expect, it } from 'vitest'
import { defaultReportDate, localDayKey, previousDayKey } from '../lib/history-calendar'
import type { MyDbDailyReport, MyDbHistoryEntry } from '../../../shared/mydb'

/**
 * 달력 옆 보고서 패널이 어느 날짜를 보여 주는가.
 *
 * MyDbView 는 통째로 렌더하기엔 무겁고(그래프 캔버스·브리지) 이 규칙은 순수
 * 계산이라, 화면이 쓰는 것과 같은 식을 여기서 고정한다.
 *
 * 규칙: 달력에서 고른 날(historyDay)이 있으면 그 날, 없으면 **전날**.
 * 보고서는 하루가 끝난 뒤 쓰이므로 오늘을 기본으로 두면 늘 비어 있다.
 */

function report(reportDate: string, totalChanges = 0): MyDbDailyReport {
  return { reportDate, generatedAt: `${reportDate}T23:00:00.000Z`, totalChanges, body: `${reportDate} 본문` }
}

/** 화면과 같은 선택 규칙. */
function pick(historyDay: string | null, reports: MyDbDailyReport[], today: Date) {
  const byDate = new Map(reports.map((r) => [r.reportDate, r]))
  const reportDate = historyDay ?? defaultReportDate(byDate.keys(), today)
  return { reportDate, shown: byDate.get(reportDate) ?? null, isYesterday: reportDate === previousDayKey(today) }
}

const TODAY = new Date(2026, 7, 22)
const REPORTS = [report('2026-08-21', 5), report('2026-08-20'), report('2026-08-16', 5)]

describe('보고서 패널이 보여 줄 날짜', () => {
  it('기본은 전날이고 내용이 채워져 있다', () => {
    // 오늘(8/22)로 두면 늘 비어 있다. 전날(8/21) 보고서를 바로 보여 준다.
    const { reportDate, shown, isYesterday } = pick(null, REPORTS, TODAY)
    expect(reportDate).toBe('2026-08-21')
    expect(isYesterday).toBe(true)
    expect(shown?.totalChanges).toBe(5)
  })

  it('전날 보고서가 없으면 있는 것 중 가장 최근을 보여 준다', () => {
    // 앱을 며칠 꺼 두면 전날 보고서가 없다.
    const { reportDate, shown } = pick(null, [report('2026-08-18', 3)], TODAY)
    expect(reportDate).toBe('2026-08-18')
    expect(shown?.totalChanges).toBe(3)
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
    const { shown, isYesterday } = pick('2026-08-19', REPORTS, TODAY)
    expect(shown).toBeNull()
    expect(isYesterday).toBe(false) // '이 날짜의 보고서가 없습니다'
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

describe('아래 목록이 보여 줄 이력', () => {
  /** 화면과 같은 규칙: 달력 보기는 보고서와 같은 하루, 목록 보기는 전체. */
  function visible(view: 'list' | 'calendar', reportDate: string, entries: MyDbHistoryEntry[]) {
    return view === 'calendar'
      ? entries.filter((e) => localDayKey(e.createdAt) === reportDate)
      : entries
  }

  const entries: MyDbHistoryEntry[] = [
    { id: 'a', action: 'imported', subjectTitle: '어제 자료', createdAt: new Date(2026, 7, 21, 10).toISOString() },
    { id: 'b', action: 'imported', subjectTitle: '어제 자료2', createdAt: new Date(2026, 7, 21, 18).toISOString() },
    { id: 'c', action: 'core_created', subjectTitle: '오늘 코어', createdAt: new Date(2026, 7, 22, 9).toISOString() }
  ]

  it('달력 보기는 보고서와 같은 하루만 보여 준다', () => {
    // 화면 전체가 한 날짜를 말한다 — 달력·보고서·목록이 어긋나지 않는다.
    const shown = visible('calendar', '2026-08-21', entries)
    expect(shown.map((e) => e.id)).toEqual(['a', 'b'])
  })

  it('다른 날짜를 고르면 목록도 따라간다', () => {
    expect(visible('calendar', '2026-08-22', entries).map((e) => e.id)).toEqual(['c'])
  })

  it('이력이 없는 날은 빈 목록이다', () => {
    expect(visible('calendar', '2026-08-19', entries)).toHaveLength(0)
  })

  it('목록 보기는 전체를 보여 준다 — 날짜를 고를 달력이 없다', () => {
    expect(visible('list', '2026-08-21', entries)).toHaveLength(3)
  })
})
