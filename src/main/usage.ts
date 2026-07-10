import { app } from 'electron'
import { join } from 'path'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'fs'
import type { UsageSummary, DailyUsage } from '../shared/usage'
import { appDataFrozen } from './appdata-guard'

const WINDOW_DAYS = 30 // 그래프에 보여줄 일수

// 토큰 사용량 영속화 — userData/usage.json 에 로컬 날짜별 누적을 저장한다.
// { "2026-07-09": 12345, ... } (날짜 키는 사전순=시간순이라 문자열 비교로 기간 합산 가능)
type Store = Record<string, number>

function usageFile(): string {
  return join(app.getPath('userData'), 'usage.json')
}

function load(): Store {
  try {
    const f = usageFile()
    if (existsSync(f)) return JSON.parse(readFileSync(f, 'utf-8')) as Store
  } catch (err) {
    console.error('[usage] 불러오기 실패:', err)
  }
  return {}
}

function save(s: Store): void {
  try {
    const f = usageFile()
    mkdirSync(join(f, '..'), { recursive: true })
    writeFileSync(f, JSON.stringify(s), 'utf-8')
  } catch (err) {
    console.error('[usage] 저장 실패:', err)
  }
}

/** 로컬 시간 기준 YYYY-MM-DD */
function localDay(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/** n일 전 로컬 날짜 — DST 안전하게 setDate로 계산 */
function dayAgo(daysAgo: number): string {
  const d = new Date()
  d.setDate(d.getDate() - daysAgo)
  return localDay(d)
}

/** 사용량 기록을 전부 지운다(공장초기화). */
export function clearUsage(): void {
  try {
    rmSync(usageFile(), { force: true })
  } catch (err) {
    console.error('[usage] 초기화 실패:', err)
  }
}

/** 한 번의 실행에서 쓴 토큰을 오늘 버킷에 더한다. */
export function recordUsage(tokens: number): void {
  if (appDataFrozen()) return // 공장초기화 직후 지연 저장 차단
  if (!Number.isFinite(tokens) || tokens <= 0) return
  const s = load()
  const day = localDay(new Date())
  s[day] = (s[day] ?? 0) + Math.round(tokens)
  save(s)
}

/** 오늘 / 최근 7일 / 최근 30일 / 전체 + 최근 30일 일별 시계열(그래프용). */
export function usageSummary(): UsageSummary {
  const s = load()
  // 전체 누적(창 밖 과거 포함)
  let total = 0
  for (const raw of Object.values(s)) {
    if (typeof raw === 'number' && raw > 0) total += raw
  }
  // 최근 WINDOW_DAYS 일별 시계열 — 과거→오늘 순, 빠진 날은 0
  const daily: DailyUsage[] = []
  for (let i = WINDOW_DAYS - 1; i >= 0; i--) {
    const day = dayAgo(i)
    const raw = s[day]
    daily.push({ day, tokens: typeof raw === 'number' && raw > 0 ? raw : 0 })
  }
  const sumLast = (n: number): number =>
    daily.slice(-n).reduce((acc, d) => acc + d.tokens, 0)
  return {
    today: daily[daily.length - 1]?.tokens ?? 0,
    week: sumLast(7),
    month: sumLast(30),
    total,
    daily
  }
}
