import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { BackendInfo } from '../../../shared/backend'
import { DEFAULT_SETTINGS } from '../../../shared/settings'
import SettingsView from './SettingsView'
import { ConfirmHost } from '../components/ConfirmDialog'

const READY_BACKEND: BackendInfo = { state: 'ready', port: 8123 }

function installApiStub(): void {
  Object.defineProperty(window, 'api', {
    configurable: true,
    value: {
      settings: {
        recoveryStatus: vi.fn().mockResolvedValue({ kind: 'none' })
      },
      nvidia: {
        credential: {
          status: vi.fn().mockResolvedValue({
            encryptionAvailable: true,
            hasStoredCredential: false,
            matchesCurrentBinding: false,
            usableForCurrentBinding: false
          }),
          save: vi.fn().mockResolvedValue(undefined),
          replace: vi.fn().mockResolvedValue(undefined),
          delete: vi.fn().mockResolvedValue(undefined)
        },
        models: {
          refresh: vi.fn().mockResolvedValue({
            models: ['model/a', 'model/b'],
            refreshedAt: '2026-08-02T05:00:00.000Z'
          })
        },
        capabilities: {
          status: vi.fn().mockResolvedValue(null),
          probe: vi.fn().mockResolvedValue({
            schemaVersion: 1,
            binding: {
              deploymentMode: 'build',
              endpoint: 'https://integrate.api.nvidia.com/v1'
            },
            model: 'model/a',
            capabilities: { chat: 'supported', stream: 'supported', tools: 'supported' },
            checkedAt: '2026-08-02T05:01:00.000Z'
          }),
          clear: vi.fn().mockResolvedValue(undefined)
        }
      },
      backend: {
        token: vi.fn(() => 'test-token')
      },
      updates: {
        version: vi.fn().mockResolvedValue('0.3.0'),
        onStatus: vi.fn(() => () => {})
      },
      comfy: {
        pickInstall: vi.fn().mockResolvedValue(null),
        models: {
          list: vi.fn().mockResolvedValue({ profiles: [] }),
          onImportProgress: vi.fn(() => () => {})
        }
      },
      skills: {
        list: vi.fn().mockResolvedValue([]),
        remove: vi.fn().mockResolvedValue(undefined)
      },
      discord: {
        hasToken: vi.fn().mockResolvedValue(false),
        status: vi.fn().mockResolvedValue(null),
        schedules: vi.fn().mockResolvedValue({ jobs: [] }),
        scheduleRemove: vi.fn().mockResolvedValue(undefined),
        setLlmProvider: vi.fn()
      }
    }
  })
}

describe('SettingsView', () => {
  beforeEach(() => installApiStub())
  afterEach(() => vi.unstubAllGlobals())

  it('combines engine, generation, and search settings in the LLM tab', () => {
    render(<SettingsView settings={DEFAULT_SETTINGS} backend={READY_BACKEND} health={null} onSave={vi.fn().mockResolvedValue(true)} active={false} />)

    expect(screen.getByRole('button', { name: 'LLM' })).not.toBeNull()
    expect(screen.queryByRole('button', { name: '엔진' })).toBeNull()
    expect(screen.queryByRole('button', { name: '생성' })).toBeNull()
    expect(screen.queryByRole('button', { name: '검색·RAG' })).toBeNull()
    expect(screen.queryByRole('button', { name: '리소스' })).toBeNull()
    expect(screen.getByText('Ollama 호스트')).not.toBeNull()
    expect(screen.getByText('생성 온도')).not.toBeNull()
    expect(screen.getByText('RAG 사용')).not.toBeNull()
    expect(screen.getByText('AI 즉시 정지 (GPU 비우기)')).not.toBeNull()
  })

  it('keeps NVIDIA key lifecycle separate from persisted settings', async () => {
    const user = userEvent.setup()
    const onSave = vi.fn().mockResolvedValue(true)
    render(<SettingsView settings={DEFAULT_SETTINGS} backend={READY_BACKEND} health={null} onSave={onSave} active />)

    await user.click(screen.getByRole('button', { name: 'NVIDIA' }))
    expect(screen.getByDisplayValue('https://integrate.api.nvidia.com/v1')).toHaveProperty('readOnly', true)
    expect(screen.getByText('NVIDIA 데이터 전송 및 Agent 권한')).not.toBeNull()
    expect(screen.getByText(/반복 확인창은 표시하지 않습니다/)).not.toBeNull()
    expect(screen.getByText(/선택한 작업 폴더·로컬 RAG의 읽기 결과가 모델에 자동 전달/)).not.toBeNull()
    expect(screen.getByText(/API 키와 Discord 토큰은 모델 입력에 포함하지 않고/)).not.toBeNull()
    expect(screen.getByText(/원본 워크플로·모델 경로·생성 이미지 바이트를 NVIDIA 대화에 자동 첨부하지 않습니다/)).not.toBeNull()
    expect(screen.getByText(/이미지 생성 프롬프트와 성공·실패 여부·크기 정보/)).not.toBeNull()

    const keyInput = screen.getByPlaceholderText('API 키 입력')
    await user.type(keyInput, 'renderer-canary-key')
    await user.click(screen.getByRole('button', { name: '키 저장' }))
    await waitFor(() => {
      expect(window.api.nvidia.credential.save).toHaveBeenCalledWith(
        { deploymentMode: 'build', endpoint: undefined },
        'renderer-canary-key'
      )
    })
    expect((keyInput as HTMLInputElement).value).toBe('')

    await user.click(screen.getByRole('button', { name: '저장' }))
    await waitFor(() => expect(onSave).toHaveBeenCalled())
    const persistedPatch = onSave.mock.calls.at(-1)?.[0] as Record<string, unknown>
    expect(persistedPatch.activeLlmProvider).toBe('nvidia')
    expect('nvidiaApiKey' in persistedPatch).toBe(false)
    expect(JSON.stringify(persistedPatch)).not.toContain('renderer-canary-key')
  })

  it('performs no model refresh or capability probe on startup or provider selection', async () => {
    const user = userEvent.setup()
    render(<SettingsView settings={DEFAULT_SETTINGS} backend={READY_BACKEND} health={null} onSave={vi.fn().mockResolvedValue(true)} active />)

    expect(window.api.nvidia.models.refresh).not.toHaveBeenCalled()
    expect(window.api.nvidia.capabilities.probe).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'NVIDIA' }))
    await Promise.resolve()
    expect(window.api.nvidia.models.refresh).not.toHaveBeenCalled()
    expect(window.api.nvidia.capabilities.probe).not.toHaveBeenCalled()
  })

  it('marks an undecryptable stored NVIDIA key as unusable and asks for replacement', async () => {
    window.api.nvidia.credential.status = vi.fn().mockResolvedValue({
      encryptionAvailable: true,
      hasStoredCredential: true,
      matchesCurrentBinding: true,
      usableForCurrentBinding: false,
      detail: '저장된 NVIDIA API 키를 현재 보안 저장소로 해독할 수 없습니다. 설정 > LLM에서 키를 다시 입력해 교체하세요.'
    })
    render(
      <SettingsView
        settings={{ ...DEFAULT_SETTINGS, activeLlmProvider: 'nvidia', nvidiaModel: 'model/a' }}
        backend={READY_BACKEND}
        health={null}
        onSave={vi.fn().mockResolvedValue(true)}
        active
      />
    )

    expect(await screen.findByText(/저장된 키를 해독할 수 없습니다/)).toBeTruthy()
    expect(screen.getByText(/같은 키를 다시 입력해 교체하세요/)).toBeTruthy()
    expect(screen.getByRole('button', { name: '모델 목록 새로고침' })).toHaveProperty('disabled', true)
    expect(screen.getByRole('button', { name: '기능 검사' })).toHaveProperty('disabled', true)
  })

  it('refreshes the NVIDIA model list only on explicit action and keeps manual IDs available', async () => {
    const user = userEvent.setup()
    window.api.nvidia.credential.status = vi.fn().mockResolvedValue({
      encryptionAvailable: true,
      hasStoredCredential: true,
      matchesCurrentBinding: true,
      usableForCurrentBinding: true
    })
    const nvidiaSettings = {
      ...DEFAULT_SETTINGS,
      activeLlmProvider: 'nvidia' as const,
      nvidiaModel: 'model/a',
      chatWebSearch: false
    }
    render(<SettingsView settings={nvidiaSettings} backend={READY_BACKEND} health={null} onSave={vi.fn().mockResolvedValue(true)} active />)

    expect(window.api.nvidia.models.refresh).not.toHaveBeenCalled()
    await waitFor(() => expect(
      (screen.getByRole('button', { name: '모델 목록 새로고침' }) as HTMLButtonElement).disabled
    ).toBe(false))
    await user.click(screen.getByRole('button', { name: '모델 목록 새로고침' }))
    await waitFor(() => expect(window.api.nvidia.models.refresh).toHaveBeenCalledTimes(1))
    expect(screen.getByText('2개 모델을 확인했습니다.')).not.toBeNull()
    expect(screen.getByRole('button', { name: '직접 입력' })).not.toBeNull()
    expect(document.body.textContent).not.toMatch(/Nemotron|무료.*영구|free forever/i)
  })

  it('warns before the low-cost probe, reports neutral states, and executes no Aiso tool', async () => {
    const user = userEvent.setup()
    window.api.nvidia.credential.status = vi.fn().mockResolvedValue({
      encryptionAvailable: true,
      hasStoredCredential: true,
      matchesCurrentBinding: true,
      usableForCurrentBinding: true
    })
    const nvidiaSettings = {
      ...DEFAULT_SETTINGS,
      activeLlmProvider: 'nvidia' as const,
      nvidiaModel: 'model/a',
      chatWebSearch: false
    }
    render(<>
      <SettingsView settings={nvidiaSettings} backend={READY_BACKEND} health={null} onSave={vi.fn().mockResolvedValue(true)} active />
      <ConfirmHost />
    </>)

    await waitFor(() => expect(
      (screen.getByRole('button', { name: '기능 검사' }) as HTMLButtonElement).disabled
    ).toBe(false))
    await user.click(screen.getByRole('button', { name: '기능 검사' }))
    expect(window.api.nvidia.capabilities.probe).not.toHaveBeenCalled()
    expect(screen.getByText(/토큰·할당량 또는 비용이 발생할 수 있습니다/)).not.toBeNull()
    expect(screen.getByText(/실제 도구는 실행하지 않습니다/)).not.toBeNull()
    await user.click(screen.getByRole('button', { name: '검사 실행' }))
    await waitFor(() => expect(window.api.nvidia.capabilities.probe).toHaveBeenCalledTimes(1))
    expect(screen.getByText(/채팅: 지원 확인/)).not.toBeNull()
    expect(screen.getByText(/도구 호출: 지원 확인/)).not.toBeNull()
    expect((screen.getByRole('button', { name: '검사 결과 초기화' }) as HTMLButtonElement).disabled)
      .toBe(false)
  })

  it('loads the registry-backed tool catalog in its own settings tab', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({
        tools: [
          {
            name: 'list_dir',
            description: '작업 폴더의 파일과 하위 폴더를 확인합니다.',
            category: 'files',
            parameters: [{ name: 'path', description: '작업 폴더 기준 경로' }],
            mutates: false,
            approval: { manual: true, read: false, auto: false },
            availability: 'workspace',
            requirements: ['작업 폴더 선택']
          },
          {
            name: 'generate_image',
            description: '등록된 ComfyUI 모델로 이미지를 생성합니다.',
            category: 'image',
            parameters: [{ name: 'prompt', description: '생성 프롬프트' }],
            mutates: true,
            approval: { manual: true, read: true, auto: false },
            availability: 'image',
            requirements: ['명시적 이미지 생성 요청', 'ComfyUI 연결', '등록 모델 준비']
          }
        ]
      })
    }))

    render(<SettingsView settings={DEFAULT_SETTINGS} backend={READY_BACKEND} health={null} onSave={vi.fn().mockResolvedValue(true)} active />)
    await user.click(screen.getByRole('button', { name: '도구' }))

    expect(await screen.findByText('폴더 목록')).not.toBeNull()
    expect(screen.queryByRole('button', { name: '툴 목록' })).toBeNull()
    expect(screen.getByText('작업 폴더 선택')).not.toBeNull()
    expect(screen.getByText('이미지 요청 시')).not.toBeNull()
    expect(screen.getByText('generate_image')).not.toBeNull()

    await user.click(screen.getByRole('button', { name: '이미지 생성' }))
    expect(screen.queryByText('폴더 목록')).toBeNull()
    expect(screen.getByText('generate_image')).not.toBeNull()

    const details = screen.getByText('세부 정보').closest('details')
    expect(details).not.toBeNull()
    expect(details?.open).toBe(false)
    await user.click(screen.getByText('세부 정보'))
    expect(details?.open).toBe(true)
  })

  it('keeps provider tool policies independent and disables unsupported NVIDIA tools', async () => {
    const user = userEvent.setup()
    const onSave = vi.fn().mockResolvedValue(true)
    const programmingTools = [
      ['write_code_file', '프로젝트 코드 파일을 작성합니다.'],
      ['edit_code_file', '프로젝트 코드 파일을 편집합니다.'],
      ['multi_edit_code_file', '여러 코드 파일을 편집합니다.'],
      ['run_web', '웹 프로젝트를 실행해 검증합니다.'],
      ['run_code', '코드를 실행해 검증합니다.'],
      ['run_command', '허용된 명령을 실행합니다.']
    ].map(([name, description]) => ({
      name,
      description,
      category: 'programming',
      parameters: [],
      mutates: true,
      approval: { manual: true, read: true, auto: true },
      availability: 'workspace',
      requirements: ['작업 폴더 선택']
    }))
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({
        tools: [
          ...programmingTools,
          {
            name: 'web_search',
            description: '인터넷 검색 결과를 조회합니다.',
            category: 'research',
            parameters: [],
            mutates: false,
            approval: { manual: false, read: false, auto: false },
            availability: 'always',
            requirements: []
          }
        ]
      })
    }))

    render(
      <SettingsView
        settings={DEFAULT_SETTINGS}
        backend={READY_BACKEND}
        health={null}
        onSave={onSave}
        active
      />
    )
    await user.click(screen.getByRole('button', { name: '도구' }))
    expect(await screen.findByText('코드 파일 작성')).not.toBeNull()

    const localProgramming = screen.getByRole('checkbox', {
      name: '로컬 모델 프로젝트 프로그래밍'
    }) as HTMLInputElement
    expect(localProgramming.checked).toBe(false)
    await user.click(localProgramming)
    expect(localProgramming.checked).toBe(true)

    await user.click(screen.getByRole('button', { name: 'NVIDIA' }))
    const nvidiaProgramming = screen.getByRole('checkbox', {
      name: 'NVIDIA 프로젝트 프로그래밍'
    }) as HTMLInputElement
    expect(nvidiaProgramming.checked).toBe(false)

    const nvidiaCommand = screen.getByRole('checkbox', { name: '명령 실행 사용' }) as HTMLInputElement
    await user.click(nvidiaCommand)
    expect(nvidiaCommand.checked).toBe(true)

    const unsupportedSearch = screen.getByRole('checkbox', { name: '웹 검색 사용' }) as HTMLInputElement
    expect(unsupportedSearch.disabled).toBe(true)
    expect(screen.getByText('현재 NVIDIA Agent 미지원')).not.toBeNull()

    await user.click(screen.getByRole('button', { name: '저장' }))
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1))
    const saved = onSave.mock.calls[0]?.[0]
    expect(saved.agentToolPolicy.ollama).toEqual(expect.arrayContaining([
      'write_code_file',
      'edit_code_file',
      'multi_edit_code_file',
      'run_web',
      'run_code',
      'run_command'
    ]))
    expect(saved.agentToolPolicy.nvidia).toContain('run_command')
    expect(saved.agentToolPolicy.nvidia).not.toContain('write_code_file')
    expect(saved.agentToolPolicy.nvidia).not.toContain('web_search')
  })

  it('preserves an unsaved ComfyUI value when unrelated external settings change', async () => {
    const user = userEvent.setup()
    const onSave = vi.fn().mockResolvedValue(true)
    const { rerender } = render(
      <SettingsView settings={DEFAULT_SETTINGS} backend={READY_BACKEND} health={null} onSave={onSave} active={false} />
    )

    await user.click(screen.getByRole('button', { name: 'ComfyUI' }))
    const baseUrl = screen.getByDisplayValue(DEFAULT_SETTINGS.comfyBaseUrl) as HTMLInputElement
    await user.clear(baseUrl)
    await user.type(baseUrl, 'http://127.0.0.1:8288')

    rerender(
      <SettingsView
        settings={{ ...DEFAULT_SETTINGS, devMode: true }}
        backend={READY_BACKEND}
        health={null}
        onSave={onSave}
        active={false}
      />
    )

    expect((screen.getByDisplayValue('http://127.0.0.1:8288') as HTMLInputElement).value)
      .toBe('http://127.0.0.1:8288')
  })

  it('accepts later external settings after saving dirty fields for a provider transition', async () => {
    const user = userEvent.setup()
    let persisted = { ...DEFAULT_SETTINGS }
    const onSave = vi.fn().mockImplementation(async (next) => {
      persisted = { ...next }
      return true
    })
    window.api.discord.setLlmProvider = vi.fn().mockImplementation(async () => ({
      ...persisted,
      discordLlmProvider: 'nvidia' as const
    }))
    const { rerender } = render(
      <SettingsView settings={persisted} backend={READY_BACKEND} health={null} onSave={onSave} active />
    )

    await user.click(screen.getByRole('button', { name: 'ComfyUI' }))
    const baseUrl = screen.getByDisplayValue(DEFAULT_SETTINGS.comfyBaseUrl) as HTMLInputElement
    await user.clear(baseUrl)
    await user.type(baseUrl, 'http://127.0.0.1:8288')
    await user.click(screen.getByRole('button', { name: '디스코드' }))
    await user.click(screen.getByRole('button', { name: 'NVIDIA 실험' }))

    await waitFor(() => expect(window.api.discord.setLlmProvider).toHaveBeenCalledWith('nvidia'))
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      comfyBaseUrl: 'http://127.0.0.1:8288'
    }))

    persisted = {
      ...persisted,
      comfyBaseUrl: 'http://127.0.0.1:8388',
      discordLlmProvider: 'nvidia'
    }
    rerender(
      <SettingsView settings={persisted} backend={READY_BACKEND} health={null} onSave={onSave} active />
    )
    await user.click(screen.getByRole('button', { name: 'ComfyUI' }))
    expect((screen.getByDisplayValue('http://127.0.0.1:8388') as HTMLInputElement).value)
      .toBe('http://127.0.0.1:8388')
  })

  it('clears successfully saved dirty fields even when the provider IPC fails', async () => {
    const user = userEvent.setup()
    const onSave = vi.fn().mockResolvedValue(true)
    window.api.discord.setLlmProvider = vi.fn().mockRejectedValue(new Error('provider failed'))
    const { rerender } = render(
      <SettingsView settings={DEFAULT_SETTINGS} backend={READY_BACKEND} health={null} onSave={onSave} active />
    )

    await user.click(screen.getByRole('button', { name: 'ComfyUI' }))
    const baseUrl = screen.getByDisplayValue(DEFAULT_SETTINGS.comfyBaseUrl) as HTMLInputElement
    await user.clear(baseUrl)
    await user.type(baseUrl, 'http://127.0.0.1:8288')
    await user.click(screen.getByRole('button', { name: '디스코드' }))
    await user.click(screen.getByRole('button', { name: 'NVIDIA 실험' }))
    await waitFor(() => expect(screen.getByText(/provider failed/)).toBeTruthy())

    rerender(
      <SettingsView
        settings={{ ...DEFAULT_SETTINGS, comfyBaseUrl: 'http://127.0.0.1:8388' }}
        backend={READY_BACKEND}
        health={null}
        onSave={onSave}
        active
      />
    )
    await user.click(screen.getByRole('button', { name: 'ComfyUI' }))
    expect((screen.getByDisplayValue('http://127.0.0.1:8388') as HTMLInputElement).value)
      .toBe('http://127.0.0.1:8388')
  })

  it('does not show a successful save when persistence fails', async () => {
    const onSave = vi.fn().mockResolvedValue(false)
    render(<SettingsView settings={DEFAULT_SETTINGS} backend={READY_BACKEND} health={null} onSave={onSave} active={false} />)

    const model = screen.getByDisplayValue(DEFAULT_SETTINGS.model)
    fireEvent.change(model, { target: { value: 'other-model:latest' } })
    fireEvent.click(screen.getByRole('button', { name: '저장' }))

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('alert').textContent).toContain('저장하지 못했습니다')
    expect(screen.queryByText('저장됨')).toBeNull()
  })

  it('keeps an in-progress ComfyUI model form while switching setting sections', async () => {
    const user = userEvent.setup()
    render(
      <SettingsView
        settings={{ ...DEFAULT_SETTINGS, comfyInstallPath: 'D:\\ComfyUI' }}
        backend={READY_BACKEND}
        health={null}
        onSave={vi.fn().mockResolvedValue(true)}
        active={false}
      />
    )

    await user.click(screen.getByRole('button', { name: 'ComfyUI' }))
    await user.click(await screen.findByRole('button', { name: '새 모델 연결' }))
    await user.type(screen.getByLabelText('모델 이름'), 'keep-this-model')

    await user.click(screen.getByRole('button', { name: 'LLM' }))
    await user.click(screen.getByRole('button', { name: 'ComfyUI' }))

    expect((screen.getByDisplayValue('keep-this-model') as HTMLInputElement).value).toBe('keep-this-model')
  })

  it('saves the ComfyUI model-selection mode independently of model registration', async () => {
    const user = userEvent.setup()
    const onSave = vi.fn().mockResolvedValue(true)
    render(<SettingsView settings={DEFAULT_SETTINGS} backend={READY_BACKEND} health={null} onSave={onSave} active={false} />)

    await user.click(screen.getByRole('button', { name: 'ComfyUI' }))
    await user.click(screen.getByRole('button', { name: '수동' }))
    await user.click(screen.getByRole('button', { name: '저장' }))

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({ comfyModelSelectionMode: 'manual' })
      )
    )
  })
})
