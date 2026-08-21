import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import HomeView from './HomeView'
import { DEFAULT_SETTINGS } from '../../../shared/settings'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'

/**
 * 홈 대시보드가 필요 이상으로 다시 로드되지 않는다.
 *
 * App 은 Ollama health 를 5초마다 폴링한다. 예전에는 refresh 하나가 데이터 세
 * 원천과 연결 점검을 함께 들고 있으면서 의존성에 health 객체가 그대로 들어가,
 * 값이 하나도 안 바뀌어도 5초마다 대시보드 전체가 다시 로드됐다
 * (실측: 60초에 12회, 내용 변화 0회). ComfyUI·디스코드·NVIDIA 로 실제 요청이
 * 나가는 점검이라 낭비가 컸다.
 *
 * 지금은 두 가지가 함께 막는다.
 *  1) App 이 health 내용이 같으면 이전 객체를 유지한다(keepIfSame) — 참조 안정.
 *  2) HomeView 가 데이터 로드와 연결 점검을 분리한다 — health 가 바뀌어도
 *     할 일·사용량·보고서까지 다시 부르지 않는다.
 */

const backend: BackendInfo = { state: 'ready', port: 12345 }

function makeApi(): { usage: { summary: ReturnType<typeof vi.fn> }; discord: { status: ReturnType<typeof vi.fn> }; myDb: { history: ReturnType<typeof vi.fn> } } {
  return {
    usage: { summary: vi.fn().mockResolvedValue({ today: 0, week: 0, month: 0, total: 0, daily: [] }) },
    discord: { status: vi.fn().mockResolvedValue({ running: false }) },
    myDb: { history: vi.fn().mockResolvedValue({ entries: [], dailyReports: [] }) }
  }
}

let api: ReturnType<typeof makeApi>
let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  api = makeApi()
  ;(window as unknown as { api: unknown }).api = api
  fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ items: [] }) }))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const props = {
  backend,
  settings: { ...DEFAULT_SETTINGS, activeLlmProvider: 'ollama' as const, comfyBaseUrl: '' },
  onNavigate: vi.fn()
}

/** health 폴링이 만들어 내는 값 — 내용은 같지만 매번 새 객체다. */
function healthTick(): HealthInfo {
  return { ollama: true, models: ['gemma4:12b', 'bge-m3'], detail: undefined }
}

describe('홈 대시보드 새로고침', () => {
  it('health 내용이 같으면 데이터를 다시 부르지 않는다', async () => {
    // App 의 keepIfSame 이 참조를 유지하므로, 같은 객체를 계속 넘기는 것과 같다.
    const stable = healthTick()
    const { rerender } = render(<HomeView active {...props} health={stable} />)
    await waitFor(() => expect(api.myDb.history).toHaveBeenCalledTimes(1))

    for (let i = 0; i < 11; i++) rerender(<HomeView active {...props} health={stable} />)
    await new Promise((r) => setTimeout(r, 30))

    // 12번 폴링해도 최초 1회뿐 — 예전에는 12회였다.
    expect(api.myDb.history).toHaveBeenCalledTimes(1)
    expect(api.usage.summary).toHaveBeenCalledTimes(1)
    expect(api.discord.status).toHaveBeenCalledTimes(1)
  })

  it('health 가 실제로 바뀌면 연결 상태는 다시 확인한다', async () => {
    const { rerender } = render(<HomeView active {...props} health={healthTick()} />)
    await waitFor(() => expect(api.discord.status).toHaveBeenCalledTimes(1))

    // Ollama 가 끊겼다 — 이건 반드시 반영되어야 한다.
    rerender(<HomeView active {...props} health={{ ollama: false, models: [], detail: '연결 실패' }} />)
    await waitFor(() => expect(api.discord.status).toHaveBeenCalledTimes(2))
  })

  it('health 가 바뀌어도 할 일·사용량·보고서는 다시 부르지 않는다', async () => {
    // 이 셋은 health 와 무관하다. 합쳐 두면 폴링마다 같이 끌려 나온다.
    const { rerender } = render(<HomeView active {...props} health={healthTick()} />)
    await waitFor(() => expect(api.myDb.history).toHaveBeenCalledTimes(1))

    rerender(<HomeView active {...props} health={{ ollama: false, models: [], detail: '연결 실패' }} />)
    await waitFor(() => expect(api.discord.status).toHaveBeenCalledTimes(2))

    expect(api.myDb.history).toHaveBeenCalledTimes(1)
    expect(api.usage.summary).toHaveBeenCalledTimes(1)
  })
})
