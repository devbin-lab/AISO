import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DEFAULT_SETTINGS, type AppSettings } from '../../../shared/settings'
import { NVIDIA_BUILD_BASE_URL } from '../../../shared/nvidia'
import { streamChat } from './chat'

function streamResponse(lines: string[]): {
  ok: boolean
  status: number
  body: { getReader: () => ReadableStreamDefaultReader<Uint8Array> }
} {
  const chunks = lines.map((line) => new TextEncoder().encode(line))
  const read = vi.fn()
  for (const value of chunks) read.mockResolvedValueOnce({ done: false, value })
  read.mockResolvedValueOnce({ done: true, value: undefined })
  return {
    ok: true,
    status: 200,
    body: { getReader: () => ({ read } as unknown as ReadableStreamDefaultReader<Uint8Array>) }
  }
}

function nvidiaSettings(patch: Partial<AppSettings> = {}): AppSettings {
  return {
    ...DEFAULT_SETTINGS,
    activeLlmProvider: 'nvidia',
    nvidiaDeploymentMode: 'build',
    nvidiaModel: 'meta/llama-test',
    chatWebSearch: false,
    ...patch
  }
}

describe('streamChat NVIDIA execution boundary', () => {
  let prepare: ReturnType<typeof vi.fn>

  beforeEach(() => {
    prepare = vi.fn().mockResolvedValue({ ready: true, credential: 'stored' })
    Object.defineProperty(window, 'api', {
      configurable: true,
      value: {
        backend: { token: () => 'renderer-session-token' },
        nvidia: { execution: { prepare } }
      }
    })
  })

  afterEach(() => vi.unstubAllGlobals())

  it('prepares and sends one immutable NVIDIA snapshot without Ollama routing or a key', async () => {
    const settings = nvidiaSettings()
    prepare.mockImplementation(async () => {
      settings.activeLlmProvider = 'ollama'
      settings.model = 'changed-after-start'
      settings.ollamaHost = 'http://attacker.invalid:11434'
      return { ready: true, credential: 'stored' }
    })
    const fetchMock = vi.fn().mockResolvedValue(streamResponse([
      '{"type":"content","text":"ok"}\n{"type":"do',
      'ne"}\n'
    ]))
    vi.stubGlobal('fetch', fetchMock)
    const events: string[] = []

    await streamChat(
      8123,
      settings,
      [{ role: 'user', content: 'hello' }],
      (event) => events.push(event.type)
    )

    expect(prepare).toHaveBeenCalledWith({
      deploymentMode: 'build',
      endpoint: NVIDIA_BUILD_BASE_URL
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body)) as Record<string, unknown>
    expect(body.provider).toBe('nvidia')
    expect(body.deployment_mode).toBe('build')
    expect(body.endpoint).toBe(NVIDIA_BUILD_BASE_URL)
    expect(body.model).toBe('meta/llama-test')
    expect(body).not.toHaveProperty('ollama_host')
    expect(JSON.stringify(body)).not.toContain('CANARY')
    expect(events).toEqual(['content', 'done'])
  })

  it('blocks unsupported NVIDIA research before prepare or any network egress', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(streamChat(
      8123,
      nvidiaSettings({ chatWebSearch: true }),
      [{ role: 'user', content: 'research this' }],
      vi.fn()
    )).rejects.toThrow()

    expect(prepare).not.toHaveBeenCalled()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('does not call the sidecar chat endpoint when Main preparation fails', async () => {
    prepare.mockRejectedValue(new Error('binding denied'))
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(streamChat(
      8123,
      nvidiaSettings(),
      [{ role: 'user', content: 'hello' }],
      vi.fn()
    )).rejects.toThrow('binding denied')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects a malformed or unterminated backend stream instead of faking done', async () => {
    const malformedFetch = vi.fn().mockResolvedValue(streamResponse(['not-json\n']))
    vi.stubGlobal('fetch', malformedFetch)
    await expect(streamChat(
      8123,
      nvidiaSettings(),
      [{ role: 'user', content: 'hello' }],
      vi.fn()
    )).rejects.toThrow()

    const truncatedFetch = vi.fn().mockResolvedValue(streamResponse([
      '{"type":"content","text":"partial"}\n'
    ]))
    vi.stubGlobal('fetch', truncatedFetch)
    await expect(streamChat(
      8123,
      nvidiaSettings(),
      [{ role: 'user', content: 'hello' }],
      vi.fn()
    )).rejects.toThrow()
  })
})
