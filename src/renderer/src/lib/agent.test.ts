import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DEFAULT_SETTINGS } from '../../../shared/settings'
import type { ComfyModelProfile } from '../../../shared/comfy-model'
import { approveAgent, fetchAgentToolCatalog, streamAgent } from './agent'
import { ragIndex } from './rag'

const profile = { id: 'profile-manual-1', agentEnabled: true } as ComfyModelProfile

function installApiStub(): void {
  Object.defineProperty(window, 'api', {
    configurable: true,
    value: {
      backend: { token: () => 'test-token' },
      nvidia: {
        agent: {
          prepare: vi.fn().mockResolvedValue({
            grantId: 'one-use-grant',
            assistantTurnId: 'assistant-turn-test',
            expiresInSeconds: 60
          })
        }
      }
    }
  })
}

function streamResponse(): { ok: boolean; body: { getReader: () => ReadableStreamDefaultReader<Uint8Array> } } {
  const reader = {
    read: vi
      .fn()
      .mockResolvedValueOnce({ done: false, value: new TextEncoder().encode('{"type":"done"}\n') })
      .mockResolvedValueOnce({ done: true, value: undefined })
  } as unknown as ReadableStreamDefaultReader<Uint8Array>
  return { ok: true, body: { getReader: () => reader } }
}

describe('streamAgent ComfyUI model-selection payload', () => {
  beforeEach(() => installApiStub())
  afterEach(() => vi.unstubAllGlobals())

  it('loads the tool catalog through the authenticated backend endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ tools: [] })
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchAgentToolCatalog(8123)).resolves.toEqual({ tools: [] })
    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8123/agent/tools',
      { headers: { 'X-Aiso-Token': 'test-token' } }
    )
  })

  it('sends the exact selected profile ID only in manual mode', async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamResponse())
    vi.stubGlobal('fetch', fetchMock)

    await streamAgent(
      8123,
      { ...DEFAULT_SETTINGS, comfyModelSelectionMode: 'manual' },
      '',
      [{ role: 'user', content: '그림을 그려줘' }],
      'session-1',
      'assistant-turn-1',
      'read',
      [profile],
      vi.fn(),
      undefined,
      { selectedComfyModelId: profile.id }
    )

    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body)) as Record<string, unknown>
    expect(body.comfy_selection_mode).toBe('manual')
    expect(body.selected_comfy_model_id).toBe(profile.id)
    expect(body.comfy_profiles).toEqual([profile])
  })

  it('does not send a manual selection ID in automatic mode', async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamResponse())
    vi.stubGlobal('fetch', fetchMock)

    await streamAgent(
      8123,
      DEFAULT_SETTINGS,
      '',
      [{ role: 'user', content: '그림을 그려줘' }],
      'session-2',
      'assistant-turn-2',
      'read',
      [profile],
      vi.fn(),
      undefined,
      { selectedComfyModelId: profile.id }
    )

    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body)) as Record<string, unknown>
    expect(body.comfy_selection_mode).toBe('auto')
    expect(body).not.toHaveProperty('selected_comfy_model_id')
  })

  it('sends only the approval ID to the approval endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true })
    vi.stubGlobal('fetch', fetchMock)

    await approveAgent(8123, 'session-approval-01', 'approval-distinct-01', true)

    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body)) as Record<string, unknown>
    expect(body).toEqual({
      session_id: 'session-approval-01',
      call_id: 'approval-distinct-01',
      approved: true
    })
    expect(JSON.stringify(body)).not.toContain('execution-distinct-01')
    expect(JSON.stringify(body)).not.toContain('provider-call-distinct-01')
  })

  it('requires a Main grant and strips workspace, RAG, and Comfy metadata from NVIDIA Agent', async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamResponse())
    vi.stubGlobal('fetch', fetchMock)

    await streamAgent(
      8123,
      {
        ...DEFAULT_SETTINGS,
        activeLlmProvider: 'nvidia',
        nvidiaDeploymentMode: 'build',
        nvidiaModel: 'nvidia/test-model',
        ragEnabled: true,
        comfyModelSelectionMode: 'manual'
      },
      'C:/private/workspace',
      [{ role: 'user', content: 'run a tool' }],
      'session-nvidia-0001',
      'assistant-turn-0001',
      'read',
      [profile],
      vi.fn(),
      undefined,
      { nvidiaGrantId: 'one-use-grant' }
    )

    expect(window.api.nvidia.agent.prepare).not.toHaveBeenCalled()
    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body)) as Record<string, unknown>
    expect(body).toMatchObject({
      provider: 'nvidia',
      workspace: '',
      model: 'nvidia/test-model',
      assistant_turn_id: 'assistant-turn-0001',
      nvidia_grant: 'one-use-grant',
      rag_enabled: false,
      comfy_profiles: [],
      comfy_base_url: null,
      comfy_selection_mode: 'auto'
    })
    expect(JSON.stringify(body)).not.toContain('C:/private/workspace')
    expect(body).not.toHaveProperty('selected_comfy_model_id')
  })

  it('does not call the sidecar without a pre-issued Main NVIDIA Agent grant', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(streamAgent(
      8123,
      { ...DEFAULT_SETTINGS, activeLlmProvider: 'nvidia', nvidiaModel: 'nvidia/test-model' },
      'C:/private/workspace',
      [{ role: 'user', content: 'run a tool' }],
      'session-nvidia-0002',
      'assistant-turn-0002',
      'read',
      [profile],
      vi.fn()
    )).rejects.toThrow('NVIDIA Agent')

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('keeps NVIDIA Agent RAG indexing local through the configured Ollama endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamResponse())
    vi.stubGlobal('fetch', fetchMock)

    await expect(ragIndex(
      8123,
      {
        ...DEFAULT_SETTINGS,
        activeLlmProvider: 'nvidia',
        nvidiaModel: 'NVIDIA-MODEL-CANARY'
      },
      'C:/workspace',
      vi.fn()
    )).resolves.toBeUndefined()

    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body)) as Record<string, unknown>
    expect(body).toMatchObject({
      workspace: 'C:/workspace',
      ollama_host: DEFAULT_SETTINGS.ollamaHost,
      embed_model: DEFAULT_SETTINGS.embeddingModel
    })
    expect(JSON.stringify(body)).not.toContain('NVIDIA-MODEL-CANARY')
  })
})
