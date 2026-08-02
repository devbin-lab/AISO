import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ComfyGeneratedImage } from '../../../shared/agent'
import GeneratedImage from './GeneratedImage'

const image: ComfyGeneratedImage = {
  jobId: 'job-1',
  filename: 'result.png',
  subfolder: 'Aiso',
  storageType: 'output',
  baseUrl: 'http://127.0.0.1:8188',
  profileId: 'flux2',
  profileName: 'FLUX.2 Klein 4B',
  modelName: 'flux2-klein.safetensors',
  selectionReason: '태그 일치',
  prompt: 'A cinematic city at night.',
  negativePrompt: '',
  originalPrompt: 'A cinematic city at night.',
  originalNegativePrompt: 'blurry, watermark',
  effectivePrompt: 'A cinematic city at night. The main subject is clearly focused.',
  effectiveNegativePrompt: '',
  promptPolicy: {
    id: 'flux-positive-constraints-v1',
    label: 'FLUX 제외 요소 긍정 변환',
    description: '제외 의도를 긍정적인 시각 조건으로 바꿉니다.',
    addedPositive: ['The main subject is clearly focused.'],
    addedNegative: []
  },
  pipeline: {
    source: 'aiso-built-in',
    nodeCount: 2,
    vaeDecode: true,
    negativeMode: 'positive-constraints',
    scaleProcess: false,
    processingNodes: []
  },
  workflow: {
    '1': { class_type: 'UNETLoader', inputs: { unet_name: 'flux2-klein.safetensors' } },
    '2': { class_type: 'VAEDecode', inputs: { samples: ['1', 0], vae: ['1', 1] } }
  },
  seed: '7',
  width: 1024,
  height: 1024,
  steps: 4,
  cfg: 1,
  sampler: 'euler',
  scheduler: 'Flux2Scheduler'
}

describe('GeneratedImage', () => {
  it('shows the truthful workflow pipeline and prompt conversion trace', () => {
    render(<GeneratedImage image={image} backendPort={null} />)
    expect(screen.getByText('Aiso 계열별 기본 워크플로')).not.toBeNull()
    expect(screen.getByText('결과 경로 2개 노드')).not.toBeNull()
    expect(screen.getByText('VAE 디코드 경로 포함')).not.toBeNull()
    expect(screen.getByText('제외 요소 긍정 변환')).not.toBeNull()
    expect(screen.getByText('스케일 처리 노드 경로 미포함')).not.toBeNull()

    fireEvent.click(screen.getByText(/실제 ComfyUI 노드 워크플로 보기/))
    expect(screen.getByText('FLUX 제외 요소 긍정 변환')).not.toBeNull()
    expect(screen.getByText('blurry, watermark')).not.toBeNull()
    expect(screen.getAllByText(/clearly focused/).length).toBeGreaterThan(0)
  })
})
