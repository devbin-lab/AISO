export const COMFY_ASSET_KINDS = [
  'checkpoint',
  'diffusion_model',
  'text_encoder',
  'vae',
  'lora',
  'controlnet'
] as const

export type ComfyAssetKind = (typeof COMFY_ASSET_KINDS)[number]

export const COMFY_ASSET_SLOTS = [
  'checkpoint',
  'diffusion_model',
  'clip_l',
  't5xxl',
  'vae',
  'lora',
  'controlnet'
] as const

export type ComfyAssetSlot = (typeof COMFY_ASSET_SLOTS)[number]

export const COMFY_MODEL_FAMILIES = ['sd15', 'sdxl', 'flux1', 'flux2', 'custom'] as const

export type ComfyModelFamily = (typeof COMFY_MODEL_FAMILIES)[number]

export const COMFY_MODEL_CAPABILITIES = ['txt2img', 'img2img', 'inpaint'] as const

export type ComfyModelCapability = (typeof COMFY_MODEL_CAPABILITIES)[number]

export interface ComfyGenerationDefaults {
  width: number
  height: number
  steps: number
  cfg: number
  sampler?: string
  scheduler?: string
}

export interface ComfyModelAsset {
  id: string
  kind: ComfyAssetKind
  /** 워크플로 입력 슬롯. text_encoder는 clip_l/t5xxl을 구분한다. */
  slot?: ComfyAssetSlot
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
  family: ComfyModelFamily
  capabilities: ComfyModelCapability[]
  tags: string[]
  assets: ComfyModelAsset[]
  workflowTemplateId: string
  defaults: ComfyGenerationDefaults
  agentEnabled: boolean
  priority: number
  createdAt: number
  updatedAt: number
}

export interface ComfyModelRegistry {
  schemaVersion: 1
  profiles: ComfyModelProfile[]
}

export interface ComfyModelProfileInput {
  name: string
  family: ComfyModelFamily
  capabilities?: ComfyModelCapability[]
  tags?: string[]
  workflowTemplateId?: string
  defaults?: Partial<ComfyGenerationDefaults>
  agentEnabled?: boolean
  priority?: number
}

export interface ComfyModelImportRequest extends ComfyModelProfileInput {
  operationId: string
  profileId?: string
  assetKind: ComfyAssetKind
  assetSlot?: ComfyAssetSlot
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
  family?: ComfyModelFamily
  capabilities?: ComfyModelCapability[]
  tags?: string[]
  workflowTemplateId?: string
  defaults?: Partial<ComfyGenerationDefaults>
  agentEnabled?: boolean
  priority?: number
}

export interface ComfyAgentReadiness {
  ready: boolean
  missingSlots: ComfyAssetSlot[]
  detail: string
  notices: string[]
}

/** Agent 워크플로 컴파일러가 현재 실행할 수 있는 최소 자산 계약. */
export function getComfyAgentReadiness(profile: ComfyModelProfile): ComfyAgentReadiness {
  const automaticSlots: ComfyAssetSlot[] = profile.family === 'flux1'
    ? ['diffusion_model', 'clip_l', 't5xxl', 'vae']
    : profile.family === 'sd15' || profile.family === 'sdxl'
      ? ['checkpoint']
      : []
  const automaticSlotSet = new Set(automaticSlots)
  const ignoredKinds = [...new Set(
    profile.assets
      .filter((asset) => !asset.slot || !automaticSlotSet.has(asset.slot))
      .map((asset) => asset.kind)
  )]
  const ignoredLabel: Record<ComfyAssetKind, string> = {
    checkpoint: '체크포인트',
    diffusion_model: 'Diffusion model',
    text_encoder: 'Text encoder',
    vae: 'VAE',
    lora: 'LoRA',
    controlnet: 'ControlNet'
  }
  const notices = ignoredKinds.length
    ? [`${ignoredKinds.map((kind) => ignoredLabel[kind]).join('/')}는 등록됨 · 현재 Agent 템플릿에는 적용되지 않음`]
    : []
  if (profile.family === 'flux2' || profile.family === 'custom') {
    return {
      ready: false,
      missingSlots: [],
      detail: '현재 Agent 자동 워크플로 미지원',
      notices
    }
  }
  const installed = new Set(profile.assets.map((asset) => asset.slot).filter(Boolean))
  const missingSlots = automaticSlots.filter((slot) => !installed.has(slot))
  return missingSlots.length === 0
    ? { ready: true, missingSlots: [], detail: 'Agent 실행 준비됨', notices }
    : {
        ready: false,
        missingSlots,
        detail: `필수 자산 누락: ${missingSlots.join(', ')}`,
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
