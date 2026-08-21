import { useCallback, useEffect, useState } from 'react'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import type { AppSettings } from '../../../shared/settings'
import type { MyDbDailyReport } from '../../../shared/mydb'
import type { UsageSummary } from '../../../shared/usage'
import { authHeaders } from '../lib/backend'
import { collectConnectionChecks, stateLabel, summarizeChecks, type Check } from '../lib/diagnostics'
import { getMyDbBridge } from '../lib/mydb'
import { DatabaseIcon, RefreshIcon, SlidersIcon, TodoIcon } from '../components/icons'

type Priority = 'high' | 'medium' | 'low'

interface TodoItem {
  id: string
  title: string
  priority: Priority
  dueDate: string | null
  dueTime?: string | null
  status: 'open' | 'done'
}

interface Props {
  active: boolean
  backend: BackendInfo
  health: HealthInfo | null
  settings: AppSettings
  onNavigate: (view: 'todo' | 'graph' | 'settings') => void
}

const PRIORITY_LABEL: Record<Priority, string> = { high: 'P1', medium: 'P2', low: 'P3' }

function localDay(offsetDays = 0): string {
  const date = new Date()
  date.setDate(date.getDate() + offsetDays)
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

/** 오늘까지 기한인 미완료 항목 = 지금 신경 써야 할 것. 기한 없는 항목은 뒤로 민다. */
function dueSoon(items: TodoItem[]): TodoItem[] {
  const today = localDay()
  return items
    .filter((item) => item.status === 'open')
    .filter((item) => !item.dueDate || item.dueDate <= today)
    .sort((a, b) => {
      if (!a.dueDate && b.dueDate) return 1
      if (a.dueDate && !b.dueDate) return -1
      if (a.dueDate && b.dueDate && a.dueDate !== b.dueDate) return a.dueDate < b.dueDate ? -1 : 1
      const order: Priority[] = ['high', 'medium', 'low']
      return order.indexOf(a.priority) - order.indexOf(b.priority)
    })
}

function overdue(item: TodoItem): boolean {
  return Boolean(item.dueDate) && item.dueDate! < localDay()
}

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`
  return String(value)
}

/** 가장 나쁜 상태가 요약 색을 정한다 — 좋은 소식으로 나쁜 소식을 가리지 않는다. */
function worstState(checks: Check[]): string {
  if (checks.some((check) => check.state === 'error')) return 'error'
  if (checks.some((check) => check.state === 'warning')) return 'warning'
  return checks.length === 0 ? 'info' : 'ok'
}

/** 이 보고서가 오늘 작성된 것인지 — 앱을 며칠 꺼 뒀다면 오래된 보고서가 최신일 수 있다. */
function reportFreshness(report: MyDbDailyReport): string {
  const generated = report.generatedAt.slice(0, 10)
  return generated === localDay(0) ? '오늘 작성' : `${generated.slice(5).replace('-', '월 ')}일 작성`
}

/** 최근 7일만 잘라 온다. summary.daily 는 최근 30일(과거→오늘)이다. */
function lastWeek(daily: UsageSummary['daily']): UsageSummary['daily'] {
  return daily.slice(-7)
}

function HomeView({ active, backend, health, settings, onNavigate }: Props): React.JSX.Element {
  const [todos, setTodos] = useState<TodoItem[]>([])
  const [usage, setUsage] = useState<UsageSummary | null>(null)
  const [report, setReport] = useState<MyDbDailyReport | null>(null)
  const [checks, setChecks] = useState<Check[]>([])
  const [loading, setLoading] = useState(false)
  const [todoError, setTodoError] = useState<string | null>(null)

  /**
   * 데이터 세 원천(할 일·사용량·보고서). 연결 점검과 분리해 둔다 —
   * 합쳐 두면 health 폴링이 5초마다 이 셋까지 다시 부른다.
   *
   * 세 원천은 서로 독립이다. 하나가 실패해도 나머지는 보여 준다 —
   * 대시보드가 통째로 비면 사용자는 무엇이 문제인지조차 알 수 없다.
   */
  const refreshData = useCallback(async () => {
    const [todoResult, usageResult, reportResult] = await Promise.allSettled([
      backend.state === 'ready' && backend.port
        ? fetch(`http://127.0.0.1:${backend.port}/creator/todos`, { headers: authHeaders() })
            .then((response) => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
            .then((data: { items?: TodoItem[] }) => data.items ?? [])
        : Promise.reject(new Error('backend-not-ready')),
      window.api?.usage?.summary() ?? Promise.reject(new Error('no-usage-bridge')),
      // getMyDbBridge()는 브리지가 없으면 **동기 throw**다. allSettled 배열 안에서 그대로
      // 부르면 세 원천이 통째로 날아간다 — 반드시 거부된 프로미스로 바꿔서 넘긴다.
      Promise.resolve().then(() => getMyDbBridge().history())
    ])

    if (todoResult.status === 'fulfilled') {
      setTodos(todoResult.value)
      setTodoError(null)
    } else {
      setTodoError(
        backend.state === 'ready' ? '할 일을 불러오지 못했습니다.' : '백엔드를 준비하는 중입니다…'
      )
    }
    if (usageResult.status === 'fulfilled') setUsage(usageResult.value)
    // dailyReports 는 report_date 내림차순이라 첫 항목이 가장 최근 보고서다.
    if (reportResult.status === 'fulfilled') setReport(reportResult.value.dailyReports[0] ?? null)
  }, [backend])

  /**
   * 연결 점검. ComfyUI·디스코드·NVIDIA 로 실제 요청이 나가므로 값이 정말
   * 바뀌었을 때만 돈다. App 이 health 참조를 안정시켜 주므로(keepIfSame),
   * 상태가 그대로면 이 콜백도 다시 만들어지지 않는다.
   */
  const refreshChecks = useCallback(async () => {
    setChecks(await collectConnectionChecks(backend, health, settings))
  }, [backend, health, settings])

  const refresh = useCallback(async () => {
    setLoading(true)
    await Promise.all([refreshData(), refreshChecks()])
    setLoading(false)
  }, [refreshData, refreshChecks])

  useEffect(() => {
    if (!active) return
    void refreshData()
  }, [active, refreshData])

  useEffect(() => {
    if (!active) return
    void refreshChecks()
  }, [active, refreshChecks])

  const pending = dueSoon(todos)
  const week = usage ? lastWeek(usage.daily) : []
  const peak = week.reduce((max, day) => Math.max(max, day.tokens), 0)

  return (
    <div className="home">
      <header className="home__head">
        <div>
          <h1 className="home__title">홈</h1>
          <p className="home__subtitle">오늘 할 일, 이번 주 사용량, 최근 My DB 변경을 한눈에 봅니다.</p>
        </div>
        <button
          type="button"
          className="home__refresh"
          onClick={() => void refresh()}
          disabled={loading}
          aria-label="새로 고침"
          title="새로 고침"
        >
          <RefreshIcon size={15} />
          <span>{loading ? '불러오는 중…' : '새로 고침'}</span>
        </button>
      </header>

      <div className="home__grid">
        {/* ── 연결 상태 ── */}
        <section className="home-card home-card--status" aria-label="연결 상태">
          <div className="home-card__head">
            <SlidersIcon size={15} />
            <h2 className="home-card__title">연결 상태</h2>
            <span className={`home-status__summary home-status__summary--${worstState(checks)}`}>
              {checks.length === 0 ? '확인 중…' : summarizeChecks(checks)}
            </span>
            <button type="button" className="home-card__link" onClick={() => onNavigate('settings')}>
              진단 센터
            </button>
          </div>
          <ul className="home-status">
            {checks.map((check) => (
              <li key={check.id} className={`home-status__row home-status__row--${check.state}`} title={check.detail}>
                <i className="home-status__dot" />
                <span className="home-status__label">{check.label}</span>
                <span className="home-status__state">{stateLabel(check.state)}</span>
              </li>
            ))}
            {checks.length === 0 && <li className="home-status__row"><span className="home-status__label">연결을 확인하는 중입니다…</span></li>}
          </ul>
        </section>

        {/* ── 할 일 ── */}
        <section className="home-card home-card--todos" aria-label="오늘 할 일">
          <div className="home-card__head">
            <TodoIcon size={15} />
            <h2 className="home-card__title">오늘 할 일</h2>
            <span className="home-card__count">{pending.length}</span>
            <button type="button" className="home-card__link" onClick={() => onNavigate('todo')}>
              캘린더 열기
            </button>
          </div>
          {todoError ? (
            <p className="home-card__empty">{todoError}</p>
          ) : pending.length === 0 ? (
            <p className="home-card__empty">기한이 지났거나 오늘까지인 할 일이 없습니다.</p>
          ) : (
            <ul className="home-todos">
              {pending.map((item) => (
                <li key={item.id} className={`home-todo${overdue(item) ? ' home-todo--overdue' : ''}`}>
                  <span className={`home-todo__pri home-todo__pri--${item.priority}`}>
                    {PRIORITY_LABEL[item.priority]}
                  </span>
                  <span className="home-todo__title">{item.title}</span>
                  <span className="home-todo__due">
                    {item.dueDate
                      ? `${overdue(item) ? '지남 · ' : ''}${item.dueDate.slice(5)}${item.dueTime ? ` ${item.dueTime}` : ''}`
                      : '기한 없음'}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* ── 토큰 사용량 ── */}
        <section className="home-card home-card--usage" aria-label="이번 주 토큰 사용량">
          <div className="home-card__head">
            <h2 className="home-card__title">이번 주 토큰</h2>
            <span className="home-card__count">{usage ? formatTokens(usage.week) : '—'}</span>
          </div>
          {week.length === 0 ? (
            <p className="home-card__empty">아직 기록된 사용량이 없습니다.</p>
          ) : (
            <>
              <ol className="home-spark" role="img" aria-label="최근 7일 일별 토큰 사용량">
                {week.map((day) => (
                  <li key={day.day} className="home-spark__col" title={`${day.day} · ${day.tokens.toLocaleString()} 토큰`}>
                    <span
                      className="home-spark__bar"
                      style={{ height: `${peak > 0 ? Math.max(2, (day.tokens / peak) * 100) : 2}%` }}
                    />
                    <span className="home-spark__label">{day.day.slice(8)}</span>
                  </li>
                ))}
              </ol>
              <dl className="home-usage-stats">
                <div><dt>오늘</dt><dd>{formatTokens(usage!.today)}</dd></div>
                <div><dt>30일</dt><dd>{formatTokens(usage!.month)}</dd></div>
                <div><dt>전체</dt><dd>{formatTokens(usage!.total)}</dd></div>
              </dl>
            </>
          )}
        </section>

        {/* ── My DB 히스토리 보고서 ── */}
        <section className="home-card home-card--reports" aria-label="My DB 일일 변경 보고서">
          <div className="home-card__head">
            <DatabaseIcon size={15} />
            <h2 className="home-card__title">My DB 일일 변경 보고서</h2>
            {report !== null && <span className="home-card__count">{reportFreshness(report)}</span>}
            <button type="button" className="home-card__link" onClick={() => onNavigate('graph')}>
              My DB 열기
            </button>
          </div>
          {report === null ? (
            <p className="home-card__empty">아직 생성된 보고서가 없습니다. 전날 변경이 있으면 하루 한 번 작성됩니다.</p>
          ) : (
            <div className="home-report">
              <div className="home-report__head">
                <span className="home-report__count">변경 {report.totalChanges}건</span>
              </div>
              <p className="home-report__body">{report.body}</p>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

export default HomeView
