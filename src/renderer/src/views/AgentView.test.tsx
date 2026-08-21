import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import { DEFAULT_SETTINGS } from '../../../shared/settings'
import { NVIDIA_BUILD_BASE_URL } from '../../../shared/nvidia'
import AgentView from './AgentView'
import { MAX_AUTO_CONTINUES } from '../lib/agent'


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

function installApiStub(status: ReturnType<typeof vi.fn>, conversation: unknown = null): void {
  Object.defineProperty(window, 'api', {
    configurable: true,
    value: {
      conversations: {
        list: vi.fn().mockResolvedValue([]),
        get: vi.fn().mockResolvedValue(conversation),
        save: vi.fn().mockResolvedValue(undefined),
        setPinned: vi.fn().mockResolvedValue(undefined),
        remove: vi.fn().mockResolvedValue(undefined)
      },
      nvidia: {
        capabilities: { status },
        agent: {
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
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ indexed: false, count: 0, files: 0 })
    }))
  })
  afterEach(() => vi.unstubAllGlobals())

  it('groups the agent prompt, context controls, and send action in one composer surface', () => {
    installApiStub(vi.fn().mockResolvedValue(null))
    const { container } = render(
      <AgentView
        {...commonProps}
        settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'ollama' }}
      />
    )

    const composer = container.querySelector('.agent-composer')
    expect(composer).toBeTruthy()
    expect(composer?.querySelector('textarea.agent-composer__ta')).toBeTruthy()
    expect(composer?.querySelector('[aria-label="파일 또는 폴더 첨부"]')).toBeTruthy()
    expect(composer?.querySelector('[aria-label="실행"]')).toBeTruthy()
    expect(container.querySelector('.composer-head .ws-pick')).toBeTruthy()
    expect(container.querySelector('.composer-tools')).toBeNull()
  })

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
    await waitFor(() => expect(screen.getByRole('textbox').hasAttribute('disabled')).toBe(false))
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
    await waitFor(() => expect(screen.getByRole('textbox').hasAttribute('disabled')).toBe(false))
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

  it('does not start workspace preview or RAG work while the Agent view is hidden', async () => {
    const status = vi.fn().mockResolvedValue(capability('model/a'))
    installApiStub(status)
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ indexed: false, count: 0, files: 0 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { rerender } = render(
      <AgentView
        {...commonProps}
        active={false}
        settings={{ ...DEFAULT_SETTINGS, workspace: 'C:/workspace', activeLlmProvider: 'ollama' }}
      />
    )

    await act(async () => { await Promise.resolve() })
    expect(fetchMock).not.toHaveBeenCalled()

    rerender(
      <AgentView
        {...commonProps}
        active
        settings={{ ...DEFAULT_SETTINGS, workspace: 'C:/workspace', activeLlmProvider: 'ollama' }}
      />
    )
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
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

    await waitFor(() => expect(screen.getByRole('textbox').hasAttribute('disabled')).toBe(false))
    const input = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: 'do not mutate this request' } })
    fireEvent.click(screen.getByRole('button', { name: '실행' }))
    await waitFor(() => expect(window.api.nvidia.agent.prepare).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(input.value).toBe('do not mutate this request'))

    expect(window.api.conversations.save).not.toHaveBeenCalled()
    expect(screen.queryByText('do not mutate this request', { selector: '.msg' })).toBeNull()
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/agent'))).toBe(false)
  })

  it('starts NVIDIA Agent without a repeated transfer-scope card and sends the current permission mode to Main', async () => {
    const status = vi.fn().mockResolvedValue(capability('model/a'))
    installApiStub(status)
    const encoder = new TextEncoder()
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/agent')) {
        return {
          ok: true,
          status: 200,
          body: new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(encoder.encode(`${JSON.stringify({ type: 'done' })}\n`))
              controller.close()
            }
          })
        }
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
        settings={{
          ...DEFAULT_SETTINGS,
          workspace: 'C:/workspace',
          activeLlmProvider: 'nvidia',
          nvidiaModel: 'model/a'
        }}
      />
    )

    await waitFor(() => expect(status).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('NVIDIA 전송 범위 확인')).toBeNull()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '권한 정책 테스트' } })
    fireEvent.click(screen.getByRole('button', { name: '실행' }))
    await waitFor(() => expect(window.api.nvidia.agent.prepare).toHaveBeenCalledTimes(1))
    expect(window.api.nvidia.agent.prepare).toHaveBeenCalledWith(expect.objectContaining({
      approvalMode: 'read'
    }))
  })

  it('clears a run notice when the Agent stream finishes with an error', async () => {
    const status = vi.fn().mockResolvedValue(null)
    installApiStub(status)
    const encoder = new TextEncoder()
    let streamController: ReadableStreamDefaultController<Uint8Array> | null = null
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/agent')) {
        return {
          ok: true,
          status: 200,
          body: new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller
              controller.enqueue(encoder.encode(`${JSON.stringify({
                type: 'notice',
                text: '이미지 생성 도구 호출을 이어갑니다…',
                transient: true
              })}\n`))
            }
          })
        }
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
        settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'ollama' }}
      />
    )
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: '테스트 이미지 생성' } })
    fireEvent.click(screen.getByRole('button', { name: '실행' }))

    await screen.findByText('이미지 생성 도구 호출을 이어갑니다…')
    await act(async () => {
      ;(streamController as ReadableStreamDefaultController<Uint8Array>).enqueue(encoder.encode([
        JSON.stringify({ type: 'error', error: 'ComfyUI 이미지 생성 실패' }),
        JSON.stringify({ type: 'done' })
      ].join('\n') + '\n'))
      ;(streamController as ReadableStreamDefaultController<Uint8Array>).close()
    })

    await waitFor(() => {
      expect(screen.queryByText('이미지 생성 도구 호출을 이어갑니다…')).toBeNull()
      expect(screen.getByText(/ComfyUI 이미지 생성 실패/)).toBeTruthy()
    })
  })

  it('removes a transient routing notice while preserving a durable notice after the response finishes', async () => {
    const status = vi.fn().mockResolvedValue(null)
    installApiStub(status)
    const encoder = new TextEncoder()
    let streamController: ReadableStreamDefaultController<Uint8Array> | null = null
    const routingNotice = '요청에 맞는 실제 도구 호출이 없어 한 번만 올바른 도구 호출로 다시 시도합니다…'
    const durableNotice = '안전 한도에 도달해 이후 실행은 중단했습니다.'
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/agent')) {
        return {
          ok: true,
          status: 200,
          body: new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller
              controller.enqueue(encoder.encode(`${JSON.stringify({
                type: 'notice',
                text: routingNotice,
                transient: true
              })}\n`))
            }
          })
        }
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
        settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'ollama' }}
      />
    )
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '현재 작업 폴더의 파일구조 보여줘' } })
    fireEvent.click(screen.getByRole('button', { name: '실행' }))

    await screen.findByText(routingNotice)
    await act(async () => {
      ;(streamController as ReadableStreamDefaultController<Uint8Array>).enqueue(encoder.encode([
        JSON.stringify({ type: 'content', text: '현재 작업 폴더의 파일 구조입니다.' }),
        JSON.stringify({ type: 'notice', text: durableNotice }),
        JSON.stringify({ type: 'done' })
      ].join('\n') + '\n'))
      ;(streamController as ReadableStreamDefaultController<Uint8Array>).close()
    })

    await waitFor(() => {
      expect(screen.getByText('현재 작업 폴더의 파일 구조입니다.')).toBeTruthy()
      expect(screen.queryByText(routingNotice)).toBeNull()
      expect(screen.getByText(durableNotice)).toBeTruthy()
    })
  })

  it('uses only a rendered image card as correction context and ignores stale image events', async () => {
    const historicalImage = {
      jobId: 'historical-image-job',
      filename: 'historical.png',
      subfolder: '',
      storageType: 'output',
      baseUrl: 'http://127.0.0.1:8188',
      profileId: 'historical-profile',
      profileName: 'Historical verified image',
      modelName: 'historical-model',
      selectionReason: 'verified result',
      prompt: '1girl',
      negativePrompt: '',
      seed: '42',
      width: 1024,
      height: 1024,
      steps: 20,
      cfg: 5,
      sampler: 'euler',
      scheduler: 'normal'
    }
    const status = vi.fn().mockResolvedValue(null)
    installApiStub(status, {
      id: 'agent-with-image-history',
      kind: 'agent',
      title: 'image history',
      createdAt: 1,
      updatedAt: 1,
      pinned: false,
      data: {
        items: [{ kind: 'image', image: historicalImage }],
        history: [{ role: 'assistant', content: '이미지 생성을 완료했습니다. 결과 카드에서 확인할 수 있습니다.' }],
        plan: [],
        workspace: ''
      }
    })

    const encoder = new TextEncoder()
    const agentRequests: Array<Record<string, unknown>> = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/agent')) {
        const request = JSON.parse(String(init?.body)) as Record<string, string>
        agentRequests.push(request)
        const currentImage = {
          ...historicalImage,
          jobId: 'current-image-job',
          filename: 'current.png',
          profileName: 'Current image result'
        }
        const staleImage = {
          ...historicalImage,
          jobId: 'stale-image-job',
          filename: 'stale.png',
          profileName: 'Stale image result'
        }
        return {
          ok: true,
          status: 200,
          body: new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(encoder.encode([
                JSON.stringify({
                  type: 'image_result',
                  id: 'stale-image-call',
                  assistantTurnId: 'stale-assistant-turn',
                  image: staleImage
                }),
                JSON.stringify({
                  type: 'image_result',
                  id: 'current-image-call',
                  assistantTurnId: request.assistant_turn_id,
                  image: currentImage
                }),
                JSON.stringify({ type: 'content', text: 'stream complete' }),
                JSON.stringify({ type: 'done' })
              ].join('\n') + '\n'))
              controller.close()
            }
          })
        }
      }
      if (url.includes('/comfy/image?')) return { ok: false, status: 404 }
      return {
        ok: true,
        status: 200,
        json: vi.fn().mockResolvedValue({ indexed: false, count: 0, files: 0 })
      }
    }))

    render(
      <AgentView
        {...commonProps}
        conversationRequest={{ kind: 'agent', id: 'agent-with-image-history', nonce: 1 }}
        settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'ollama' }}
      />
    )

    await screen.findByText('Historical verified image')
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'continue' } })
    fireEvent.click(screen.getByRole('button', { name: '실행' }))

    await screen.findByText('stream complete')
    await waitFor(() => expect(agentRequests).toHaveLength(1))
    expect(agentRequests[0]?.image_context_verified).toBe(true)
    expect(screen.getByText('Current image result')).toBeTruthy()
    expect(screen.queryByText('Stale image result')).toBeNull()
    expect(document.querySelectorAll('.generated-image')).toHaveLength(2)
  })
  it('carries the harness run summary into the next run so "계속해줘" is not a blank restart', async () => {
    // 하네스는 안전 한도로 멈출 때 "여기까지 한 내용은 유지됩니다"라고 안내한다.
    // 예전에는 마지막 assistant 텍스트 하나만 이어붙여서 그 안내가 사실이 아니었다.
    const status = vi.fn().mockResolvedValue(null)
    installApiStub(status)

    const encoder = new TextEncoder()
    const agentRequests: Array<Record<string, unknown>> = []
    const SUMMARY = `[이번 실행에서 실제로 수행한 도구]
- write_file a.md — 성공`

    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/agent')) {
        agentRequests.push(JSON.parse(String(init?.body)) as Record<string, unknown>)
        const first = agentRequests.length === 1
        return {
          ok: true,
          status: 200,
          body: new ReadableStream<Uint8Array>({
            start(controller) {
              const events = first
                ? [
                    { type: 'notice', text: '안전선에서 멈췄습니다.' },
                    { type: 'run_summary', text: SUMMARY },
                    { type: 'done' }
                  ]
                : [{ type: 'content', text: '이어서 진행했습니다.' }, { type: 'done' }]
              controller.enqueue(encoder.encode(events.map((e) => JSON.stringify(e)).join(`\n`) + `\n`))
              controller.close()
            }
          })
        }
      }
      return { ok: true, status: 200, json: vi.fn().mockResolvedValue({ indexed: false, count: 0, files: 0 }) }
    }))

    render(<AgentView {...commonProps} settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'ollama' }} />)

    fireEvent.change(screen.getByRole('textbox'), { target: { value: '긴 작업 해줘' } })
    fireEvent.click(screen.getByRole('button', { name: '실행' }))
    await waitFor(() => expect(agentRequests).toHaveLength(1))
    await screen.findByText('안전선에서 멈췄습니다.')

    fireEvent.change(screen.getByRole('textbox'), { target: { value: '계속해줘' } })
    fireEvent.click(screen.getByRole('button', { name: '실행' }))
    await waitFor(() => expect(agentRequests).toHaveLength(2))

    const second = JSON.stringify(agentRequests[1]?.messages ?? [])
    expect(second).toContain('write_file a.md')
  })
  it('carries recent tool results into the next run, correctly paired', async () => {
    // 결과 본문은 이미 타임라인에 있다. 예전에는 모델에게 보내지 않아
    // '계속해줘'가 무엇을 읽었는지 모른 채 다시 시작했다.
    const status = vi.fn().mockResolvedValue(null)
    installApiStub(status, {
      id: 'agent-with-tool-history',
      kind: 'agent',
      title: 'tool history',
      createdAt: 1,
      updatedAt: 1,
      pinned: false,
      data: {
        items: [
          {
            kind: 'tool',
            callId: 'exec-1',
            approvalId: 'appr-1',
            providerToolCallId: 'ollama-turn-a-0',
            assistantTurnId: 'turn-a',
            name: 'read_file',
            args: { path: 'spec.md' },
            status: 'done',
            output: 'SPEC_CANARY 문서 본문'
          },
          {
            kind: 'tool',
            callId: 'exec-2',
            approvalId: 'appr-2',
            providerToolCallId: 'ollama-turn-a-1',
            assistantTurnId: 'turn-a',
            name: 'list_dir',
            args: { path: '.' },
            status: 'awaiting'
          }
        ],
        history: [{ role: 'assistant', content: '안전선에서 멈췄습니다.' }],
        plan: [],
        workspace: ''
      }
    })

    const encoder = new TextEncoder()
    const agentRequests: Array<Record<string, unknown>> = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/agent')) {
        agentRequests.push(JSON.parse(String(init?.body)) as Record<string, unknown>)
        return {
          ok: true,
          status: 200,
          body: new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(encoder.encode(
                [{ type: 'content', text: '이어서 진행합니다.' }, { type: 'done' }]
                  .map((e) => JSON.stringify(e)).join(`
`) + `
`
              ))
              controller.close()
            }
          })
        }
      }
      return { ok: true, status: 200, json: vi.fn().mockResolvedValue({ indexed: false, count: 0, files: 0 }) }
    }))

    render(
      <AgentView
        {...commonProps}
        conversationRequest={{ kind: 'agent', id: 'agent-with-tool-history', nonce: 1 }}
        settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'ollama' }}
      />
    )

    // 저장된 history는 렌더되지 않는다 — 타임라인(items)의 도구 카드로 로드를 기다린다.
    await screen.findByText('spec.md')
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '계속해줘' } })
    fireEvent.click(screen.getByRole('button', { name: '실행' }))
    await waitFor(() => expect(agentRequests).toHaveLength(1))

    const messages = (agentRequests[0]?.messages ?? []) as Array<Record<string, unknown>>
    const serialized = JSON.stringify(messages)
    expect(serialized).toContain('SPEC_CANARY')

    // 결과가 없는 호출(승인 대기)은 실리지 않는다 — 짝이 깨지면 공급자가 거부한다.
    expect(serialized).not.toContain('ollama-turn-a-1')

    const assistantWithCalls = messages.find((m) => Array.isArray(m.tool_calls))
    expect(assistantWithCalls).toBeTruthy()
    const calls = assistantWithCalls!.tool_calls as Array<Record<string, unknown>>
    expect(calls).toHaveLength(1)
    const toolMessages = messages.filter((m) => m.role === 'tool')
    expect(toolMessages).toHaveLength(1)
    expect(toolMessages[0].tool_call_id).toBe(calls[0].id)

    // 새 사용자 발화는 도구 기록 뒤에 온다.
    expect(messages[messages.length - 1].role).toBe('user')
  })
  it('keeps the preview iframe sandboxed against popups and top-level navigation', () => {
    // /f/ 응답의 PREVIEW_CSP는 window.open을 막지 못한다 — CSP에 그 지시어가 없다
    // (navigate-to는 스펙 폐기). 그 마지막 유출 채널을 닫는 것이 이 sandbox 속성이고,
    // 값이 조용히 넓어지면 방어가 사라지므로 여기서 고정한다.
    installApiStub(vi.fn().mockResolvedValue(null))
    const { container } = render(
      <AgentView {...commonProps} settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'ollama' }} />
    )
    const iframes = Array.from(container.querySelectorAll('iframe'))
    for (const frame of iframes) {
      const sandbox = frame.getAttribute('sandbox') ?? ''
      expect(sandbox).not.toContain('allow-popups')
      expect(sandbox).not.toContain('allow-top-navigation')
      expect(sandbox).not.toContain('allow-modals')
    }
  })

  // ── 작업 자동 이어가기 ──────────────────────────────────────────────

  const chr10 = String.fromCharCode(10)

  function limitStream(encoder: TextEncoder) {
    return (): Response => {
      const events = [
        { type: 'tool_call', id: 't1', name: 'write_file', args: { path: 'a.md' }, assistantTurnId: 'a1' },
        { type: 'tool_result', id: 't1', ok: true, output: '됨', name: 'write_file', assistantTurnId: 'a1' },
        { type: 'notice', text: '안전선에서 멈췄습니다.' },
        { type: 'run_summary', text: '[이번 실행에서 실제로 수행한 도구] - write_file a.md — 성공' },
        { type: 'run_limit', reason: 'max_steps' },
        { type: 'done' }
      ]
      return {
        ok: true,
        status: 200,
        body: new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode(events.map((e) => JSON.stringify(e)).join(chr10) + chr10))
            controller.close()
          }
        })
      } as unknown as Response
    }
  }

  it('설정이 꺼져 있으면 한도에서 멈추고 스스로 이어가지 않는다', async () => {
    const status = vi.fn().mockResolvedValue(null)
    installApiStub(status)
    const agentRequests: unknown[] = []
    const encoder = new TextEncoder()
    const stream = limitStream(encoder)
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).includes('/agent')) {
        agentRequests.push(JSON.parse(String(init?.body ?? '{}')))
        return stream()
      }
      return { ok: true, status: 200, json: vi.fn().mockResolvedValue({ indexed: false, count: 0, files: 0 }) }
    }))
    render(<AgentView {...commonProps}
      settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'ollama', autoContinueOnLimit: false }} />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '긴 작업' } })
    fireEvent.click(screen.getByRole('button', { name: '실행' }))
    await waitFor(() => expect(agentRequests).toHaveLength(1))
    await screen.findByText('안전선에서 멈췄습니다.')
    // 잠깐 기다려도 두 번째 요청이 생기지 않는다.
    await new Promise((resolve) => setTimeout(resolve, 60))
    expect(agentRequests).toHaveLength(1)
  })

  it('설정이 켜져 있으면 한도에서 스스로 이어가되 상한을 넘지 않는다', async () => {
    // 상한이 없으면 "한도 → 이어감 → 또 한도"가 무한히 돈다. 토큰이 사용자 모르게 계속 나간다.
    const status = vi.fn().mockResolvedValue(null)
    installApiStub(status)
    const agentRequests: { messages?: unknown[] }[] = []
    const encoder = new TextEncoder()
    const stream = limitStream(encoder)
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).includes('/agent')) {
        agentRequests.push(JSON.parse(String(init?.body ?? '{}')))
        return stream()          // 매번 한도로 끝난다 = 최악의 경우
      }
      return { ok: true, status: 200, json: vi.fn().mockResolvedValue({ indexed: false, count: 0, files: 0 }) }
    }))
    render(<AgentView {...commonProps}
      settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'ollama', autoContinueOnLimit: true }} />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '긴 작업' } })
    fireEvent.click(screen.getByRole('button', { name: '실행' }))

    // 최초 1회 + 자동 이어가기 MAX_AUTO_CONTINUES 회에서 **정확히 멈춘다**.
    await waitFor(
      () => expect(agentRequests).toHaveLength(1 + MAX_AUTO_CONTINUES),
      { timeout: 4000 }
    )
    await new Promise((resolve) => setTimeout(resolve, 120))
    expect(agentRequests).toHaveLength(1 + MAX_AUTO_CONTINUES)

    // 이어가는 요청은 백지가 아니다 — 이전 런이 실제로 한 일이 함께 넘어간다.
    const second = JSON.stringify(agentRequests[1]?.messages ?? [])
    expect(second).toContain('write_file a.md')
    expect(second).toContain('이어서 계속 진행해라')
  })
})
