import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import { DEFAULT_SETTINGS } from '../../../shared/settings'
import { NVIDIA_BUILD_BASE_URL } from '../../../shared/nvidia'
import AgentView from './AgentView'


const backend: BackendInfo = { state: 'ready', port: 8123 }
const health: HealthInfo = { ollama: true, models: [] }
let manifestWorkspacePath = 'C:/workspace'

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
  const describeManifest = vi.fn().mockImplementation(async ({ sessionId, scope }) => ({
    schemaVersion: 1,
    manifestId: 'manifest-gate6-1234567890abcdef',
    sessionId,
    model: 'model/a',
    deploymentMode: 'build',
    expiresInSeconds: 600,
    sends: {
      conversation: true,
      workspace: scope.workspace,
      rag: scope.rag,
      imagePrompt: scope.image,
      toolResults: ['update_plan', 'get_system_time'],
      toolResultDetails: ['plan', 'time']
    },
    scopeDetails: {
      workspacePath: scope.workspace ? manifestWorkspacePath : null,
      rag: { enabled: scope.rag, localOllama: true, topK: scope.rag ? 5 : 0 },
      image: { enabled: scope.image, selectionMode: 'auto' }
    },
    localOnly: ['workspace', 'RAG', 'ComfyUI'],
    allowedTools: ['update_plan', 'get_system_time']
  }))
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
      nvidia: {
        capabilities: { status },
        agent: {
          describeManifest,
          decideManifest: vi.fn().mockResolvedValue({ approved: true }),
          prepare: vi.fn().mockResolvedValue({
            grantId: 'agent-grant-gate6',
            assistantTurnId: 'assistant-turn-gate6',
            expiresInSeconds: 60
          }),
          finish: vi.fn().mockResolvedValue(undefined)
        }
      },
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
    manifestWorkspacePath = 'C:/workspace'
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

  it('keeps input and conversation untouched when the Main grant expires before execution', async () => {
    const status = vi.fn().mockResolvedValue(capability('model/a'))
    installApiStub(status)
    vi.mocked(window.api.nvidia.agent.prepare).mockRejectedValueOnce(
      new Error('NVIDIA Agent approval expired')
    )
    const fetchMock = vi.mocked(fetch)
    render(
      <AgentView
        {...commonProps}
        settings={{
          ...DEFAULT_SETTINGS,
          activeLlmProvider: 'nvidia',
          nvidiaModel: 'model/a'
        }}
      />
    )

    const approve = await screen.findByRole('button', { name: '이 범위 승인' })
    await waitFor(() => expect(approve.hasAttribute('disabled')).toBe(false))
    fireEvent.click(approve)
    await waitFor(() => expect(screen.getByRole('button', { name: '승인됨' })).toBeTruthy())

    const input = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: 'do not mutate this request' } })
    fireEvent.click(screen.getByRole('button', { name: '실행' }))
    await waitFor(() => expect(window.api.nvidia.agent.prepare).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(input.value).toBe('do not mutate this request'))

    expect(window.api.conversations.save).not.toHaveBeenCalled()
    expect(screen.queryByText('do not mutate this request', { selector: '.msg' })).toBeNull()
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/agent'))).toBe(false)
  })

  it('shows the manifest RAG topK only after the RAG scope is enabled', async () => {
    const status = vi.fn().mockResolvedValue(capability('model/a'))
    installApiStub(status)
    render(
      <AgentView
        {...commonProps}
        settings={{
          ...DEFAULT_SETTINGS,
          workspace: 'C:/workspace',
          activeLlmProvider: 'nvidia',
          nvidiaModel: 'model/a'
        }}
      />
    )

    const workspaceScope = await screen.findByLabelText(/작업 폴더 읽기/)
    expect(screen.queryByText(/상위 0개/)).toBeNull()
    fireEvent.click(workspaceScope)
    const ragScope = screen.getByLabelText(/로컬 RAG 검색/)
    await waitFor(() => expect((ragScope as HTMLInputElement).disabled).toBe(false))
    fireEvent.click(ragScope)
    await waitFor(() => expect(screen.getByText(/로컬 RAG 검색 .*상위 5개/)).toBeTruthy())
  })

  it('invalidates and re-describes an approved manifest when fingerprint settings change', async () => {
    const status = vi.fn().mockResolvedValue(capability('model/a'))
    installApiStub(status)
    const settings = {
      ...DEFAULT_SETTINGS,
      workspace: 'C:/workspace-one',
      activeLlmProvider: 'nvidia' as const,
      nvidiaModel: 'model/a'
    }
    manifestWorkspacePath = settings.workspace
    const { rerender } = render(
      <AgentView {...commonProps} settings={settings} />
    )

    const workspaceScope = await screen.findByLabelText(/작업 폴더 읽기/)
    fireEvent.click(workspaceScope)
    await waitFor(() => expect(screen.getByText(/C:\/workspace-one/)).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: '이 범위 승인' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '승인됨' })).toBeTruthy())

    manifestWorkspacePath = 'C:/workspace-two'
    rerender(
      <AgentView
        {...commonProps}
        settings={{ ...settings, workspace: manifestWorkspacePath }}
      />
    )

    expect(screen.queryByRole('button', { name: '승인됨' })).toBeNull()
    await waitFor(() => expect(screen.getByText(/C:\/workspace-two/)).toBeTruthy())
    expect(screen.getByRole('button', { name: '이 범위 승인' })).toBeTruthy()
  })
})
