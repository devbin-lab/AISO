import assert from 'node:assert/strict'
import test from 'node:test'
import {
  SAFE_TENSORS_HEADER_MAX_BYTES,
  inferComfyModelAssetFromHeader,
  inferComfyModelAssetFromSafeTensors,
  inferComfyModelFamilyFromHeader,
  inferComfyModelFamilyFromSafeTensors,
  parseSafeTensorsHeader
} from './comfy-model-analysis.ts'
import {
  getEffectiveComfyQualityMode,
  getComfyAgentReadiness,
  getComfyGenerationDefaults,
  supportsComfyQualityRefinement,
  type ComfyAssetSlot,
  type ComfyModelProfile
} from '../shared/comfy-model.ts'

function headerBuffer(value: Record<string, unknown>): Buffer {
  const header = Buffer.from(JSON.stringify(value), 'utf8')
  const prefix = Buffer.alloc(8)
  prefix.writeBigUInt64LE(BigInt(header.length), 0)
  return Buffer.concat([prefix, header])
}

test('quality refinement is limited to the automatic SDXL workflow', () => {
  const automaticSdxl = { family: 'sdxl' as const, qualityMode: 'refine' as const }
  const flux = { family: 'flux2' as const, qualityMode: 'refine' as const }
  const userWorkflow = {
    family: 'sdxl' as const,
    qualityMode: 'refine' as const,
    workflowTemplate: {} as NonNullable<ComfyModelProfile['workflowTemplate']>
  }

  assert.equal(supportsComfyQualityRefinement(automaticSdxl), true)
  assert.equal(getEffectiveComfyQualityMode(automaticSdxl), 'refine')
  assert.equal(supportsComfyQualityRefinement(flux), false)
  assert.equal(getEffectiveComfyQualityMode(flux), 'base')
  assert.equal(supportsComfyQualityRefinement(userWorkflow), false)
  assert.equal(getEffectiveComfyQualityMode(userWorkflow), 'base')
})

test('모델 메타데이터에서 SDXL을 내부적으로 판별한다', () => {
  const header = parseSafeTensorsHeader(headerBuffer({
    __metadata__: {
      'modelspec.architecture': 'stable-diffusion-xl-v1-base',
      'modelspec.thumbnail': 'x'.repeat(5_000)
    },
    'conditioner.embedders.1.model.transformer.text_model.embeddings.token_embedding.weight': {
      dtype: 'F16',
      shape: [1],
      data_offsets: [0, 2]
    }
  }))
  assert.equal(header?.metadata['modelspec.architecture'], 'stable-diffusion-xl-v1-base')
  assert.equal(header?.metadata['modelspec.thumbnail'], undefined)
  assert.equal(inferComfyModelFamilyFromHeader(header), 'sdxl')
})

test('메타데이터가 없어도 명확한 텐서 구조만 보조 근거로 사용한다', () => {
  const header = parseSafeTensorsHeader(headerBuffer({
    'cond_stage_model.transformer.text_model.embeddings.token_embedding.weight': {
      dtype: 'F16',
      shape: [1],
      data_offsets: [0, 2]
    }
  }))
  assert.equal(inferComfyModelFamilyFromHeader(header), 'sd15')
})

test('명시된 분리형 FLUX 메타데이터만 내부 워크플로 후보로 분류한다', () => {
  const flux1 = parseSafeTensorsHeader(headerBuffer({
    __metadata__: { architecture: 'flux.1-dev' },
    'double_blocks.0.img_attn.qkv.weight': { dtype: 'F16', shape: [1], data_offsets: [0, 2] },
    'single_blocks.0.linear1.weight': { dtype: 'F16', shape: [1], data_offsets: [2, 4] }
  }))
  const flux2 = parseSafeTensorsHeader(headerBuffer({
    __metadata__: { architecture: 'flux.2' },
    'some.tensor': { dtype: 'F16', shape: [1], data_offsets: [0, 2] }
  }))
  assert.equal(inferComfyModelFamilyFromHeader(flux1), 'flux1')
  assert.equal(inferComfyModelFamilyFromHeader(flux2), 'flux2')
})

test('통합 SD 체크포인트는 UNet과 해당 텍스트 인코더 구조를 모두 가질 때만 연결한다', () => {
  const sdxl = parseSafeTensorsHeader(headerBuffer({
    __metadata__: { architecture: 'stable-diffusion-xl-v1-base' },
    'model.diffusion_model.input_blocks.0.0.weight': { dtype: 'F16', shape: [1], data_offsets: [0, 2] },
    'conditioner.embedders.0.model.text_model.embeddings.token_embedding.weight': { dtype: 'F16', shape: [1], data_offsets: [2, 4] },
    'conditioner.embedders.1.model.transformer.text_model.embeddings.token_embedding.weight': { dtype: 'F16', shape: [1], data_offsets: [4, 6] }
  }))
  const sd15 = parseSafeTensorsHeader(headerBuffer({
    'model.diffusion_model.input_blocks.0.0.weight': { dtype: 'F16', shape: [1], data_offsets: [0, 2] },
    'cond_stage_model.transformer.text_model.embeddings.token_embedding.weight': { dtype: 'F16', shape: [1], data_offsets: [2, 4] }
  }))
  const componentOnly = parseSafeTensorsHeader(headerBuffer({
    __metadata__: { architecture: 'stable-diffusion-xl-v1-base' },
    'conditioner.embedders.0.model.text_model.embeddings.token_embedding.weight': { dtype: 'F16', shape: [1], data_offsets: [0, 2] },
    'conditioner.embedders.1.model.transformer.text_model.embeddings.token_embedding.weight': { dtype: 'F16', shape: [1], data_offsets: [2, 4] }
  }))
  assert.deepEqual(inferComfyModelAssetFromHeader(sdxl), { kind: 'checkpoint', slot: 'checkpoint', agentFamilies: ['sdxl'] })
  assert.deepEqual(inferComfyModelAssetFromHeader(sd15), { kind: 'checkpoint', slot: 'checkpoint', agentFamilies: ['sd15'] })
  assert.equal(inferComfyModelAssetFromHeader(componentOnly), null)
})

test('FLUX.1 핵심 구조만 확산 모델로 분류하고 유사 구조는 추측하지 않는다', () => {
  const flux1 = parseSafeTensorsHeader(headerBuffer({
    __metadata__: { architecture: 'flux.1-dev' },
    'img_in.weight': { dtype: 'F16', shape: [1], data_offsets: [0, 2] },
    'double_blocks.0.img_attn.norm.key_norm.weight': { dtype: 'F16', shape: [1], data_offsets: [2, 4] },
    'single_blocks.0.linear1.weight': { dtype: 'F16', shape: [1], data_offsets: [4, 6] },
    'final_layer.linear.weight': { dtype: 'F16', shape: [1], data_offsets: [6, 8] }
  }))
  const similar = parseSafeTensorsHeader(headerBuffer({
    'double_blocks.0.any.weight': { dtype: 'F16', shape: [1], data_offsets: [0, 2] },
    'single_blocks.0.any.weight': { dtype: 'F16', shape: [1], data_offsets: [2, 4] }
  }))
  assert.deepEqual(inferComfyModelAssetFromHeader(flux1), { kind: 'diffusion_model', slot: 'diffusion_model', agentFamilies: ['flux1'] })
  assert.equal(inferComfyModelAssetFromHeader(similar), null)
})

test('확인된 FLUX.2 확산 구조는 FLUX.2 Agent 계약 후보로 표시한다', () => {
  const flux2 = parseSafeTensorsHeader(headerBuffer({
    __metadata__: { architecture: 'flux.2' },
    'double_stream_modulation_img.lin.weight': { dtype: 'F16', shape: [1], data_offsets: [0, 2] },
    'img_in.weight': { dtype: 'F16', shape: [1], data_offsets: [2, 4] }
  }))
  assert.deepEqual(inferComfyModelAssetFromHeader(flux2), {
    kind: 'diffusion_model', slot: 'diffusion_model', agentFamilies: ['flux2']
  })
})

test('텍스트 인코더 슬롯은 CLIP-L과 T5XXL 크기까지 확인해 연결한다', () => {
  const clipL = parseSafeTensorsHeader(headerBuffer({
    'text_model.embeddings.token_embedding.weight': { dtype: 'F16', shape: [49408, 768], data_offsets: [0, 2] },
    'text_model.encoder.layers.11.mlp.fc1.weight': { dtype: 'F16', shape: [3072, 768], data_offsets: [2, 4] }
  }))
  const clipG = parseSafeTensorsHeader(headerBuffer({
    'text_model.embeddings.token_embedding.weight': { dtype: 'F16', shape: [49408, 768], data_offsets: [0, 2] },
    'text_model.encoder.layers.11.mlp.fc1.weight': { dtype: 'F16', shape: [3072, 768], data_offsets: [2, 4] },
    'text_model.encoder.layers.30.mlp.fc1.weight': { dtype: 'F16', shape: [3072, 768], data_offsets: [4, 6] }
  }))
  const t5xxl = parseSafeTensorsHeader(headerBuffer({
    'shared.weight': { dtype: 'F16', shape: [32128, 4096], data_offsets: [0, 2] },
    'encoder.block.23.layer.1.DenseReluDense.wi_1.weight': { dtype: 'F16', shape: [10240, 4096], data_offsets: [2, 4] }
  }))
  const smallerT5 = parseSafeTensorsHeader(headerBuffer({
    'shared.weight': { dtype: 'F16', shape: [32128, 2048], data_offsets: [0, 2] },
    'encoder.block.23.layer.1.DenseReluDense.wi_1.weight': { dtype: 'F16', shape: [5120, 2048], data_offsets: [2, 4] }
  }))
  assert.deepEqual(inferComfyModelAssetFromHeader(clipL), { kind: 'text_encoder', slot: 'clip_l', agentFamilies: ['flux1'] })
  assert.equal(inferComfyModelAssetFromHeader(clipG), null)
  assert.deepEqual(inferComfyModelAssetFromHeader(t5xxl), { kind: 'text_encoder', slot: 't5xxl', agentFamilies: ['flux1'] })
  assert.equal(inferComfyModelAssetFromHeader(smallerT5), null)
})

test('VAE는 FLUX.1/FLUX.2의 서로 다른 latent 구조를 구분해 Agent 호환으로 표시한다', () => {
  const vae = (latentChannels: number) => parseSafeTensorsHeader(headerBuffer({
    'encoder.conv_in.weight': { dtype: 'F16', shape: [128, 3, 3, 3], data_offsets: [0, 2] },
    'encoder.conv_out.weight': { dtype: 'F16', shape: [latentChannels * 2, 128, 3, 3], data_offsets: [2, 4] },
    'decoder.conv_in.weight': { dtype: 'F16', shape: [128, latentChannels, 3, 3], data_offsets: [4, 6] },
    'decoder.conv_out.weight': { dtype: 'F16', shape: [3, 128, 3, 3], data_offsets: [6, 8] },
    'quant_conv.weight': { dtype: 'F16', shape: [1], data_offsets: [8, 10] },
    'post_quant_conv.weight': { dtype: 'F16', shape: [1], data_offsets: [10, 12] }
  }))
  assert.deepEqual(inferComfyModelAssetFromHeader(vae(4)), { kind: 'vae', slot: 'vae', agentFamilies: [] })
  assert.deepEqual(inferComfyModelAssetFromHeader(vae(16)), { kind: 'vae', slot: 'vae', agentFamilies: ['flux1'] })
  assert.deepEqual(inferComfyModelAssetFromHeader(vae(32)), { kind: 'vae', slot: 'vae', agentFamilies: ['flux2'] })
})

test('Qwen 3 4B 텍스트 인코더만 FLUX.2 자동 워크플로 슬롯으로 연결한다', () => {
  const qwen3FourB = parseSafeTensorsHeader(headerBuffer({
    'model.embed_tokens.weight': { dtype: 'F16', shape: [151936, 2560], data_offsets: [0, 2] },
    'model.layers.0.self_attn.q_proj.weight': { dtype: 'F16', shape: [2560, 2560], data_offsets: [2, 4] },
    'model.layers.35.mlp.down_proj.weight': { dtype: 'F16', shape: [2560, 9728], data_offsets: [4, 6] },
    'model.norm.weight': { dtype: 'F16', shape: [2560], data_offsets: [6, 8] }
  }))
  const genericQwen = parseSafeTensorsHeader(headerBuffer({
    'model.embed_tokens.weight': { dtype: 'F16', shape: [151936, 2048], data_offsets: [0, 2] },
    'model.layers.0.self_attn.q_proj.weight': { dtype: 'F16', shape: [2048, 2048], data_offsets: [2, 4] },
    'model.layers.35.mlp.down_proj.weight': { dtype: 'F16', shape: [2048, 8192], data_offsets: [4, 6] },
    'model.norm.weight': { dtype: 'F16', shape: [2048], data_offsets: [6, 8] }
  }))
  assert.deepEqual(inferComfyModelAssetFromHeader(qwen3FourB), {
    kind: 'text_encoder', slot: 'qwen3', agentFamilies: ['flux2']
  })
  assert.equal(inferComfyModelAssetFromHeader(genericQwen), null)
})

test('FLUX Agent는 연결된 VAE가 있어도 호환 구조가 확인되지 않으면 켜지지 않는다', () => {
  const profile = (vaeAgentFamilies: string[]): ComfyModelProfile => ({
    id: 'flux-profile',
    name: 'local model',
    family: 'flux1',
    capabilities: ['txt2img'],
    tags: [],
    assets: (['diffusion_model', 'clip_l', 't5xxl', 'vae'] as const).map((slot) => ({
      id: slot,
      kind: slot === 'diffusion_model'
        ? 'diffusion_model'
        : slot === 'vae'
          ? 'vae'
          : 'text_encoder',
      slot: slot as ComfyAssetSlot,
      agentFamilies: slot === 'vae' ? vaeAgentFamilies as ('flux1' | 'sd15' | 'sdxl' | 'flux2' | 'custom')[] : ['flux1'],
      fileName: `${slot}.safetensors`,
      comfyName: `${slot}.safetensors`,
      relativePath: `${slot}/${slot}.safetensors`,
      size: 1,
      sha256: 'a'.repeat(64),
      importedAt: 1
    })),
    workflowTemplateId: 'flux1.txt2img.v1',
    defaults: getComfyGenerationDefaults('flux1'),
    qualityMode: 'base',
    agentEnabled: false,
    priority: 0,
    createdAt: 1,
    updatedAt: 1
  })
  const incompatible = getComfyAgentReadiness(profile([]))
  assert.equal(incompatible.ready, false)
  assert.deepEqual(incompatible.incompatibleSlots, ['vae'])
  assert.equal(getComfyAgentReadiness(profile(['flux1'])).ready, true)
})

test('FLUX.2 Agent는 확산 모델·Qwen 3·FLUX.2 VAE 구성이 완성돼야 켜진다', () => {
  const slots = ['diffusion_model', 'qwen3', 'vae'] as const
  const profile = (vaeFamilies: ('flux1' | 'flux2')[]): ComfyModelProfile => ({
    id: 'flux2-profile',
    name: 'FLUX.2 Klein',
    family: 'flux2',
    capabilities: ['txt2img'],
    tags: [],
    assets: slots.map((slot) => ({
      id: slot,
      kind: slot === 'diffusion_model' ? 'diffusion_model' : slot === 'vae' ? 'vae' : 'text_encoder',
      slot,
      agentFamilies: slot === 'vae' ? vaeFamilies : ['flux2'],
      fileName: `${slot}.safetensors`,
      comfyName: `${slot}.safetensors`,
      relativePath: `${slot}/${slot}.safetensors`,
      size: 1,
      sha256: 'b'.repeat(64),
      importedAt: 1
    })),
    workflowTemplateId: 'flux2.txt2img.v1',
    defaults: getComfyGenerationDefaults('flux2'),
    qualityMode: 'base',
    agentEnabled: false,
    priority: 0,
    createdAt: 1,
    updatedAt: 1
  })
  assert.equal(getComfyAgentReadiness(profile(['flux2'])).ready, true)
  assert.deepEqual(getComfyAgentReadiness(profile(['flux1'])).incompatibleSlots, ['vae'])
  const legacy = profile(['flux2'])
  legacy.assets[0].agentFamilies = []
  assert.equal(getComfyAgentReadiness(legacy).ready, true)
})

test('LoRA와 ControlNet은 서로 충돌하지 않는 완전한 구조에서만 연결한다', () => {
  const lora = parseSafeTensorsHeader(headerBuffer({
    'lora_unet_a.lora_down.weight': { dtype: 'F16', shape: [1], data_offsets: [0, 2] },
    'lora_unet_a.lora_up.weight': { dtype: 'F16', shape: [1], data_offsets: [2, 4] },
    'lora_unet_b.lora_down.weight': { dtype: 'F16', shape: [1], data_offsets: [4, 6] },
    'lora_unet_b.lora_up.weight': { dtype: 'F16', shape: [1], data_offsets: [6, 8] }
  }))
  const controlNet = parseSafeTensorsHeader(headerBuffer({
    'input_hint_block.0.weight': { dtype: 'F16', shape: [1], data_offsets: [0, 2] },
    'zero_convs.0.0.weight': { dtype: 'F16', shape: [1], data_offsets: [2, 4] },
    'middle_block_out.0.weight': { dtype: 'F16', shape: [1], data_offsets: [4, 6] }
  }))
  assert.deepEqual(inferComfyModelAssetFromHeader(lora), { kind: 'lora', slot: 'lora', agentFamilies: [] })
  assert.deepEqual(inferComfyModelAssetFromHeader(controlNet), { kind: 'controlnet', slot: 'controlnet', agentFamilies: [] })
})

test('지원하지 않는 모델과 비정상 헤더는 자동 실행 대상으로 만들지 않는다', () => {
  const unknown = parseSafeTensorsHeader(headerBuffer({
    __metadata__: { 'modelspec.architecture': 'unknown-architecture' },
    'some.other.tensor': { dtype: 'F16', shape: [1], data_offsets: [0, 2] }
  }))
  assert.equal(inferComfyModelFamilyFromHeader(unknown), null)

  const oversized = Buffer.alloc(8)
  oversized.writeBigUInt64LE(BigInt(SAFE_TENSORS_HEADER_MAX_BYTES + 1), 0)
  assert.equal(parseSafeTensorsHeader(oversized), null)
})

test('직접 연결 파일은 표시 이름과 무관하게 등록되지만 Agent 자동 선택에는 쓰이지 않는다', () => {
  const profile: ComfyModelProfile = {
    id: 'custom-profile',
    name: '테스트',
    family: 'custom',
    capabilities: ['txt2img'],
    tags: [],
    assets: [{
      id: 'custom-asset',
      kind: 'custom',
      agentFamilies: [],
      fileName: 'arbitrary-model.safetensors',
      comfyName: 'arbitrary-model.safetensors',
      relativePath: 'my-custom-loader/arbitrary-model.safetensors',
      size: 1,
      sha256: 'a'.repeat(64),
      importedAt: 1
    }],
    workflowTemplateId: 'custom.txt2img.v1',
    defaults: getComfyGenerationDefaults('custom'),
    qualityMode: 'base',
    // 저장 파일이 잘못된 값이어도 readiness가 Agent 경로를 막아야 한다.
    agentEnabled: true,
    priority: 0,
    createdAt: 1,
    updatedAt: 1
  }
  const readiness = getComfyAgentReadiness(profile)
  assert.equal(readiness.ready, false)
  assert.match(readiness.detail, /직접 연결 파일/)
})

test('선택 사항인 실제 SafeTensors 파일도 본문 없이 판별한다', {
  skip: !process.env.AISO_COMFY_SDXL_TEST_MODEL
}, () => {
  assert.equal(inferComfyModelFamilyFromSafeTensors(process.env.AISO_COMFY_SDXL_TEST_MODEL!), 'sdxl')
  assert.deepEqual(
    inferComfyModelAssetFromSafeTensors(process.env.AISO_COMFY_SDXL_TEST_MODEL!),
    { kind: 'checkpoint', slot: 'checkpoint', agentFamilies: ['sdxl'] }
  )
})
