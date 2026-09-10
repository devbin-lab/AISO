import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { BackendInfo } from '../../../shared/backend'
import { DEFAULT_SETTINGS } from '../../../shared/settings'
import SettingsView from './SettingsView'
import { ConfirmHost } from '../components/ConfirmDialog'
import { installSettingsApiStub } from '../test/settings-api-stub'

/**
 * 설정 탭에서 저장소 보고를 등록하는 화면의 계약.
 *
 * 자연어 등록만 있던 시절에는 사람이 경로와 브랜치를 문장 안에 손으로 적어야 했다.
 * 브랜치를 잘못 적는 실수(`main` vs `origin/main`)는 fetch 가 성공하는데 보고는 영원히
 * 비어 있는 상태로만 드러나고, 새 커밋이 없을 때 침묵하는 것이 정상 동작이라 아무도
 * 눈치채지 못한다. 그래서 이 화면의 핵심은 **고르게 만드는 것**이다.
 */

const READY_BACKEND: BackendInfo = { state: 'ready', port: 8123 }

const REFS = {
  ok: true,
  current: 'main',
  local: ['main', 'feature/login'],
  remote: ['origin/main', 'origin/feature/login'],
  recommended: 'origin/main',
  warning: ''
}

const GUILDS = [
  {
    guild_id: '777',
    guild_name: '학기작 개발',
    channels: [{ id: '123', name: 'dev-log' }],
    command_channel_id: '999'
  }
]

function stubApi(overrides: Record<string, unknown> = {}): Record<string, ReturnType<typeof vi.fn>> {
  const discord = {
    hasToken: vi.fn().mockResolvedValue(true),
    status: vi.fn().mockResolvedValue({ running: true, user: 'Aiso#1' }),
    schedules: vi.fn().mockResolvedValue({ jobs: [] }),
    scheduleRemove: vi.fn().mockResolvedValue({ ok: true }),
    channels: vi.fn().mockResolvedValue({ guilds: GUILDS }),
    pickRepo: vi.fn().mockResolvedValue('D:/My_Git/AISO'),
    repoBranches: vi.fn().mockResolvedValue(REFS),
    repoReportAdd: vi.fn().mockResolvedValue({ ok: true }),
    repoReportNow: vi.fn().mockResolvedValue({ ok: true, detail: '보고를 보냈습니다.' }),
    setLlmProvider: vi.fn(),
    saveToken: vi.fn().mockResolvedValue(undefined),
    apply: vi.fn().mockResolvedValue({ ok: true }),
    ...overrides
  }
  installSettingsApiStub({ discord })
  return discord as unknown as Record<string, ReturnType<typeof vi.fn>>
}

function openDiscordSection(): void {
  render(
    <>
      <SettingsView
        settings={{ ...DEFAULT_SETTINGS, devMode: true }}
        backend={READY_BACKEND}
        health={null}
        onSave={vi.fn().mockResolvedValue({ ok: true })}
        active
      />
      <ConfirmHost />
    </>
  )
  fireEvent.click(screen.getByRole('button', { name: '디스코드' }))
}

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  Reflect.deleteProperty(window, 'api')
})

describe('저장소 보고 등록', () => {
  it('폴더를 고르면 브랜치를 원격 추적 ref 로 미리 잡아 준다', async () => {
    // 원격이 하나뿐이면 고를 여지가 없다. 익숙한 `main` 이 아니라 `origin/main` 이
    // 잡혀 있어야 남의 커밋이 잡힌다.
    stubApi({ repoBranches: vi.fn().mockResolvedValue({ ...REFS, remote: ['origin/main'] }) })
    openDiscordSection()

    fireEvent.click(screen.getByRole('button', { name: '폴더 선택' }))

    await waitFor(() => expect(screen.getByText('D:/My_Git/AISO')).toBeTruthy())
    await waitFor(() => expect(screen.getByText('origin/main')).toBeTruthy())
  })

  it('폴더를 고르기 전에는 브랜치를 묻지 않는다 — 고를 것이 없다', () => {
    stubApi()
    openDiscordSection()
    expect(screen.queryByLabelText('보고할 브랜치')).toBeNull()
    expect(screen.getByText('선택된 저장소 없음')).toBeTruthy()
  })

  it('저장소를 고르지 않고 등록하면 그 사실을 말한다', async () => {
    const discord = stubApi()
    openDiscordSection()

    fireEvent.click(screen.getByRole('button', { name: '등록' }))

    await waitFor(() => expect(screen.getByText(/저장소 폴더를 고르세요/)).toBeTruthy())
    expect(discord.repoReportAdd).not.toHaveBeenCalled()
  })

  it('브랜치 목록을 읽지 못하면 사유를 보여 주고 등록으로 넘어가지 않는다', async () => {
    const discord = stubApi({
      repoBranches: vi.fn().mockResolvedValue({ ok: false, detail: 'git 저장소가 아닙니다' })
    })
    openDiscordSection()

    fireEvent.click(screen.getByRole('button', { name: '폴더 선택' }))

    await waitFor(() => expect(screen.getByText('git 저장소가 아닙니다')).toBeTruthy())
    expect(screen.queryByLabelText('보고할 브랜치')).toBeNull()
    expect(discord.repoReportAdd).not.toHaveBeenCalled()
  })

  it('원격을 새로 받지 못했으면 목록이 오래됐다는 사실을 밝힌다', async () => {
    stubApi({
      repoBranches: vi.fn().mockResolvedValue({ ...REFS, warning: '원격을 새로 받지 못했습니다' })
    })
    openDiscordSection()

    fireEvent.click(screen.getByRole('button', { name: '폴더 선택' }))

    await waitFor(() => expect(screen.getByText(/원격을 새로 받지 못했습니다/)).toBeTruthy())
  })

  it('고른 폴더·브랜치·채널을 그대로 등록한다', async () => {
    const discord = stubApi()
    openDiscordSection()

    fireEvent.click(screen.getByRole('button', { name: '폴더 선택' }))
    await waitFor(() => expect(screen.getByText('D:/My_Git/AISO')).toBeTruthy())

    // 기본값(모든 브랜치)에서 한 브랜치로 바꿔 고른다 — 고른 값이 그대로 가야 한다.
    fireEvent.click(screen.getByRole('button', { name: '보고할 브랜치' }))
    fireEvent.click(screen.getByRole('option', { name: 'origin/main' }))
    fireEvent.click(screen.getByRole('button', { name: '보고를 보낼 채널' }))
    fireEvent.click(screen.getByRole('option', { name: '학기작 개발 · #dev-log' }))
    fireEvent.click(screen.getByRole('button', { name: '등록' }))

    await waitFor(() =>
      expect(discord.repoReportAdd).toHaveBeenCalledWith({
        repoPath: 'D:/My_Git/AISO',
        branch: 'origin/main',
        guildId: '777',
        channelId: '123',
        intervalHours: 6,
        instruction: ''
      })
    )
  })

  it('등록에 성공하면 목록을 다시 읽어 방금 만든 예약이 보이게 한다', async () => {
    const discord = stubApi()
    openDiscordSection()
    const before = discord.schedules.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: '폴더 선택' }))
    await waitFor(() => expect(screen.getByText('D:/My_Git/AISO')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: '보고를 보낼 채널' }))
    fireEvent.click(screen.getByRole('option', { name: '학기작 개발 · #dev-log' }))
    fireEvent.click(screen.getByRole('button', { name: '등록' }))

    await waitFor(() => expect(discord.schedules.mock.calls.length).toBeGreaterThan(before))
  })

  it('사이드카가 거부하면 사유를 그대로 보여 주고 고른 값을 버리지 않는다', async () => {
    stubApi({ repoReportAdd: vi.fn().mockResolvedValue({ ok: false, detail: '예약은 최대 20개까지' }) })
    openDiscordSection()

    fireEvent.click(screen.getByRole('button', { name: '폴더 선택' }))
    await waitFor(() => expect(screen.getByText('D:/My_Git/AISO')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: '보고를 보낼 채널' }))
    fireEvent.click(screen.getByRole('option', { name: '학기작 개발 · #dev-log' }))
    fireEvent.click(screen.getByRole('button', { name: '등록' }))

    await waitFor(() => expect(screen.getByText('예약은 최대 20개까지')).toBeTruthy())
    expect(screen.getByText('D:/My_Git/AISO')).toBeTruthy()
  })

  it('원격 브랜치가 여럿이면 모든 브랜치를 기본으로 잡는다', async () => {
    // 갈라져 일하는 저장소에서 한 브랜치만 고르는 것은 거의 언제나 실수다.
    stubApi()
    openDiscordSection()

    fireEvent.click(screen.getByRole('button', { name: '폴더 선택' }))

    await waitFor(() => expect(screen.getByText('모든 브랜치')).toBeTruthy())
    expect(screen.getByText(/새 브랜치가 생겨도 따로 등록할 필요가 없고/)).toBeTruthy()
  })

  it('원격 브랜치가 하나뿐이면 그 브랜치를 기본으로 잡는다', async () => {
    stubApi({
      repoBranches: vi.fn().mockResolvedValue({ ...REFS, remote: ['origin/main'] })
    })
    openDiscordSection()

    fireEvent.click(screen.getByRole('button', { name: '폴더 선택' }))

    await waitFor(() => expect(screen.getByText('origin/main')).toBeTruthy())
  })

  it('모든 브랜치를 고르면 사이드카에는 별표로 보낸다', async () => {
    const discord = stubApi()
    openDiscordSection()

    fireEvent.click(screen.getByRole('button', { name: '폴더 선택' }))
    await waitFor(() => expect(screen.getByText('모든 브랜치')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: '보고를 보낼 채널' }))
    fireEvent.click(screen.getByRole('option', { name: '학기작 개발 · #dev-log' }))
    fireEvent.click(screen.getByRole('button', { name: '등록' }))

    await waitFor(() =>
      expect(discord.repoReportAdd).toHaveBeenCalledWith(
        expect.objectContaining({ branch: '*' })
      )
    )
  })

  it('봇이 꺼져 있어 채널 목록이 비면 채널을 고를 수 없다고 말한다', () => {
    stubApi({ channels: vi.fn().mockResolvedValue({ guilds: [] }) })
    openDiscordSection()
    expect(screen.getByText('봇을 먼저 연결하세요')).toBeTruthy()
  })
})

describe('지금 보고', () => {
  const JOB = {
    id: 'job-1',
    kind: 'repo_report',
    channel_name: 'dev-log',
    text: '',
    repeat: 'interval',
    interval_hours: 6,
    repo_path: 'D:/GitHub/CK_SemesterProject',
    branch: '*',
    branch_cursors: { 'origin/main': 'aaaa1111' },
    next_run: '2026-09-10T18:00'
  }

  it('저장소 보고에만 두 버튼이 붙는다', async () => {
    stubApi({
      schedules: vi.fn().mockResolvedValue({
        jobs: [JOB, { ...JOB, id: 'job-2', kind: 'message', text: '알림' }]
      })
    })
    openDiscordSection()

    await waitFor(() => expect(screen.getAllByRole('button', { name: '새 커밋 보고' })).toHaveLength(1))
    expect(screen.getAllByRole('button', { name: '테스트 보고' })).toHaveLength(1)
  })

  it("'새 커밋 보고'는 새 것만 묻는다 — preview 없이 부른다", async () => {
    const discord = stubApi({ schedules: vi.fn().mockResolvedValue({ jobs: [JOB] }) })
    openDiscordSection()

    fireEvent.click(await screen.findByRole('button', { name: '새 커밋 보고' }))

    await waitFor(() => expect(discord.repoReportNow).toHaveBeenCalledWith('job-1', false))
    await waitFor(() => expect(screen.getByText('보고를 보냈습니다.')).toBeTruthy())
  })

  it("'테스트 보고'는 새 커밋 여부와 상관없이 시험 보고서를 요청한다", async () => {
    const discord = stubApi({ schedules: vi.fn().mockResolvedValue({ jobs: [JOB] }) })
    openDiscordSection()

    fireEvent.click(await screen.findByRole('button', { name: '테스트 보고' }))

    await waitFor(() => expect(discord.repoReportNow).toHaveBeenCalledWith('job-1', true))
  })

  it('보낼 것이 없었다는 결과도 보여 준다 — 침묵은 고장과 구별되지 않는다', async () => {
    stubApi({
      schedules: vi.fn().mockResolvedValue({ jobs: [JOB] }),
      repoReportNow: vi
        .fn()
        .mockResolvedValue({ ok: true, detail: 'CK_SemesterProject 에 마지막 보고 이후 새 커밋이 없습니다.' })
    })
    openDiscordSection()

    fireEvent.click(await screen.findByRole('button', { name: '새 커밋 보고' }))

    await waitFor(() => expect(screen.getByText(/새 커밋이 없습니다/)).toBeTruthy())
  })

  it('보고가 끝나면 목록을 다시 읽어 마지막 보고 시각을 반영한다', async () => {
    const discord = stubApi({ schedules: vi.fn().mockResolvedValue({ jobs: [JOB] }) })
    openDiscordSection()
    const before = discord.schedules.mock.calls.length

    fireEvent.click(await screen.findByRole('button', { name: '새 커밋 보고' }))

    await waitFor(() => expect(discord.schedules.mock.calls.length).toBeGreaterThan(before))
  })
})

describe('매일 정해진 시각', () => {
  async function fillRepoAndChannel(): Promise<void> {
    fireEvent.click(screen.getByRole('button', { name: '폴더 선택' }))
    await waitFor(() => expect(screen.getByText('D:/My_Git/AISO')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: '보고를 보낼 채널' }))
    fireEvent.click(screen.getByRole('option', { name: '학기작 개발 · #dev-log' }))
  }

  it('기본은 주기마다이고, 시각은 토글해야 나온다', () => {
    stubApi()
    openDiscordSection()
    expect(screen.getByLabelText('보고 주기(시간)')).toBeTruthy()
    expect(screen.queryByLabelText('매일 보고할 시각')).toBeNull()
  })

  it('시각을 고르면 주기 대신 시각을 보낸다', async () => {
    const discord = stubApi()
    openDiscordSection()
    await fillRepoAndChannel()

    fireEvent.click(screen.getByRole('button', { name: '매일 정해진 시각' }))
    fireEvent.change(screen.getByLabelText('매일 보고할 시각'), { target: { value: '07:30' } })
    fireEvent.click(screen.getByRole('button', { name: '등록' }))

    await waitFor(() =>
      expect(discord.repoReportAdd).toHaveBeenCalledWith(
        expect.objectContaining({ dailyAt: '07:30' })
      )
    )
    expect(discord.repoReportAdd.mock.calls[0]![0]).not.toHaveProperty('intervalHours')
  })

  it('시각이 HH:MM 이 아니면 보내기 전에 막는다', async () => {
    const discord = stubApi()
    openDiscordSection()
    await fillRepoAndChannel()

    fireEvent.click(screen.getByRole('button', { name: '매일 정해진 시각' }))
    fireEvent.change(screen.getByLabelText('매일 보고할 시각'), { target: { value: '9시' } })
    fireEvent.click(screen.getByRole('button', { name: '등록' }))

    await waitFor(() => expect(screen.getByText(/HH:MM/)).toBeTruthy())
    expect(discord.repoReportAdd).not.toHaveBeenCalled()
  })

  it('목록은 매일 예약의 시각을 보여 준다', async () => {
    stubApi({
      schedules: vi.fn().mockResolvedValue({
        jobs: [{
          id: 'job-d', kind: 'repo_report', channel_name: 'dev-log', text: '',
          repeat: 'daily', daily_at: '09:00', repo_path: 'D:/GitHub/CK_SemesterProject',
          branch: '*', branch_cursors: {}, next_run: '2026-09-12T09:00'
        }]
      })
    })
    openDiscordSection()

    await waitFor(() => expect(screen.getByText(/매일 09:00/)).toBeTruthy())
  })
})

describe('예약 목록 자동 갱신', () => {
  it('디스코드 섹션이 보이는 동안 러너의 틱에 맞춰 목록을 다시 읽는다', async () => {
    vi.useFakeTimers()
    try {
      const discord = stubApi()
      openDiscordSection()
      await vi.advanceTimersByTimeAsync(0)
      const before = discord.schedules.mock.calls.length

      await vi.advanceTimersByTimeAsync(30_000)

      expect(discord.schedules.mock.calls.length).toBeGreaterThan(before)
    } finally {
      vi.useRealTimers()
    }
  })

  it('창으로 돌아오면 바로 한 번 읽는다 — 자리를 비운 사이 지난 시각이 남아 있지 않게', async () => {
    const discord = stubApi()
    openDiscordSection()
    await waitFor(() => expect(discord.schedules).toHaveBeenCalled())
    const before = discord.schedules.mock.calls.length

    window.dispatchEvent(new Event('focus'))

    await waitFor(() => expect(discord.schedules.mock.calls.length).toBeGreaterThan(before))
  })
})
