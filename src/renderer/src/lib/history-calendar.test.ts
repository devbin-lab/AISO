import { describe, expect, it } from 'vitest'
import { buildMonth, countByDay, intensityOf, localDayKey, monthRange, monthsWithHistory, shiftMonth } from './history-calendar'
import type { MyDbHistoryEntry } from '../../../shared/mydb'

function entry(createdAt: string, id = createdAt): MyDbHistoryEntry {
  return { id, action: 'imported', subjectTitle: '자료', createdAt }
}

describe('히스토리 달력', () => {
  it('날짜를 로컬 기준으로 센다', () => {
    // ISO 문자열을 그대로 자르면 UTC 기준이라 자정 근처가 하루 밀린다.
    // 한국(UTC+9)에서 이 시각은 8월 22일 오전 1시다.
    const key = localDayKey('2026-08-21T16:30:00.000Z')
    const local = new Date('2026-08-21T16:30:00.000Z')
    const expected = `${local.getFullYear()}-${String(local.getMonth() + 1).padStart(2, '0')}-${String(local.getDate()).padStart(2, '0')}`
    expect(key).toBe(expected)
  })

  it('파싱할 수 없는 값은 버리고 나머지를 센다', () => {
    const counts = countByDay([entry('2026-08-16T01:00:00.000Z'), entry('말이 안 되는 값', 'bad')])
    expect([...counts.values()].reduce((a, b) => a + b, 0)).toBe(1)
  })

  it('같은 날 여러 건을 합산한다', () => {
    const day = '2026-08-16T01:00:00.000Z'
    const counts = countByDay([entry(day, 'a'), entry(day, 'b'), entry(day, 'c')])
    expect([...counts.values()][0]).toBe(3)
  })

  it('이력이 있는 달만 최신순으로 준다', () => {
    const months = monthsWithHistory([
      entry('2026-06-02T03:00:00.000Z', 'a'),
      entry('2026-08-16T03:00:00.000Z', 'b'),
      entry('2026-07-01T03:00:00.000Z', 'c')
    ])
    expect(months).toEqual(['2026-08', '2026-07', '2026-06'])
  })

  it('이력이 없으면 달도 없다', () => {
    expect(monthsWithHistory([])).toEqual([])
  })

  it('격자가 항상 7의 배수이고 날짜 수가 맞는다', () => {
    const month = buildMonth('2026-08', new Map(), new Date(2026, 7, 22))
    expect(month.days.length % 7).toBe(0)
    expect(month.days.filter((d) => !d.filler).length).toBe(31)
    expect(month.label).toBe('2026년 8월')
  })

  it('윤년 2월도 정확하다', () => {
    // 2028 은 윤년 — 29일이어야 한다.
    expect(buildMonth('2028-02', new Map()).days.filter((d) => !d.filler).length).toBe(29)
    expect(buildMonth('2026-02', new Map()).days.filter((d) => !d.filler).length).toBe(28)
  })

  it('첫날이 요일 자리에 맞게 놓인다', () => {
    const month = buildMonth('2026-08', new Map())
    const lead = new Date(2026, 7, 1).getDay()
    expect(month.days.slice(0, lead).every((d) => d.filler)).toBe(true)
    expect(month.days[lead]?.day).toBe(1)
  })

  it('오늘을 표시하되 다른 날은 표시하지 않는다', () => {
    const month = buildMonth('2026-08', new Map(), new Date(2026, 7, 22))
    const marked = month.days.filter((d) => d.isToday)
    expect(marked).toHaveLength(1)
    expect(marked[0]?.day).toBe(22)
  })

  it('다른 달을 보면 오늘 표시가 없다', () => {
    const month = buildMonth('2026-06', new Map(), new Date(2026, 7, 22))
    expect(month.days.some((d) => d.isToday)).toBe(false)
  })

  it('그 달의 합계를 낸다', () => {
    const counts = new Map([['2026-08-16', 5], ['2026-08-20', 2], ['2026-07-01', 9]])
    expect(buildMonth('2026-08', counts).total).toBe(7)
  })

  it('농도는 그 달의 최대치 기준이다', () => {
    // 절대 기준이면 활동이 적은 사용자는 달력이 늘 비어 보인다.
    expect(intensityOf(0, 10)).toBe(0)
    expect(intensityOf(10, 10)).toBe(4)
    expect(intensityOf(1, 40)).toBe(1)
    expect(intensityOf(1, 1)).toBe(4)
    expect(intensityOf(3, 4)).toBe(3)
  })
})

describe('달력 월 이동', () => {
  it('앞뒤로 한 달씩 옮긴다', () => {
    expect(shiftMonth('2026-08', -1)).toBe('2026-07')
    expect(shiftMonth('2026-08', 1)).toBe('2026-09')
  })

  it('연도 경계를 넘는다', () => {
    expect(shiftMonth('2026-01', -1)).toBe('2025-12')
    expect(shiftMonth('2026-12', 1)).toBe('2027-01')
  })

  it('이력이 한 달뿐이어도 앞뒤로 넘길 수 있다', () => {
    // 예전에는 '이력이 있는 달' 사이로만 건너뛰게 해서, 이력이 한 달뿐이면
    // 양쪽 버튼이 동시에 비활성돼 달력이 멈춘 것처럼 보였다.
    const entries = [
      { id: 'a', action: 'imported' as const, subjectTitle: 'x', createdAt: '2026-08-16T01:00:00.000Z' }
    ]
    const range = monthRange(entries, new Date(2026, 7, 22))
    // 범위가 한 점이면 양쪽 버튼이 동시에 잠긴다. 최소 12개월 창을 보장한다.
    expect(range.max).toBe('2026-08')
    expect(range.min).toBe('2025-09')
    expect(range.min < range.max).toBe(true)
  })

  it('가장 오래된 이력 달부터 이번 달까지 묶는다', () => {
    const entries = [
      { id: 'a', action: 'imported' as const, subjectTitle: 'x', createdAt: '2026-05-02T03:00:00.000Z' },
      { id: 'b', action: 'imported' as const, subjectTitle: 'y', createdAt: '2026-07-02T03:00:00.000Z' }
    ]
    const range = monthRange(entries, new Date(2026, 7, 22))
    // 이력은 5월부터지만 최소 창(12개월)이 더 넓으면 그쪽을 쓴다.
    expect(range.min).toBe('2025-09')
    expect(range.max).toBe('2026-08')
  })

  it('이력이 없어도 넘겨 볼 수 있다', () => {
    const range = monthRange([], new Date(2026, 7, 22))
    // 이력이 없어도 달력은 넘길 수 있어야 한다.
    expect(range.max).toBe('2026-08')
    expect(range.min).toBe('2025-09')
  })

  it('미래에 기록된 이력도 볼 수 있다', () => {
    // 시계를 바꿨거나 다른 기기에서 이관한 경우 미래 날짜가 있을 수 있다.
    const entries = [
      { id: 'a', action: 'imported' as const, subjectTitle: 'x', createdAt: '2026-12-02T03:00:00.000Z' }
    ]
    const range = monthRange(entries, new Date(2026, 7, 22))
    expect(range.max).toBe('2026-12')
  })
})
