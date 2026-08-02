import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

  it('shows the selected NVIDIA model instead of the Ollama model in the Agent model control', async () => {
    const status = vi.fn().mockResolvedValue(capability('nvidia/model-selected'))
    installApiStub(status)
    const onSaveSettings = vi.fn().mockResolvedValue(true)
    render(
      <AgentView
        {...commonProps}
        onSaveSettings={onSaveSettings}
        settings={{
          ...DEFAULT_SETTINGS,
          model: 'gpt-oss:20b',
          activeLlmProvider: 'nvidia',
          nvidiaModel: 'nvidia/model-selected'
        }}
      />
    )

    await waitFor(() => expect(status).toHaveBeenCalledTimes(1))
    const modelControl = screen.getByRole('button', { name: '모델' })
    expect(modelControl.textContent).toContain('nvidia/model-selected')
    expect(modelControl.textContent).not.toContain('gpt-oss:20b')
    fireEvent.click(modelControl)
    const nvidiaOption = screen.getByRole('option', { name: 'nvidia/model-selected' })
    expect(screen.queryByRole('option', { name: 'gpt-oss:20b' })).toBeNull()
    fireEvent.click(nvidiaOption)
    expect(onSaveSettings).toHaveBeenCalledWith({ nvidiaModel: 'nvidia/model-selected' })
  })

  it('keeps a tool approval visible and retryable when the backend rejects it', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const status = vi.fn().mockResolvedValue(capability('model/a'))
    installApiStub(status)
    let streamController: ReadableStreamDefaultController<Uint8Array> | null = null
    let resolveApproval!: (value: { ok: boolean }) => void
    const approvalResult = new Promise<{ ok: boolean }>((resolve) => { resolveApproval = resolve })
    const encoder = new TextEncoder()
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/agent/approve')) {
        return {
          ok: true,
          status: 200,
          json: vi.fn(() => approvalResult)
        }
      }
      if (url.endsWith('/agent')) {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            streamController = controller
            controller.enqueue(encoder.encode([
              JSON.stringify({
                type: 'tool_call',
                executionId: 'execution-approval-ui',
                approvalId: 'approval-ui',
                providerToolCallId: 'provider-tool-ui',
                assistantTurnId: 'assistant-turn-ui',
                name: 'run_command',
                args: { command: 'safe-test' }
              }),
              JSON.stringify({
                type: 'approval_request',
                executionId: 'execution-approval-ui',
                approvalId: 'approval-ui',
                providerToolCallId: 'provider-tool-ui',
                assistantTurnId: 'assistant-turn-ui',
                name: 'run_command',
                args: { command: 'safe-test' }
              })
            ].join('\n') + '\n'))
          }
        })
        return { ok: true, status: 200, body }
      }
      return {
        ok: true,
        status: 200,
        json: vi.fn().mockResolvedValue({ indexed: false, count: 0, files: 0 })
      }
    })
    vi.stubGlobal('fetch', fetchMock)

    render(
      <AgentView
        {...commonProps}
        settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'nvidia', nvidiaModel: 'model/a' }}
      />
    )
    const manifestApprove = await screen.findByRole('button', { name: '이 범위 승인' })
    fireEvent.click(manifestApprove)
    await screen.findByRole('button', { name: '승인됨' })
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: '승인 UI 테스트' } })
    fireEvent.click(screen.getByRole('button', { name: '실행' }))

    const approve = await screen.findByRole('button', { name: '승인' })
    fireEvent.click(approve)
    fireEvent.click(approve)
    await waitFor(() => {
      expect(screen.getByText('승인 필요')).toBeTruthy()
      expect(screen.getByRole('button', { name: '처리 중…' })).toHaveProperty('disabled', true)
      expect(screen.getByRole('button', { name: '거부' })).toHaveProperty('disabled', true)
    })
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/agent/approve'))).toHaveLength(1)

    resolveApproval({ ok: false })
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/만료되었거나/))
    expect(screen.getByText('승인 필요')).toBeTruthy()
    expect(screen.getByRole('button', { name: '승인' })).toHaveProperty('disabled', false)
    expect(screen.getByRole('button', { name: '거부' })).toHaveProperty('disabled', false)
    ;(streamController as ReadableStreamDefaultController<Uint8Array> | null)?.close()
    consoleError.mockRestore()
  })

  it('does not let a late approval acknowledgement overwrite an earlier tool result', async () => {
    const status = vi.fn().mockResolvedValue(capability('model/a'))
    installApiStub(status)
    let streamController: ReadableStreamDefaultController<Uint8Array> | null = null
    let resolveApproval!: (value: { ok: boolean }) => void
    const approvalResult = new Promise<{ ok: boolean }>((resolve) => { resolveApproval = resolve })
    const encoder = new TextEncoder()
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/agent/approve')) {
        return { ok: true, status: 200, json: vi.fn(() => approvalResult) }
      }
      if (url.endsWith('/agent')) {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            streamController = controller
            const identity = {
              id: 'execution-fast-result',
              executionId: 'execution-fast-result',
              approvalId: 'approval-fast-result',
              providerToolCallId: 'provider-fast-result',
              assistantTurnId: 'assistant-fast-result'
            }
            controller.enqueue(encoder.encode([
              JSON.stringify({
                type: 'tool_call',
                ...identity,
                name: 'run_command',
                args: { command: 'safe-test' }
              }),
              JSON.stringify({
                type: 'approval_request',
                ...identity,
                name: 'run_command',
                args: { command: 'safe-test' }
              })
            ].join('\n') + '\n'))
          }
        })
        return { ok: true, status: 200, body }
      }
      return {
        ok: true,
        status: 200,
        json: vi.fn().mockResolvedValue({ indexed: false, count: 0, files: 0 })
      }
    }))

    render(
      <AgentView
        {...commonProps}
        settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'nvidia', nvidiaModel: 'model/a' }}
      />
    )
    fireEvent.click(await screen.findByRole('button', { name: '이 범위 승인' }))
    await screen.findByRole('button', { name: '승인됨' })
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '승인 경쟁 테스트' } })
    fireEvent.click(screen.getByRole('button', { name: '실행' }))
    fireEvent.click(await screen.findByRole('button', { name: '승인' }))

    await act(async () => {
      ;(streamController as ReadableStreamDefaultController<Uint8Array>).enqueue(encoder.encode(
        JSON.stringify({
          type: 'tool_result',
          id: 'execution-fast-result',
          executionId: 'execution-fast-result',
          approvalId: 'approval-fast-result',
          providerToolCallId: 'provider-fast-result',
          assistantTurnId: 'assistant-fast-result',
          ok: true,
          output: 'completed first'
        }) + '\n'
      ))
    })
    await waitFor(() => expect(document.querySelector('.tool__badge')?.textContent).toBe('완료'))

    await act(async () => {
      resolveApproval({ ok: true })
      await Promise.resolve()
    })
    expect(document.querySelector('.tool__badge')?.textContent).toBe('완료')
    expect(screen.queryByText('승인 필요')).toBeNull()
    ;(streamController as ReadableStreamDefaultController<Uint8Array> | null)?.close()
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
