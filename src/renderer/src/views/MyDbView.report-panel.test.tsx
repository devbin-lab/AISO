import { describe, expect, it } from 'vitest'
import { localDayKey, previousDayKey, resolveReportDate } from '../lib/history-calendar'
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
  const selectedDay = historyDay ?? previousDayKey(today)
  const reportDate = resolveReportDate(selectedDay, byDate.keys())
  return {
    selectedDay,
    reportDate,
    shown: byDate.get(reportDate) ?? null,
    // 고른 날과 읽는 날이 다르면 화면이 그 사실을 밝힌다.
    fellBack: reportDate !== selectedDay,
    isYesterday: reportDate === previousDayKey(today)
  }
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

  it('오늘을 골라도 패널이 비지 않는다 — 전날 보고서로 물러난다', () => {
    // 스크린샷의 상황: 8/22(오늘)을 고르면 '이 날짜의 보고서가 없습니다' 만 떴다.
    // 이력은 오늘 것이 이미 있는데 보고서만 없는 어긋남이었다.
    const { selectedDay, reportDate, shown, fellBack } = pick('2026-08-22', REPORTS, TODAY)
    expect(selectedDay).toBe('2026-08-22')
    expect(reportDate).toBe('2026-08-21')
    expect(shown?.totalChanges).toBe(5)
    expect(fellBack).toBe(true)
  })

  it('보고서가 없는 날을 고르면 그 이전 최근 것으로 내려간다', () => {
    const { reportDate, shown, fellBack } = pick('2026-08-19', REPORTS, TODAY)
    expect(reportDate).toBe('2026-08-16')
    expect(shown).not.toBeNull()
    expect(fellBack).toBe(true)
  })

  it('그 날 보고서가 있으면 물러나지 않는다', () => {
    const { fellBack } = pick('2026-08-16', REPORTS, TODAY)
    expect(fellBack).toBe(false)
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

  it('달력 보기는 고른 하루만 보여 준다', () => {
    const shown = visible('calendar', '2026-08-21', entries)
    expect(shown.map((e) => e.id)).toEqual(['a', 'b'])
  })

  it('보고서가 전날로 물러나도 목록은 고른 날 그대로다', () => {
    // 오늘 한 일을 감추면 안 된다 — 보고서만 하루 뒤로 물러난다.
    expect(visible('calendar', '2026-08-22', entries).map((e) => e.id)).toEqual(['c'])
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

describe('보고서가 새로 쓰이면 화면이 다시 읽는다', () => {
  /**
   * My DB 화면은 들어올 때만 이력을 읽는다. 화면을 열어 둔 채 자정을 넘기면
   * 메인이 새 보고서를 쓰지만 화면은 옛 것을 그대로 보여 준다.
   * preload 가 여는 구독(onDailyReport)이 그 틈을 메운다.
   */
  function wireSubscription(bridge: { onDailyReport?: (cb: (d: string) => void) => () => void }, reload: () => void) {
    if (!bridge?.onDailyReport) return () => {}
    return bridge.onDailyReport(() => reload())
  }

  it('알림이 오면 다시 읽는다', () => {
    // 명시 타입이 없으면 TS 가 대입을 못 보고 never 로 좁힌다(호출 불가 오류).
    let fire: null | ((d: string) => void) = null as null | ((d: string) => void)
    let reloads = 0
    const bridge = { onDailyReport: (cb: (d: string) => void) => { fire = cb; return () => { fire = null } } }
    wireSubscription(bridge, () => { reloads += 1 })

    expect(reloads).toBe(0)
    fire?.('2026-08-22')
    expect(reloads).toBe(1)
  })

  it('화면을 떠나면 구독을 푼다 — 사라진 화면을 다시 읽지 않는다', () => {
    // 명시 타입이 없으면 TS 가 대입을 못 보고 never 로 좁힌다(호출 불가 오류).
    let fire: null | ((d: string) => void) = null as null | ((d: string) => void)
    let reloads = 0
    const bridge = { onDailyReport: (cb: (d: string) => void) => { fire = cb; return () => { fire = null } } }
    const unsubscribe = wireSubscription(bridge, () => { reloads += 1 })
    unsubscribe()
    fire?.('2026-08-22')
    expect(reloads).toBe(0)
  })

  it('브리지가 없어도 터지지 않는다', () => {
    // 예전 preload 로 실행되는 경우(설치본 갱신 중 등).
    expect(() => wireSubscription({}, () => {})).not.toThrow()
  })
})
