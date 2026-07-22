import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  ComfyModelImportProgress,
  ComfyModelImportResult,
  ComfyModelProfile,
  ComfyWorkflowTemplate
} from '../../../shared/comfy-model'
import ComfyModelManager from './ComfyModelManager'

const asset = {
  id: 'asset-1',
  kind: 'checkpoint' as const,
  slot: 'checkpoint' as const,
  agentFamilies: ['sdxl'] as const,
  fileName: 'example.safetensors',
  comfyName: 'example.safetensors',
  relativePath: 'checkpoints/example.safetensors',
  size: 1_024,
  sha256: 'a'.repeat(64),
  importedAt: 1
}

function userWorkflow(): ComfyWorkflowTemplate {
  return {
    schemaVersion: 1,
    id: 'user.api.txt2img.v1',
    sourceFileName: 'my-workflow-api.json',
    sha256: 'b'.repeat(64),
    graph: {
      '1': {
        class_type: 'CheckpointLoaderSimple',
        inputs: { ckpt_name: 'example.safetensors' }
      }
    },
    bindings: {
      positivePrompt: [{ nodeId: '2', input: 'text' }],
      negativePrompt: [],
      seed: [],
      width: [{ nodeId: '3', input: 'width' }],
      height: [{ nodeId: '3', input: 'height' }],
      steps: [],
      cfg: [],
      sampler: [{ nodeId: '4', input: 'sampler_name' }],
      scheduler: [],
      filenamePrefix: [{ nodeId: '5', input: 'filename_prefix' }]
    },
    assetBindings: [{
      nodeId: '1',
      input: 'ckpt_name',
      assetId: asset.id,
      sha256: asset.sha256,
      relativePath: asset.relativePath,
      comfyName: asset.comfyName
    }],
    importedAt: 1
  }
}

function profileWithUserWorkflow(): ComfyModelProfile {
  return {
    id: 'profile-1',
    name: '테스트 사용자 워크플로',
    family: 'custom',
    capabilities: ['txt2img'],
    tags: [],
    assets: [asset],
    workflowTemplateId: 'user.api.txt2img.v1',
    workflowTemplate: userWorkflow(),
    defaults: {
      width: 1024,
      height: 1024,
      steps: 20,
      cfg: 5,
      sampler: 'euler'
    },
    agentEnabled: false,
    priority: 0,
    createdAt: 1,
    updatedAt: 1
  }
}

interface ApiStubs {
  list: ReturnType<typeof vi.fn>
  update: ReturnType<typeof vi.fn>
  importAssets: ReturnType<typeof vi.fn>
  cancelImport: ReturnType<typeof vi.fn>
  emitProgress: (progress: ComfyModelImportProgress) => void
}

function installApiStub(profiles: ComfyModelProfile[]): ApiStubs {
  let onProgress: ((progress: ComfyModelImportProgress) => void) | undefined
  const list = vi.fn().mockResolvedValue({ profiles })
  const update = vi.fn().mockResolvedValue(profiles[0])
  const importAssets = vi.fn().mockResolvedValue({ canceled: true, imported: [], reused: [] })
  const cancelImport = vi.fn().mockResolvedValue(true)
  Object.defineProperty(window, 'api', {
    configurable: true,
    value: {
      comfy: {
        models: {
          list,
          importAssets,
          cancelImport,
          update,
          importWorkflow: vi.fn().mockResolvedValue({ canceled: true }),
          removeWorkflow: vi.fn(),
          unregister: vi.fn(),
          onImportProgress: vi.fn((callback: (progress: ComfyModelImportProgress) => void) => {
            onProgress = callback
            return () => { onProgress = undefined }
          })
        }
      }
    }
  })
  return {
    list,
    update,
    importAssets,
    cancelImport,
    emitProgress: (progress) => onProgress?.(progress)
  }
}

describe('ComfyModelManager', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('shows exact user-workflow asset bindings and only its declared controls', async () => {
    const user = userEvent.setup()
    const profile = profileWithUserWorkflow()
    const api = installApiStub([profile])
    render(<ComfyModelManager installPath={'D:\\ComfyUI'} />)

    await user.click(await screen.findByRole('button', { name: '설정 편집' }))
    expect(screen.getByText('1.ckpt_name').textContent).toBe('1.ckpt_name')
    expect(screen.getAllByText('example.safetensors')).toHaveLength(2)
    expect(screen.getByText(/Agent 입력: 프롬프트/)).not.toBeNull()

    await user.click(screen.getByText('고급 생성 기본값'))
    const sampler = screen.getByLabelText('Sampler') as HTMLInputElement
    expect(sampler.tagName).toBe('INPUT')
    expect(screen.queryByLabelText('Scheduler')).toBeNull()

    await user.clear(sampler)
    await user.type(sampler, 'custom_sampler')
    await user.click(screen.getByRole('button', { name: '설정 저장' }))

    await waitFor(() => expect(api.update).toHaveBeenCalledWith(
      profile.id,
      expect.objectContaining({
        defaults: expect.objectContaining({ sampler: 'custom_sampler' })
      })
    ))
  })

  it('offers cancellation while a SafeTensors import is in progress', async () => {
    const user = userEvent.setup()
    const api = installApiStub([])
    let finishImport: ((result: ComfyModelImportResult) => void) | undefined
    api.importAssets.mockImplementation(() => new Promise<ComfyModelImportResult>((resolve) => {
      finishImport = resolve
    }))
    render(<ComfyModelManager installPath={'D:\\ComfyUI'} />)

    await user.click(await screen.findByRole('button', { name: '새 모델 연결' }))
    await user.type(screen.getByLabelText('모델 이름'), 'large-model')
    await user.click(screen.getByRole('button', { name: '파일 선택 및 연결' }))
    await waitFor(() => expect(api.importAssets).toHaveBeenCalledTimes(1))
    api.emitProgress({
      operationId: 'test-operation',
      phase: 'hashing',
      fileName: 'large-model.safetensors',
      completedBytes: 512,
      totalBytes: 1_024
    })

    await user.click(await screen.findByRole('button', { name: '가져오기 취소' }))
    expect(api.cancelImport).toHaveBeenCalledWith(expect.any(String))

    finishImport?.({ canceled: true, imported: [], reused: [] })
    await waitFor(() => expect(screen.getByText('파일 연결을 취소했습니다.')).not.toBeNull())
  })
})
