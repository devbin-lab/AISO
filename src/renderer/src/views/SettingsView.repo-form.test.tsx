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
    stubApi()
    openDiscordSection()

    fireEvent.click(screen.getByRole('button', { name: '폴더 선택' }))

    await waitFor(() => expect(screen.getByText('D:/My_Git/AISO')).toBeTruthy())
    // 익숙한 `main` 이 아니라 `origin/main` 이 잡혀 있어야 남의 커밋이 잡힌다.
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

  it('봇이 꺼져 있어 채널 목록이 비면 채널을 고를 수 없다고 말한다', () => {
    stubApi({ channels: vi.fn().mockResolvedValue({ guilds: [] }) })
    openDiscordSection()
    expect(screen.getByText('봇을 먼저 연결하세요')).toBeTruthy()
  })
})
