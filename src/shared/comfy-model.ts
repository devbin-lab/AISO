export const COMFY_ASSET_KINDS = [
  'checkpoint',
  'diffusion_model',
  'text_encoder',
  'vae',
  'lora',
  'controlnet',
  /** 역할을 추측하지 않고 사용자가 지정한 ComfyUI 폴더에 직접 연결한 파일. */
  'custom'
] as const

export type ComfyAssetKind = (typeof COMFY_ASSET_KINDS)[number]
/** 헤더 분석으로 역할을 확정해 자동 배치할 수 있는 파일 종류다. */
export type ComfyAutomaticAssetKind = Exclude<ComfyAssetKind, 'custom'>

export const COMFY_ASSET_SLOTS = [
  'checkpoint',
  'diffusion_model',
  'clip_l',
  't5xxl',
  /** FLUX.2 Klein 4B에서 사용하는 Qwen 3 4B 텍스트 인코더. */
  'qwen3',
  'vae',
  'lora',
  'controlnet'
] as const

export type ComfyAssetSlot = (typeof COMFY_ASSET_SLOTS)[number]

/** 화면과 안내문에서 사용하는 사람이 읽을 수 있는 구성 파일 이름이다. */
export const COMFY_ASSET_KIND_LABELS: Record<ComfyAssetKind, string> = {
  checkpoint: '생성 모델(체크포인트)',
  diffusion_model: '확산 모델',
  text_encoder: '텍스트 인코더',
  vae: 'VAE',
  lora: 'LoRA',
  controlnet: 'ControlNet',
  custom: '직접 연결 파일'
}

export const COMFY_ASSET_SLOT_LABELS: Record<ComfyAssetSlot, string> = {
  checkpoint: '생성 모델(체크포인트)',
  diffusion_model: '확산 모델',
  clip_l: '텍스트 인코더 · CLIP-L',
  t5xxl: '텍스트 인코더 · T5XXL',
  qwen3: '텍스트 인코더 · Qwen 3',
  vae: 'VAE',
  lora: 'LoRA',
  controlnet: 'ControlNet'
}

export const COMFY_MODEL_FAMILIES = ['sd15', 'sdxl', 'flux1', 'flux2', 'custom'] as const

export type ComfyModelFamily = (typeof COMFY_MODEL_FAMILIES)[number]

export const COMFY_MODEL_CAPABILITIES = ['txt2img', 'img2img', 'inpaint'] as const

export type ComfyModelCapability = (typeof COMFY_MODEL_CAPABILITIES)[number]

/**
 * The quality policy is intentionally small and explicit.  `refine` is not a
 * generic upscaler promise: it only selects Aiso's built-in latent refinement
 * path when the registered profile supports that exact workflow.
 */
export const COMFY_QUALITY_MODES = ['base', 'refine'] as const

export type ComfyQualityMode = (typeof COMFY_QUALITY_MODES)[number]

export interface ComfyGenerationDefaults {
  width: number
  height: number
  steps: number
  cfg: number
  sampler?: string
  scheduler?: string
}

export const COMFY_WORKFLOW_BINDING_TARGETS = [
  'positivePrompt',
  'negativePrompt',
  'seed',
  'width',
  'height',
  'steps',
  'cfg',
  'sampler',
  'scheduler',
  'filenamePrefix'
] as const

export type ComfyWorkflowBindingTarget = (typeof COMFY_WORKFLOW_BINDING_TARGETS)[number]

export interface ComfyWorkflowInputRef {
  nodeId: string
  input: string
}

/**
 * A model-loader input in a user supplied API workflow is bound to one exact
 * asset that Aiso has registered.  The loader value is retained because
 * ComfyUI uses its own folder-relative names, while the asset identity is
 * pinned by id, hash, and its path under ComfyUI/models.
 */
export interface ComfyWorkflowAssetBinding extends ComfyWorkflowInputRef {
  assetId: string
  sha256: string
  relativePath: string
  comfyName: string
}

export type ComfyWorkflowInputValue = string | number | boolean | null | ComfyWorkflowInputValue[]

export interface ComfyWorkflowNode {
  class_type: string
  inputs: Record<string, ComfyWorkflowInputValue>
}

export interface ComfyWorkflowTemplate {
  schemaVersion: 1
  /** 내용 해시로 만든 Aiso 내부 ID. 파일명이나 모델 계열에 의존하지 않는다. */
  id: string
  sourceFileName: string
  sha256: string
  graph: Record<string, ComfyWorkflowNode>
  bindings: Record<ComfyWorkflowBindingTarget, ComfyWorkflowInputRef[]>
  /** Every literal SafeTensors loader value that Agent may execute. */
  assetBindings: ComfyWorkflowAssetBinding[]
  importedAt: number
}

export interface ComfyModelAsset {
  id: string
  kind: ComfyAssetKind
  /** 워크플로 입력 슬롯. text_encoder는 clip_l/t5xxl을 구분한다. */
  slot?: ComfyAssetSlot
  /** 새로 연결한 파일의 헤더 분석으로 확인한 Agent 자동 워크플로 호환 범위다. */
  /** Read-only so registry consumers cannot mutate an asset capability in place. */
  agentFamilies?: readonly ComfyModelFamily[]
  fileName: string
  /** 해당 ComfyUI loader 카테고리를 기준으로 한 상대 이름. */
  comfyName: string
  /** ComfyUI/models를 기준으로 한 POSIX 상대 경로. */
  relativePath: string
  size: number
  sha256: string
  importedAt: number
}

export interface ComfyModelProfile {
  id: string
  name: string
  /** 연결한 파일을 분석해 Aiso 내부에서만 정하는 실행 분류다. */
  family: ComfyModelFamily
  capabilities: ComfyModelCapability[]
  tags: string[]
  assets: ComfyModelAsset[]
  workflowTemplateId: string
  /** 사용자가 ComfyUI에서 내보내 연결한 검증된 API 형식 워크플로. */
  workflowTemplate?: ComfyWorkflowTemplate
  defaults: ComfyGenerationDefaults
  /** Per-profile quality choice. Unsupported profiles are normalized to `base`. */
  qualityMode: ComfyQualityMode
  agentEnabled: boolean
  priority: number
  createdAt: number
  updatedAt: number
}

export interface ComfyModelRegistry {
  schemaVersion: 1
  profiles: ComfyModelProfile[]
}

export interface ComfyModelImportRequest {
  operationId: string
  profileId?: string
  name: string
  capabilities?: ComfyModelCapability[]
  tags?: string[]
}

export interface ComfyModelImportResult {
  canceled: boolean
  profile?: ComfyModelProfile
  imported: ComfyModelAsset[]
  reused: ComfyModelAsset[]
}

export type ComfyModelImportPhase = 'hashing' | 'copying' | 'verifying' | 'complete'

export interface ComfyModelImportProgress {
  operationId: string
  phase: ComfyModelImportPhase
  fileName: string
  completedBytes: number
  totalBytes: number
}

export interface ComfyModelProfilePatch {
  name?: string
  capabilities?: ComfyModelCapability[]
  tags?: string[]
  defaults?: Partial<ComfyGenerationDefaults>
  qualityMode?: ComfyQualityMode
  agentEnabled?: boolean
  priority?: number
}

/**
 * v0.4's built-in refinement graph has been verified only for the automatic
 * SDXL workflow.  SD 1.5, FLUX, and user-provided API workflows keep their
 * own existing graph and therefore remain on base generation.
 */
export function supportsComfyQualityRefinement(
  profile: Pick<ComfyModelProfile, 'family' | 'workflowTemplate'>
): boolean {
  return profile.family === 'sdxl' && profile.workflowTemplate === undefined
}

/** Return the effective mode so stale/imported registry data never overclaims support. */
export function getEffectiveComfyQualityMode(
  profile: Pick<ComfyModelProfile, 'family' | 'workflowTemplate' | 'qualityMode'>
): ComfyQualityMode {
  return profile.qualityMode === 'refine' && supportsComfyQualityRefinement(profile)
    ? 'refine'
    : 'base'
}

export interface ComfyWorkflowImportResult {
  canceled: boolean
  profile?: ComfyModelProfile
}

export interface ComfyAgentReadiness {
  ready: boolean
  missingSlots: ComfyAssetSlot[]
  incompatibleSlots: ComfyAssetSlot[]
  detail: string
  notices: string[]
}

export interface ComfyWorkflowAssetBindingStatus {
  expected: ComfyWorkflowInputRef[]
  missing: ComfyWorkflowInputRef[]
  boundAssets: ComfyModelAsset[]
}

const COMFY_MODEL_LOADER_INPUT_NAMES = new Set([
  'ckpt_name',
  'checkpoint_name',
  'unet_name',
  'model_name',
  'diffusion_model',
  'clip_name',
  'clip_name1',
  'clip_name2',
  'text_encoder_name',
  'vae_name',
  'lora_name',
  'control_net_name',
  'controlnet_name',
  'adapter_name'
])

function isWorkflowModelAssetInput(input: string, value: ComfyWorkflowInputValue): value is string {
  if (typeof value !== 'string' || !value.toLowerCase().endsWith('.safetensors')) return false
  if (COMFY_MODEL_LOADER_INPUT_NAMES.has(input.toLowerCase())) return true
  return /(?:^|_)(?:checkpoint|ckpt|unet|model|diffusion|clip|text_encoder|vae|lora|control_?net|adapter)(?:_name|_file|_path)$/i.test(input)
}

function workflowRefKey(ref: ComfyWorkflowInputRef): string {
  return `${ref.nodeId}\u0000${ref.input}`
}

function normalizedComfyAssetName(value: string): string {
  return value.replace(/\\/g, '/').replace(/^\.\//, '').toLocaleLowerCase('en-US')
}

/** Literal SafeTensors loader inputs that must be pinned before Agent can run a user workflow. */
export function getComfyWorkflowAssetRefs(template: Pick<ComfyWorkflowTemplate, 'graph'>): ComfyWorkflowInputRef[] {
  const refs: ComfyWorkflowInputRef[] = []
  for (const [nodeId, node] of Object.entries(template.graph)) {
    for (const [input, value] of Object.entries(node.inputs)) {
      if (isWorkflowModelAssetInput(input, value)) refs.push({ nodeId, input })
    }
  }
  return refs
}

/**
 * Do not trust a workflow's literal loader value on its own.  It is usable by
 * Agent only when it still resolves to the exact registered asset contract.
 */
export function getComfyWorkflowAssetBindingStatus(
  template: ComfyWorkflowTemplate,
  assets: readonly ComfyModelAsset[]
): ComfyWorkflowAssetBindingStatus {
  const expected = getComfyWorkflowAssetRefs(template)
  const bindings = new Map<string, ComfyWorkflowAssetBinding>()
  const duplicateBindings = new Set<string>()
  for (const binding of template.assetBindings) {
    const key = workflowRefKey(binding)
    if (bindings.has(key)) duplicateBindings.add(key)
    else bindings.set(key, binding)
  }

  const missing: ComfyWorkflowInputRef[] = []
  const boundAssets: ComfyModelAsset[] = []
  const seenAssets = new Set<string>()
  for (const ref of expected) {
    const key = workflowRefKey(ref)
    const binding = bindings.get(key)
    const literal = template.graph[ref.nodeId]?.inputs[ref.input]
    const asset = binding ? assets.find((item) => item.id === binding.assetId) : undefined
    if (
      duplicateBindings.has(key) ||
      !binding ||
      typeof literal !== 'string' ||
      binding.comfyName !== literal ||
      !asset ||
      asset.sha256 !== binding.sha256 ||
      asset.relativePath !== binding.relativePath ||
      ![
        normalizedComfyAssetName(asset.comfyName),
        normalizedComfyAssetName(asset.relativePath)
      ].includes(normalizedComfyAssetName(binding.comfyName))
    ) {
      missing.push(ref)
      continue
    }
    if (!seenAssets.has(asset.id)) {
      seenAssets.add(asset.id)
      boundAssets.push(asset)
    }
  }
  return { expected, missing, boundAssets }
}

/** Agent 기본 텍스트→이미지 템플릿이 현재 사용하는 필수 구성 파일이다. */
export function getComfyRequiredSlots(family: ComfyModelFamily): ComfyAssetSlot[] {
  if (family === 'flux1') return ['diffusion_model', 'clip_l', 't5xxl', 'vae']
  if (family === 'flux2') return ['diffusion_model', 'qwen3', 'vae']
  if (family === 'sd15' || family === 'sdxl') return ['checkpoint']
  return []
}

function assetSupportsAgentFamily(asset: ComfyModelAsset, family: ComfyModelFamily): boolean {
  if (asset.agentFamilies !== undefined) {
    if (asset.agentFamilies.includes(family)) return true
    // FLUX.2 지원을 추가하기 전 저장한 프로필은 확산 모델의 SafeTensors 구조를
    // 이미 flux2로 판별했지만 agentFamilies를 빈 배열로 기록했다. 프로필 family와
    // 주 확산 모델 slot이 모두 일치할 때만 이전 저장값을 호환 처리한다.
    return family === 'flux2' && asset.slot === 'diffusion_model' && asset.agentFamilies.length === 0
  }
  // 이전 레지스트리에는 헤더 분석 결과가 없었다. 단일 체크포인트 기반 프로필만
  // 기존 실행 계약을 보존하고, 분리형 구성은 다시 연결해 검증하도록 한다.
  return (family === 'sd15' || family === 'sdxl') && asset.slot === 'checkpoint'
}

/** Agent 워크플로 컴파일러가 현재 실행할 수 있는 최소 구성 파일 계약. */
export function getComfyAgentReadiness(profile: ComfyModelProfile): ComfyAgentReadiness {
  if (profile.workflowTemplate) {
    const contract = getComfyWorkflowAssetBindingStatus(profile.workflowTemplate, profile.assets)
    const missingLabels = contract.missing.map((ref) => `${ref.nodeId}.${ref.input}`)
    return {
      ready: contract.expected.length > 0 && contract.missing.length === 0,
      missingSlots: [],
      incompatibleSlots: [],
      detail: contract.expected.length === 0
        ? '사용자 워크플로에서 SafeTensors 모델 로더를 찾지 못했습니다. Agent 실행에는 등록 모델과 연결된 로더가 필요합니다.'
        : contract.missing.length > 0
          ? `사용자 워크플로의 등록 모델 연결이 확인되지 않았습니다: ${missingLabels.join(', ')}`
          : `사용자 워크플로 연결됨 · 등록 모델 ${contract.boundAssets.length}개 확인 · ${profile.workflowTemplate.sourceFileName}`,
      notices: contract.missing.length > 0
        ? ['워크플로의 모델 로더 값과 등록 자산의 이름·경로·SHA-256이 모두 일치해야 Agent를 활성화할 수 있습니다.']
        : []
    }
  }
  const automaticSlots = getComfyRequiredSlots(profile.family)
  const automaticSlotSet = new Set(automaticSlots)
  const ignoredKinds = [...new Set(
    profile.assets
      .filter((asset) => !asset.slot || !automaticSlotSet.has(asset.slot))
      .map((asset) => asset.kind)
  )]
  const notices = ignoredKinds.length
    ? [`${ignoredKinds.map((kind) => COMFY_ASSET_KIND_LABELS[kind]).join(' / ')}은(는) 등록되어 있지만 현재 기본 Agent 템플릿에는 적용되지 않습니다. ComfyUI 화면에서 직접 사용하는 용도입니다.`]
    : []
  if (profile.assets.some((asset) => asset.kind === 'custom')) {
    return {
      ready: false,
      missingSlots: [],
      incompatibleSlots: [],
      detail: '직접 연결 파일입니다. ComfyUI에서 바로 사용할 수 있으며, Agent 사용은 카드의 ‘워크플로 연결’에서 API 형식 JSON을 연결하면 활성화할 수 있습니다.',
      notices
    }
  }
  if (profile.family === 'custom') {
    return {
      ready: false,
      missingSlots: [],
      incompatibleSlots: [],
      detail: '자동 워크플로를 확인하지 못했습니다. ComfyUI에서 직접 사용하거나, 카드의 ‘워크플로 연결’에서 Save (API Format) JSON을 연결하세요.',
      notices
    }
  }
  const installed = new Set(profile.assets.map((asset) => asset.slot).filter(Boolean))
  const missingSlots = automaticSlots.filter((slot) => !installed.has(slot))
  const incompatibleSlots = automaticSlots.filter((slot) => profile.assets.some((asset) => (
    asset.slot === slot && !assetSupportsAgentFamily(asset, profile.family)
  )))
  return missingSlots.length === 0 && incompatibleSlots.length === 0
    ? {
        ready: true,
        missingSlots: [],
        incompatibleSlots: [],
        detail: '필수 구성 파일 연결됨 · Aiso 자동 워크플로를 사용할 수 있습니다.',
        notices
      }
    : {
        ready: false,
        missingSlots,
        incompatibleSlots,
        detail: [
          missingSlots.length > 0
            ? `필수 구성 파일 필요: ${missingSlots.map((slot) => COMFY_ASSET_SLOT_LABELS[slot]).join(', ')}`
            : '',
          incompatibleSlots.length > 0
            ? `자동 워크플로 호환 확인 필요: ${incompatibleSlots.map((slot) => COMFY_ASSET_SLOT_LABELS[slot]).join(', ')}`
            : ''
        ].filter(Boolean).join(' · '),
        notices
      }
}

export const DEFAULT_COMFY_GENERATION: ComfyGenerationDefaults = {
  width: 1024,
  height: 1024,
  steps: 28,
  cfg: 5,
  sampler: 'euler_ancestral',
  scheduler: 'normal'
}

/** 모델 실행 방식에 맞춘 새 프로필의 안전한 시작값이다. */
export function getComfyGenerationDefaults(family: ComfyModelFamily): ComfyGenerationDefaults {
  if (family === 'sd15') {
    return {
      width: 512,
      height: 512,
      steps: 28,
      cfg: 7,
      sampler: 'euler_ancestral',
      scheduler: 'normal'
    }
  }
  if (family === 'flux1') {
    return {
      width: 1024,
      height: 1024,
      steps: 20,
      cfg: 3.5,
      sampler: 'euler',
      scheduler: 'simple'
    }
  }
  if (family === 'flux2') {
    return {
      width: 1024,
      height: 1024,
      steps: 4,
      cfg: 1,
      sampler: 'euler',
      // FLUX.2는 Flux2Scheduler가 해상도와 step으로 sigma를 계산한다.
      // 일반 scheduler 선택값은 워크플로에 전달하지 않는다.
      scheduler: undefined
    }
  }
  return { ...DEFAULT_COMFY_GENERATION }
}
