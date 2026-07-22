import { closeSync, openSync, readSync } from 'fs'
import type {
  ComfyAutomaticAssetKind,
  ComfyAssetSlot,
  ComfyModelFamily
} from '../shared/comfy-model'

/**
 * SafeTensors 파일의 JSON 헤더만 읽는 상한이다.
 * 모델 본문을 읽거나 GPU에 올리지 않으며, 비정상적으로 큰 헤더 선언도 거부한다.
 */
export const SAFE_TENSORS_HEADER_MAX_BYTES = 4 * 1024 * 1024

export interface SafeTensorsHeaderInfo {
  metadata: Readonly<Record<string, string>>
  tensorNames: readonly string[]
  tensorShapes: Readonly<Record<string, readonly number[]>>
}

/**
 * 파일 이름이 아닌 SafeTensors 헤더의 메타데이터와 텐서 구조에서만 얻은
 * ComfyUI 자산 분류다. 확신할 수 없는 경우에는 null을 반환해 잘못된 폴더로
 * 복사하지 않는다.
 */
export interface DetectedComfyModelAsset {
  kind: ComfyAutomaticAssetKind
  slot: ComfyAssetSlot
  agentFamilies: readonly ComfyModelFamily[]
}

type JsonObject = Record<string, unknown>

function asObject(value: unknown): JsonObject | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as JsonObject
    : null
}

function tensorShape(value: unknown): readonly number[] | null {
  const descriptor = asObject(value)
  const shape = descriptor?.shape
  if (!Array.isArray(shape) || shape.length > 16) return null
  const normalized = shape.map((dimension) => (
    typeof dimension === 'number' && Number.isSafeInteger(dimension) && dimension >= 0
      ? dimension
      : null
  ))
  return normalized.some((dimension) => dimension === null)
    ? null
    : normalized as number[]
}

function readExactly(fileDescriptor: number, target: Buffer, position: number): boolean {
  let offset = 0
  while (offset < target.length) {
    const read = readSync(fileDescriptor, target, offset, target.length - offset, position + offset)
    if (read <= 0) return false
    offset += read
  }
  return true
}

/** 순수 Buffer 파서라서 파일 I/O 없이도 안전하게 단위 검증할 수 있다. */
export function parseSafeTensorsHeader(buffer: Buffer): SafeTensorsHeaderInfo | null {
  if (buffer.length < 8) return null

  let headerLength: number
  try {
    const declaredLength = buffer.readBigUInt64LE(0)
    if (declaredLength > BigInt(SAFE_TENSORS_HEADER_MAX_BYTES)) return null
    headerLength = Number(declaredLength)
  } catch {
    return null
  }
  if (headerLength < 2 || buffer.length < 8 + headerLength) return null

  let parsed: JsonObject | null
  try {
    parsed = asObject(JSON.parse(buffer.subarray(8, 8 + headerLength).toString('utf8')))
  } catch {
    return null
  }
  if (!parsed) return null

  const metadataSource = asObject(parsed.__metadata__)
  const metadata: Record<string, string> = {}
  if (metadataSource) {
    for (const [key, value] of Object.entries(metadataSource)) {
      // 썸네일(base64) 같은 대형 부가 데이터는 분석 결과에 보관하지 않는다.
      if (key.length <= 128 && typeof value === 'string' && value.length <= 4_096) {
        metadata[key] = value
      }
    }
  }

  const tensors = Object.entries(parsed)
    .filter(([key, value]) => key !== '__metadata__' && asObject(value) !== null)
  const tensorNames = tensors.map(([key]) => key)
  const tensorShapes: Record<string, readonly number[]> = {}
  for (const [key, value] of tensors) {
    const shape = tensorShape(value)
    if (shape) tensorShapes[key] = shape
  }

  return { metadata, tensorNames, tensorShapes }
}

/**
 * 파일의 SafeTensors 헤더만 제한적으로 읽는다. 형식이 아니거나 손상된 경우에는
 * null을 반환해 호출자가 자동 워크플로 연결을 보류하도록 한다.
 */
export function readSafeTensorsHeader(filePath: string): SafeTensorsHeaderInfo | null {
  let descriptor: number | null = null
  try {
    descriptor = openSync(filePath, 'r')
    const prefix = Buffer.alloc(8)
    if (!readExactly(descriptor, prefix, 0)) return null
    const declaredLength = prefix.readBigUInt64LE(0)
    if (declaredLength > BigInt(SAFE_TENSORS_HEADER_MAX_BYTES)) return null
    const headerLength = Number(declaredLength)
    if (headerLength < 2) return null
    const payload = Buffer.alloc(8 + headerLength)
    prefix.copy(payload, 0)
    if (!readExactly(descriptor, payload.subarray(8), 8)) return null
    return parseSafeTensorsHeader(payload)
  } catch {
    return null
  } finally {
    if (descriptor !== null) closeSync(descriptor)
  }
}

/**
 * 메타데이터와 텐서 이름이 모두 명확할 때만 현재 내장 워크플로 계열을 판별한다.
 * 제목·파일명은 사용자가 임의로 바꿀 수 있으므로 근거로 쓰지 않는다.
 */
export function inferComfyModelFamilyFromHeader(
  header: SafeTensorsHeaderInfo | null
): ComfyModelFamily | null {
  if (!header) return null
  const metadataText = Object.entries(header.metadata)
    .filter(([key]) => /architecture|base_model|model_type|implementation/i.test(key))
    .map(([key, value]) => `${key}:${value}`)
    .join('\n')
    .toLowerCase()

  if (/flux[\s._-]*2\b/.test(metadataText)) return 'flux2'
  if (/(?:stable[\s_-]*diffusion[\s_-]*xl|sd[\s_-]*xl|sdxl)/.test(metadataText)) return 'sdxl'
  if (/(?:stable[\s_-]*diffusion[\s_-]*(?:v?1|1\.5)|sd[\s_-]*1(?:\.5)?|sd15)/.test(metadataText)) return 'sd15'
  if (/flux[\s._-]*(?:1\b|dev\b|schnell\b)/.test(metadataText)) return 'flux1'

  const tensorNames = header.tensorNames
  if (tensorNames.some((name) => name.startsWith('conditioner.embedders.1.'))) return 'sdxl'
  if (tensorNames.some((name) => name.startsWith('cond_stage_model.transformer.'))) return 'sd15'
  if (tensorNames.some((name) => name.startsWith('double_stream_modulation_img.lin.'))) return 'flux2'
  if (
    tensorNames.some((name) => name.startsWith('double_blocks.0.')) &&
    tensorNames.some((name) => name.startsWith('single_blocks.0.'))
  ) return 'flux1'

  return null
}

export function inferComfyModelFamilyFromSafeTensors(filePath: string): ComfyModelFamily | null {
  return inferComfyModelFamilyFromHeader(readSafeTensorsHeader(filePath))
}

function hasPrefix(tensorNames: readonly string[], prefix: string): boolean {
  return tensorNames.some((name) => name.startsWith(prefix))
}

function hasAllPrefixes(tensorNames: readonly string[], prefixes: readonly string[]): boolean {
  return prefixes.every((prefix) => hasPrefix(tensorNames, prefix))
}

function isLora(tensorNames: readonly string[]): boolean {
  const downOrA = new Set<string>()
  const upOrB = new Set<string>()
  for (const name of tensorNames) {
    const downMatch = name.match(/^(.*?)(?:[._])lora_(?:down|a)(?:[._]|$)/i)
    if (downMatch?.[1]) downOrA.add(downMatch[1])
    const upMatch = name.match(/^(.*?)(?:[._])lora_(?:up|b)(?:[._]|$)/i)
    if (upMatch?.[1]) upOrB.add(upMatch[1])
  }
  let pairs = 0
  for (const base of downOrA) {
    if (upOrB.has(base)) pairs += 1
  }
  return pairs >= 2
}

function isControlNet(tensorNames: readonly string[]): boolean {
  const legacy = hasAllPrefixes(tensorNames, [
    'input_hint_block.0.',
    'zero_convs.0.0.',
    'middle_block_out.0.'
  ])
  const diffusers = hasAllPrefixes(tensorNames, [
    'controlnet_cond_embedding.',
    'controlnet_down_blocks.0.',
    'controlnet_mid_block.'
  ])
  return legacy || diffusers
}

function isVae(tensorNames: readonly string[]): boolean {
  return hasAllPrefixes(tensorNames, [
    'encoder.conv_in.',
    'decoder.conv_out.',
    'quant_conv.',
    'post_quant_conv.'
  ])
}

function hasShapeFirstDimension(
  header: SafeTensorsHeaderInfo,
  name: string,
  expected: number
): boolean {
  return header.tensorShapes[name]?.[0] === expected
}

function isT5XXLTextEncoder(header: SafeTensorsHeaderInfo): boolean {
  const tensorNames = header.tensorNames
  const wi1 = 'encoder.block.23.layer.1.DenseReluDense.wi_1.weight'
  const wi = 'encoder.block.23.layer.1.DenseReluDense.wi.weight'
  return hasPrefix(tensorNames, 'shared.') &&
    (hasShapeFirstDimension(header, wi1, 10_240) || hasShapeFirstDimension(header, wi, 10_240))
}

function isClipLTextEncoder(header: SafeTensorsHeaderInfo): boolean {
  const prefixes = ['text_model.', 'transformer.text_model.']
  return prefixes.some((prefix) => {
    const tensorNames = header.tensorNames
    const tokenEmbedding = `${prefix}embeddings.token_embedding.weight`
    const layer11 = `${prefix}encoder.layers.11.mlp.fc1.weight`
    return hasShapeFirstDimension(header, tokenEmbedding, 49_408) &&
      header.tensorShapes[tokenEmbedding]?.[1] === 768 &&
      header.tensorShapes[layer11]?.[1] === 768 &&
      !hasPrefix(tensorNames, `${prefix}encoder.layers.22.`) &&
      !hasPrefix(tensorNames, `${prefix}encoder.layers.30.`)
  })
}

function detectSdCheckpointFamily(
  tensorNames: readonly string[],
  family: ComfyModelFamily | null
): Extract<ComfyModelFamily, 'sd15' | 'sdxl'> | null {
  const hasUnet = hasPrefix(tensorNames, 'model.diffusion_model.') || hasPrefix(tensorNames, 'diffusion_model.')
  const hasSdxlText = hasAllPrefixes(tensorNames, ['conditioner.embedders.0.', 'conditioner.embedders.1.'])
  const hasSd15Text = hasPrefix(tensorNames, 'cond_stage_model.transformer.')
  if (!hasUnet || (hasSdxlText === hasSd15Text)) return null
  const detected = hasSdxlText ? 'sdxl' : 'sd15'
  return family === null || family === detected ? detected : null
}

function detectFluxDiffusionFamily(
  tensorNames: readonly string[],
  family: ComfyModelFamily | null
): Extract<ComfyModelFamily, 'flux1' | 'flux2'> | null {
  const isFlux2 = hasPrefix(tensorNames, 'double_stream_modulation_img.lin.')
  const hasFlux1Core = hasAllPrefixes(tensorNames, [
    'img_in.weight',
    'double_blocks.0.img_attn.norm.key_norm.',
    'single_blocks.0.',
    'final_layer.linear.weight'
  ])
  if (isFlux2) return family === 'flux2' && hasPrefix(tensorNames, 'img_in.') ? 'flux2' : null
  return family !== 'flux2' && hasFlux1Core ? 'flux1' : null
}

function isFlux1Vae(header: SafeTensorsHeaderInfo): boolean {
  return isVae(header.tensorNames) &&
    header.tensorShapes['decoder.conv_in.weight']?.[1] === 16 &&
    header.tensorShapes['encoder.conv_out.weight']?.[0] === 32
}

/**
 * FLUX.2 VAE는 FLUX.1 VAE와 달리 latent 채널이 32개다. 파일명에 의존하지 않고
 * 양방향 첫/마지막 convolution 구조를 함께 확인해 다른 VAE를 섞지 않는다.
 */
function isFlux2Vae(header: SafeTensorsHeaderInfo): boolean {
  return isVae(header.tensorNames) &&
    header.tensorShapes['decoder.conv_in.weight']?.[1] === 32 &&
    header.tensorShapes['encoder.conv_out.weight']?.[0] === 64
}

/** FLUX.2 Klein 4B가 요구하는 Qwen 3 4B 텍스트 인코더의 고유 구조다. */
function isQwen3FourBTextEncoder(header: SafeTensorsHeaderInfo): boolean {
  return (
    hasAllPrefixes(header.tensorNames, [
      'model.embed_tokens.weight',
      'model.layers.0.self_attn.q_proj.weight',
      'model.layers.35.mlp.down_proj.weight',
      'model.norm.weight'
    ]) &&
    header.tensorShapes['model.embed_tokens.weight']?.[0] === 151936 &&
    header.tensorShapes['model.embed_tokens.weight']?.[1] === 2560
  )
}

/**
 * SafeTensors 내부 근거가 충분한 파일만 ComfyUI 대상 폴더와 슬롯으로 연결한다.
 * 사용자에게 모델 계열이나 파일 역할을 묻지 않기 위한 보수적 분류기다.
 */
export function inferComfyModelAssetFromHeader(
  header: SafeTensorsHeaderInfo | null
): DetectedComfyModelAsset | null {
  if (!header) return null

  const tensorNames = header.tensorNames
  const family = inferComfyModelFamilyFromHeader(header)

  // LoRA와 ControlNet은 자체 메타데이터가 SD/Flux를 가리킬 수 있으므로 먼저 분류한다.
  const lora = isLora(tensorNames)
  const controlNet = isControlNet(tensorNames)
  if (lora && controlNet) return null
  if (lora) return { kind: 'lora', slot: 'lora', agentFamilies: [] }
  if (controlNet) return { kind: 'controlnet', slot: 'controlnet', agentFamilies: [] }

  // 완전한 생성 모델에는 VAE/텍스트 인코더 텐서도 함께 들어갈 수 있으므로 보조 파일보다 먼저 확인한다.
  const sdFamily = detectSdCheckpointFamily(tensorNames, family)
  if (sdFamily) return { kind: 'checkpoint', slot: 'checkpoint', agentFamilies: [sdFamily] }
  const fluxFamily = detectFluxDiffusionFamily(tensorNames, family)
  if (fluxFamily) {
    return {
      kind: 'diffusion_model',
      slot: 'diffusion_model',
      // FLUX.2 Klein 4B는 Qwen 3·FLUX.2 VAE 계약이 함께 충족될 때만
      // readiness에서 실행 가능해진다. 확산 모델 자체도 해당 계약 후보로 표시해야
      // 세 파일이 모두 연결된 프로필을 불필요하게 차단하지 않는다.
      agentFamilies: [fluxFamily]
    }
  }

  if (isFlux2Vae(header)) return { kind: 'vae', slot: 'vae', agentFamilies: ['flux2'] }
  if (isFlux1Vae(header)) return { kind: 'vae', slot: 'vae', agentFamilies: ['flux1'] }
  if (isVae(tensorNames)) return { kind: 'vae', slot: 'vae', agentFamilies: [] }
  if (isQwen3FourBTextEncoder(header)) {
    return { kind: 'text_encoder', slot: 'qwen3', agentFamilies: ['flux2'] }
  }
  if (isT5XXLTextEncoder(header)) return { kind: 'text_encoder', slot: 't5xxl', agentFamilies: ['flux1'] }
  if (isClipLTextEncoder(header)) return { kind: 'text_encoder', slot: 'clip_l', agentFamilies: ['flux1'] }

  return null
}

export function inferComfyModelAssetFromSafeTensors(filePath: string): DetectedComfyModelAsset | null {
  return inferComfyModelAssetFromHeader(readSafeTensorsHeader(filePath))
}
