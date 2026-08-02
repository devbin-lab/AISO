import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DEFAULT_SETTINGS } from '../../../shared/settings'
import type { ComfyModelProfile } from '../../../shared/comfy-model'
import { fetchAgentToolCatalog, streamAgent } from './agent'
import { ragIndex } from './rag'

const profile = { id: 'profile-manual-1', agentEnabled: true } as ComfyModelProfile

function installApiStub(): void {
  Object.defineProperty(window, 'api', {
    configurable: true,
    value: { backend: { token: () => 'test-token' } }
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

  it('blocks NVIDIA Agent before any backend or external egress', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(streamAgent(
      8123,
      { ...DEFAULT_SETTINGS, activeLlmProvider: 'nvidia' },
      '',
      [{ role: 'user', content: 'run a tool' }],
      'session-nvidia',
      'read',
      [],
      vi.fn()
    )).rejects.toThrow()

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('blocks NVIDIA RAG indexing before Ollama or backend egress', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(ragIndex(
      8123,
      { ...DEFAULT_SETTINGS, activeLlmProvider: 'nvidia' },
      'C:/workspace',
      vi.fn()
    )).rejects.toThrow()

    expect(fetchMock).not.toHaveBeenCalled()
  })
})
