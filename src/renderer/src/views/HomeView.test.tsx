import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { BackendInfo } from '../../../shared/backend'
import { DEFAULT_SETTINGS } from '../../../shared/settings'
import HomeView from './HomeView'

const READY: BackendInfo = { state: 'ready', port: 51234 }
const HEALTH = null
const HEALTHY = { ollama: true, models: ['gemma4:12b'], detail: '' }
const SETTINGS = DEFAULT_SETTINGS

function day(offset: number): string {
  const date = new Date()
  date.setDate(date.getDate() + offset)
  const month = String(date.getMonth() + 1).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${String(date.getDate()).padStart(2, '0')}`
}

interface Todo {
  id: string
  title: string
  priority: 'high' | 'medium' | 'low'
  dueDate: string | null
  status: 'open' | 'done'
}

function todo(over: Partial<Todo> & { id: string; title: string }): Todo {
  return { priority: 'medium', dueDate: day(0), status: 'open', ...over }
}

const DAILY = Array.from({ length: 30 }, (_, i) => ({ day: day(i - 29), tokens: (i + 1) * 100 }))

function install(options: {
  todos?: Todo[]
  todoStatus?: number
  usage?: unknown
  reports?: unknown[]
  historyRejects?: boolean
  discordRunning?: boolean
} = {}): { fetchMock: ReturnType<typeof vi.fn>; todoCalls: () => number } {
  const todoResponse = {
    ok: options.todoStatus === undefined || options.todoStatus < 400,
    status: options.todoStatus ?? 200,
    json: async () => ({ items: options.todos ?? [] })
  }
  // fetch 는 할 일 조회와 연결 점검(ComfyUI)이 함께 쓴다. 테스트가 세려는 것은
  // 할 일 호출이므로 URL 로 갈라 센다 — 원천이 늘어도 이 단언이 깨지지 않는다.
  // /comfy/health 는 ComfyUI 가 죽어 있어도 200 을 돌려주므로, 목이 빈 본문을 주면
  // '연결 안 됨'이 된다. 이 테스트들이 보려는 건 ComfyUI 가 아니라 다른 항목이므로
  // 살아 있는 응답({ online: true })을 준다. 예전에는 판정이 response.ok 만 봐서
  // 빈 본문도 '정상'으로 통과했다 — 목이 그 버그에 기대고 있었다.
  const fetchMock = vi.fn().mockImplementation((url: unknown) =>
    String(url).includes('/creator/todos')
      ? Promise.resolve(todoResponse)
      : Promise.resolve({ ok: true, status: 200, json: async () => ({ online: true }) })
  )
  const todoCalls = (): number =>
    fetchMock.mock.calls.filter((call) => String(call[0]).includes('/creator/todos')).length
  vi.stubGlobal('fetch', fetchMock)
  Object.defineProperty(window, 'api', {
    configurable: true,
    value: {
      usage: {
        summary: vi.fn().mockResolvedValue(
          options.usage ?? { today: 3000, week: 21_000, month: 46_500, total: 1_250_000, daily: DAILY }
        )
      },
      discord: { status: vi.fn().mockResolvedValue({ running: options.discordRunning ?? false }) },
      myDb: {
        history: options.historyRejects
          ? vi.fn().mockRejectedValue(new Error('boom'))
          : vi.fn().mockResolvedValue({ entries: [], dailyReports: options.reports ?? [] })
      }
    }
  })
  return { fetchMock, todoCalls }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('HomeView 대시보드', () => {
  it('세 패널을 모두 그린다', async () => {
    install({
      todos: [todo({ id: 't1', title: '릴리스 노트 정리' })],
      reports: [{ reportDate: day(-1), generatedAt: '', totalChanges: 4, body: '문서 4건 수정' }]
    })
    render(<HomeView active backend={READY} health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />)

    await screen.findByText('릴리스 노트 정리')
    expect(screen.getByLabelText('이번 주 토큰 사용량')).toBeTruthy()
    expect(screen.getByText('문서 4건 수정')).toBeTruthy()
  })

  it('한 원천이 실패해도 나머지는 계속 보인다', async () => {
    // 대시보드가 통째로 비면 사용자는 무엇이 고장났는지조차 알 수 없다.
    install({ todoStatus: 500, historyRejects: true })
    render(<HomeView active backend={READY} health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />)

    await screen.findByText('할 일을 불러오지 못했습니다.')
    // 사용량은 정상 응답이므로 살아 있어야 한다.
    await waitFor(() => expect(screen.getByText('21.0K')).toBeTruthy())
  })

  it('사이드카가 준비되기 전에는 요청 자체를 보내지 않는다', async () => {
    // 포트를 모르는 상태에서 fetch하면 엉뚱한 주소로 나간다.
    const { fetchMock } = install()
    render(
      <HomeView active backend={{ state: 'starting', port: null }} health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />
    )
    await screen.findByText('백엔드를 준비하는 중입니다…')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('완료된 할 일과 아직 먼 기한은 오늘 목록에서 빠진다', async () => {
    install({
      todos: [
        todo({ id: 'done', title: '이미 끝낸 일', status: 'done' }),
        todo({ id: 'far', title: '다음 주 일', dueDate: day(7) }),
        todo({ id: 'now', title: '오늘 마감 항목', dueDate: day(0) })
      ]
    })
    render(<HomeView active backend={READY} health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />)

    await screen.findByText('오늘 할 일')
    const list = screen.getByLabelText('오늘 할 일')
    expect(within(list).queryByText('이미 끝낸 일')).toBeNull()
    expect(within(list).queryByText('다음 주 일')).toBeNull()
    expect(within(list).getByText('오늘 마감 항목')).toBeTruthy()
  })

  it('기한이 지난 항목을 지난 순서대로 먼저 보여 준다', async () => {
    install({
      todos: [
        todo({ id: 'today', title: '오늘치' }),
        todo({ id: 'old', title: '어제치', dueDate: day(-1) }),
        todo({ id: 'none', title: '기한 없음', dueDate: null })
      ]
    })
    render(<HomeView active backend={READY} health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />)

    await screen.findByText('어제치')
    const titles = Array.from(
      screen.getByLabelText('오늘 할 일').querySelectorAll('.home-todo__title')
    ).map((node) => node.textContent)
    // 기한 없는 항목은 마감이 있는 항목보다 뒤여야 한다.
    expect(titles).toEqual(['어제치', '오늘치', '기한 없음'])
  })

  it('스파크라인은 30일 기록에서 마지막 7일만 쓴다', async () => {
    install()
    const { container } = render(<HomeView active backend={READY} health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />)
    await waitFor(() => expect(container.querySelectorAll('.home-spark__col').length).toBe(7))
  })

  it('사용량이 0뿐이어도 높이 계산이 NaN으로 깨지지 않는다', async () => {
    install({
      usage: {
        today: 0,
        week: 0,
        month: 0,
        total: 0,
        daily: Array.from({ length: 7 }, (_, i) => ({ day: day(i - 6), tokens: 0 }))
      }
    })
    const { container } = render(<HomeView active backend={READY} health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />)
    await waitFor(() => expect(container.querySelectorAll('.home-spark__bar').length).toBe(7))
    for (const bar of container.querySelectorAll<HTMLElement>('.home-spark__bar')) {
      expect(bar.style.height).toBe('2%')
    }
  })

  it('비활성 상태에서는 데이터를 부르지 않는다', async () => {
    // 홈은 숨김 유지 방식이라 다른 탭에 있는 동안 폴링하면 안 된다.
    const { fetchMock } = install()
    render(<HomeView active={false} backend={READY} health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />)
    await Promise.resolve()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(window.api.usage.summary).not.toHaveBeenCalled()
  })

  it('My DB 브리지가 아예 없어도 나머지 패널은 산다', async () => {
    // getMyDbBridge()는 브리지가 없으면 동기 throw다. allSettled 배열 안에서
    // 그대로 부르면 세 원천이 통째로 날아가 대시보드가 빈 화면이 된다.
    const { todoCalls } = install({ todos: [todo({ id: 't1', title: '살아남아야 할 항목' })] })
    const api = window.api as unknown as Record<string, unknown>
    delete api.myDb

    render(<HomeView active backend={READY} health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />)
    await screen.findByText('살아남아야 할 항목')
    await waitFor(() => expect(screen.getByText('21.0K')).toBeTruthy())
    expect(todoCalls()).toBe(1)
  })

  it('바로가기 버튼이 해당 화면으로 넘긴다', async () => {
    install()
    const onNavigate = vi.fn()
    render(<HomeView active backend={READY} health={HEALTH} settings={SETTINGS} onNavigate={onNavigate} />)

    await userEvent.click(await screen.findByText('캘린더 열기'))
    expect(onNavigate).toHaveBeenCalledWith('todo')
    await userEvent.click(screen.getByText('My DB 열기'))
    expect(onNavigate).toHaveBeenCalledWith('graph')
  })

  it('새로 고침이 세 원천을 다시 읽는다', async () => {
    const { todoCalls } = install()
    render(<HomeView active backend={READY} health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />)
    await waitFor(() => expect(todoCalls()).toBe(1))

    await userEvent.click(screen.getByRole('button', { name: '새로 고침' }))
    await waitFor(() => expect(todoCalls()).toBe(2))
    expect(window.api.usage.summary).toHaveBeenCalledTimes(2)
  })

  it('보고서는 가장 최근 1건만 싣는다', async () => {
    // 홈은 한 화면에 훑어보는 곳이라 과거 보고서를 쌓지 않는다. 과거는 My DB 에서 본다.
    install({
      reports: Array.from({ length: 6 }, (_, i) => ({
        reportDate: day(-(i + 1)),
        generatedAt: '',
        totalChanges: i,
        body: `보고서 ${i}`
      }))
    })
    const { container } = render(<HomeView active backend={READY} health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />)
    await waitFor(() => expect(container.querySelectorAll('.home-report').length).toBe(1))
    expect(screen.getByText('보고서 0')).toBeTruthy()
    expect(screen.queryByText('보고서 1')).toBeNull()
  })

  // ── 연결 상태 ──────────────────────────────────────────────────────────

  it('연결 상태를 오늘 할 일 위에 보여 준다', async () => {
    install()
    const { container } = render(
      <HomeView active backend={READY} health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />
    )
    await screen.findByText('Aiso 백엔드')
    const status = container.querySelector('.home-card--status')!
    const todos = container.querySelector('.home-card--todos')!
    // DOM 순서로 위/아래를 고정한다 — jsdom 은 레이아웃을 계산하지 않는다.
    expect(status.compareDocumentPosition(todos)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
  })

  it('연결되지 않은 항목은 오류가 아니라 미연결로 표시한다', async () => {
    // 디스코드를 쓰지 않는 사용자에게 '확인 필요'를 띄우면 쓰지도 않는 기능으로 잔소리가 된다.
    install({ discordRunning: false })
    const { container } = render(
      <HomeView active backend={READY} health={HEALTHY} settings={SETTINGS} onNavigate={vi.fn()} />
    )
    const row = await screen.findByTitle('연결하지 않았거나 현재 중지되어 있습니다.')
    expect(row.textContent).toContain('미연결')
    expect(row.className).toContain('home-status__row--info')
    // 핵심: 미연결은 요약에서 경고로 세지 않는다. 안 쓰는 기능이 계속 경고를 띄우면
    // 요약이 무의미해지고, 진짜 경고가 묻힌다.
    expect(container.querySelector('.home-status__summary')?.textContent).toBe('핵심 연결 정상')
  })

  it('백엔드가 죽으면 요약이 오류로 바뀐다', async () => {
    install()
    const { container } = render(
      <HomeView active backend={{ state: 'error', port: null, detail: '기동 실패' }}
        health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />
    )
    await screen.findByText('Aiso 백엔드')
    const summary = container.querySelector('.home-status__summary')!
    expect(summary.textContent).toBe('오류 1')
    expect(summary.className).toContain('home-status__summary--error')
  })

  it('연결 점검이 실패해도 나머지 패널은 산다', async () => {
    install()
    const api = window.api as unknown as Record<string, unknown>
    delete api.discord
    render(<HomeView active backend={READY} health={HEALTH} settings={SETTINGS} onNavigate={vi.fn()} />)
    // 사용량은 그대로 보인다.
    await waitFor(() => expect(screen.getByText('21.0K')).toBeTruthy())
  })
})
