import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import { DEFAULT_SETTINGS } from '../../../shared/settings'
import { NVIDIA_BUILD_BASE_URL } from '../../../shared/nvidia'
import AgentView from './AgentView'


const backend: BackendInfo = { state: 'ready', port: 8123 }
const health: HealthInfo = { ollama: true, models: [] }

function capability(model: string) {
  return {
    schemaVersion: 1 as const,
    binding: { deploymentMode: 'build' as const, endpoint: NVIDIA_BUILD_BASE_URL },
    model,
    capabilities: { chat: 'supported' as const, stream: 'supported' as const, tools: 'supported' as const },
    checkedAt: '2026-08-02T00:00:00.000Z'
  }
}

function installApiStub(status: ReturnType<typeof vi.fn>): void {
  Object.defineProperty(window, 'api', {
    configurable: true,
    value: {
      conversations: {
        list: vi.fn().mockResolvedValue([]),
        get: vi.fn().mockResolvedValue(null),
        save: vi.fn().mockResolvedValue(undefined),
        setPinned: vi.fn().mockResolvedValue(undefined),
        remove: vi.fn().mockResolvedValue(undefined)
      },
      nvidia: { capabilities: { status } },
      backend: { token: vi.fn(() => 'test-token') },
      comfy: { models: { list: vi.fn().mockResolvedValue({ profiles: [] }) } },
      usage: { record: vi.fn().mockResolvedValue(undefined) }
    }
  })
}

const commonProps = {
  active: true,
  backend,
  health,
  onPickWorkspace: vi.fn().mockResolvedValue(undefined),
  onSaveSettings: vi.fn().mockResolvedValue(true),
  convCollapsed: true
}

describe('AgentView NVIDIA capability gate', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ indexed: false, count: 0, files: 0 })
    }))
  })
  afterEach(() => vi.unstubAllGlobals())

  it('keeps input disabled for unknown metadata and enables it only for cached tools=supported', async () => {
    const status = vi.fn()
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce(capability('model/unknown'))
    installApiStub(status)
    const { rerender } = render(
      <AgentView
        {...commonProps}
        settings={{
          ...DEFAULT_SETTINGS,
          activeLlmProvider: 'nvidia',
          nvidiaModel: 'model/unknown'
        }}
      />
    )
    const input = screen.getByRole('textbox')
    await waitFor(() => expect(status).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(input.hasAttribute('disabled')).toBe(true))
    expect(screen.getByText(/tools=supported/)).toBeTruthy()

    rerender(
      <AgentView
        {...commonProps}
        active={false}
        settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'nvidia', nvidiaModel: 'model/unknown' }}
      />
    )
    expect(status).toHaveBeenCalledTimes(1)
    rerender(
      <AgentView
        {...commonProps}
        active
        settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'nvidia', nvidiaModel: 'model/unknown' }}
      />
    )
    await waitFor(() => expect(status).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(input.hasAttribute('disabled')).toBe(false))
  })

  it('reconfigures preview when only the provider changes from NVIDIA back to Ollama', async () => {
    const status = vi.fn().mockResolvedValue(capability('model/a'))
    installApiStub(status)
    const fetchMock = vi.mocked(fetch)
    const workspace = 'C:/workspace'
    const { rerender } = render(
      <AgentView
        {...commonProps}
        settings={{
          ...DEFAULT_SETTINGS,
          workspace,
          activeLlmProvider: 'nvidia',
          nvidiaModel: 'model/a'
        }}
      />
    )
    await waitFor(() => expect(status).toHaveBeenCalledTimes(1))
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/preview'))).toBe(false)

    rerender(
      <AgentView
        {...commonProps}
        settings={{ ...DEFAULT_SETTINGS, workspace, activeLlmProvider: 'ollama' }}
      />
    )
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/preview'))).toBe(true)
    })
  })
})
