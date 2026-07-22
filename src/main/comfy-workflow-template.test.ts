import assert from 'node:assert/strict'
import test from 'node:test'
import {
  bindComfyWorkflowTemplateAssets,
  parseComfyWorkflowTemplate,
  parseStoredComfyWorkflowTemplate
} from './comfy-workflow-template.ts'
import {
  getComfyAgentReadiness,
  getComfyGenerationDefaults,
  type ComfyModelProfile
} from '../shared/comfy-model.ts'

function apiWorkflow(): Record<string, unknown> {
  return {
    '1': {
      class_type: 'CheckpointLoaderSimple',
      inputs: { ckpt_name: 'future-model.safetensors' }
    },
    '2': {
      class_type: 'CLIPTextEncode',
      inputs: { text: 'AISO_PROMPT', clip: ['1', 1] },
      _meta: { title: 'Positive Prompt' }
    },
    '3': {
      class_type: 'CLIPTextEncode',
      inputs: { text: 'AISO_NEGATIVE_PROMPT', clip: ['1', 1] },
      _meta: { title: 'Negative Prompt' }
    },
    '4': {
      class_type: 'EmptyLatentImage',
      inputs: { width: 832, height: 1216, batch_size: 1 }
    },
    '5': {
      class_type: 'KSampler',
      inputs: {
        model: ['1', 0], seed: 7, steps: 31, cfg: 4.5,
        sampler_name: 'dpmpp_2m', scheduler: 'karras',
        positive: ['2', 0], negative: ['3', 0], latent_image: ['4', 0], denoise: 1
      }
    },
    '6': { class_type: 'VAEDecode', inputs: { samples: ['5', 0], vae: ['1', 2] } },
    '7': { class_type: 'SaveImage', inputs: { images: ['6', 0], filename_prefix: 'ComfyUI' } }
  }
}

test('ComfyUI API 워크플로에서 허용 입력과 기본값을 자동 바인딩한다', () => {
  const parsed = parseComfyWorkflowTemplate(apiWorkflow(), 'future-model-api.json', 123)
  assert.equal(parsed.template.sourceFileName, 'future-model-api.json')
  assert.match(parsed.template.id, /^user\.[a-f0-9]{20}\.txt2img\.v1$/)
  assert.deepEqual(parsed.template.bindings.positivePrompt, [{ nodeId: '2', input: 'text' }])
  assert.deepEqual(parsed.template.bindings.negativePrompt, [{ nodeId: '3', input: 'text' }])
  assert.deepEqual(parsed.template.bindings.width, [{ nodeId: '4', input: 'width' }])
  assert.deepEqual(parsed.template.bindings.filenamePrefix, [{ nodeId: '7', input: 'filename_prefix' }])
  assert.deepEqual(parsed.suggestedDefaults, {
    width: 832,
    height: 1216,
    steps: 31,
    cfg: 4.5,
    sampler: 'dpmpp_2m',
    scheduler: 'karras'
  })
  assert.deepEqual(parseStoredComfyWorkflowTemplate(parsed.template), parsed.template)
})

test('UI 형식, 출력 없는 그래프, 끊어진 연결은 등록하지 않는다', () => {
  assert.throws(
    () => parseComfyWorkflowTemplate({ nodes: [] }, 'ui.json'),
    /Save \(API Format\)/
  )
  const noOutput = apiWorkflow()
  delete noOutput['7']
  assert.throws(() => parseComfyWorkflowTemplate(noOutput, 'no-output.json'), /SaveImage/)
  const broken = apiWorkflow()
  ;(broken['6'] as { inputs: { samples: unknown } }).inputs.samples = ['999', 0]
  assert.throws(() => parseComfyWorkflowTemplate(broken, 'broken.json'), /존재하지 않는/)
})

test('저장된 워크플로 그래프나 해시가 바뀌면 무효화한다', () => {
  const parsed = parseComfyWorkflowTemplate(apiWorkflow(), 'safe.json', 123).template
  const changed = structuredClone(parsed)
  changed.graph['5'].inputs.steps = 99
  assert.equal(parseStoredComfyWorkflowTemplate(changed), null)
  assert.equal(parseStoredComfyWorkflowTemplate({ ...parsed, sha256: '0'.repeat(64) }), null)
})

test('사용자 워크플로의 모델 로더는 등록 자산 계약과 정확히 연결되어야 한다', () => {
  const imported = parseComfyWorkflowTemplate(apiWorkflow(), 'contract.json', 123).template
  const asset = {
    id: 'registered-model',
    kind: 'custom' as const,
    agentFamilies: [],
    fileName: 'future-model.safetensors',
    comfyName: 'future-model.safetensors',
    relativePath: 'future_models/future-model.safetensors',
    size: 1,
    sha256: 'b'.repeat(64),
    importedAt: 1
  }
  const bound = bindComfyWorkflowTemplateAssets(imported, [asset])
  assert.deepEqual(bound.assetBindings, [{
    nodeId: '1',
    input: 'ckpt_name',
    assetId: 'registered-model',
    sha256: 'b'.repeat(64),
    relativePath: 'future_models/future-model.safetensors',
    comfyName: 'future-model.safetensors'
  }])
  assert.equal(parseStoredComfyWorkflowTemplate(bound)?.id, bound.id)

  const unbound = bindComfyWorkflowTemplateAssets(imported, [{ ...asset, comfyName: 'other.safetensors' }])
  assert.equal(unbound.assetBindings.length, 0)
  const tampered = structuredClone(bound)
  tampered.assetBindings[0].assetId = 'different-asset'
  assert.equal(parseStoredComfyWorkflowTemplate(tampered), null)
})

test('Agent 사용자 워크플로는 과도한 batch 출력 구성을 등록하지 않는다', () => {
  const unsafe = apiWorkflow()
  ;(unsafe['4'] as { inputs: { batch_size: number } }).inputs.batch_size = 2
  assert.throws(() => parseComfyWorkflowTemplate(unsafe, 'unsafe.json'), /batch_size/)
})

test('직접 연결 모델도 검증된 사용자 워크플로가 있을 때만 Agent 준비 상태가 된다', () => {
  const imported = parseComfyWorkflowTemplate(apiWorkflow(), 'future.json', 123).template
  const assets = [{
    id: 'future-asset',
    kind: 'custom' as const,
    agentFamilies: [],
    fileName: 'future-model.safetensors',
    comfyName: 'future-model.safetensors',
    relativePath: 'future_models/future-model.safetensors',
    size: 1,
    sha256: 'a'.repeat(64),
    importedAt: 1
  }]
  const template = bindComfyWorkflowTemplateAssets(imported, assets)
  const profile: ComfyModelProfile = {
    id: 'future-profile',
    name: 'Future model',
    family: 'custom',
    capabilities: ['txt2img'],
    tags: [],
    assets,
    workflowTemplateId: template.id,
    workflowTemplate: template,
    defaults: getComfyGenerationDefaults('custom'),
    agentEnabled: false,
    priority: 0,
    createdAt: 1,
    updatedAt: 1
  }
  const readiness = getComfyAgentReadiness(profile)
  assert.equal(readiness.ready, true)
  assert.match(readiness.detail, /사용자 워크플로 연결됨/)
})
