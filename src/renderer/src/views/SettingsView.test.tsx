import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { BackendInfo } from '../../../shared/backend'
import { DEFAULT_SETTINGS } from '../../../shared/settings'
import SettingsView from './SettingsView'

const READY_BACKEND: BackendInfo = { state: 'ready', port: 8123 }

function installApiStub(): void {
  Object.defineProperty(window, 'api', {
    configurable: true,
    value: {
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
        scheduleRemove: vi.fn().mockResolvedValue(undefined)
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
            mutates: false,
            approval: { manual: true, read: false, auto: false },
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
