import { createHash } from 'crypto'
import { basename } from 'path'
import type {
  ComfyModelAsset,
  ComfyGenerationDefaults,
  ComfyWorkflowAssetBinding,
  ComfyWorkflowBindingTarget,
  ComfyWorkflowInputRef,
  ComfyWorkflowInputValue,
  ComfyWorkflowNode,
  ComfyWorkflowTemplate
} from '../shared/comfy-model'

const COMFY_WORKFLOW_BINDING_TARGETS: readonly ComfyWorkflowBindingTarget[] = [
  'positivePrompt', 'negativePrompt', 'seed', 'width', 'height', 'steps', 'cfg',
  'sampler', 'scheduler', 'filenamePrefix'
]

const MAX_TEMPLATE_BYTES = 1024 * 1024
const MAX_NODES = 96
const MAX_INPUTS_PER_NODE = 64
const MAX_SAVE_IMAGE_NODES = 4
const MAX_NODE_CLASSES = 64
const BOUNDED_LITERAL_INPUTS = new Set(['width', 'height', 'steps', 'cfg', 'guidance', 'batch_size'])
const MAX_VALUE_DEPTH = 8
const MAX_STRING_LENGTH = 16_384
const NODE_ID_RE = /^[A-Za-z0-9._-]{1,128}$/
const NODE_CLASS_RE = /^[A-Za-z0-9_]{1,128}$/
const INPUT_NAME_RE = /^[A-Za-z0-9_]{1,128}$/
const MODEL_LOADER_INPUT_NAMES = new Set([
  'ckpt_name', 'checkpoint_name', 'unet_name', 'model_name', 'diffusion_model',
  'clip_name', 'clip_name1', 'clip_name2', 'text_encoder_name', 'vae_name',
  'lora_name', 'control_net_name', 'controlnet_name', 'adapter_name'
])
const MODEL_LOADER_INPUT_RE = /(?:^|_)(?:checkpoint|ckpt|unet|model|diffusion|clip|text_encoder|vae|lora|control_?net|adapter)(?:_name|_file|_path)$/i

function isModelLoaderInputName(input: string): boolean {
  return MODEL_LOADER_INPUT_NAMES.has(input.toLowerCase()) || MODEL_LOADER_INPUT_RE.test(input)
}

type JsonObject = Record<string, unknown>

export interface ParsedComfyWorkflowTemplate {
  template: ComfyWorkflowTemplate
  suggestedDefaults: Partial<ComfyGenerationDefaults>
}

function asObject(value: unknown): JsonObject | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as JsonObject
    : null
}

function sanitizeInputValue(value: unknown, depth = 0): ComfyWorkflowInputValue {
  if (depth > MAX_VALUE_DEPTH) throw new Error('워크플로 입력 구조가 너무 깊습니다.')
  if (value === null || typeof value === 'boolean') return value
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('워크플로에 유효하지 않은 숫자가 있습니다.')
    return value
  }
  if (typeof value === 'string') {
    if (value.length > MAX_STRING_LENGTH || /[\u0000]/.test(value)) {
      throw new Error('워크플로 문자열 입력이 허용 범위를 벗어났습니다.')
    }
    return value
  }
  if (Array.isArray(value)) {
    if (value.length > 256) throw new Error('워크플로 배열 입력이 너무 큽니다.')
    return value.map((item) => sanitizeInputValue(item, depth + 1))
  }
  throw new Error('ComfyUI API 워크플로가 아닌 입력 값이 포함되어 있습니다.')
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize)
  const object = asObject(value)
  if (!object) return value
  return Object.fromEntries(
    Object.keys(object).sort().map((key) => [key, canonicalize(object[key])])
  )
}

function contentHash(
  graph: Record<string, ComfyWorkflowNode>,
  bindings: Record<ComfyWorkflowBindingTarget, ComfyWorkflowInputRef[]>,
  assetBindings: readonly ComfyWorkflowAssetBinding[]
): string {
  return createHash('sha256')
    .update(JSON.stringify(canonicalize({ graph, bindings, assetBindings })), 'utf8')
    .digest('hex')
}

/** Kept only to migrate pre-contract workflow records without treating them as valid new records. */
function legacyContentHash(
  graph: Record<string, ComfyWorkflowNode>,
  bindings: Record<ComfyWorkflowBindingTarget, ComfyWorkflowInputRef[]>
): string {
  return createHash('sha256')
    .update(JSON.stringify(canonicalize({ graph, bindings })), 'utf8')
    .digest('hex')
}

function workflowRefKey(ref: ComfyWorkflowInputRef): string {
  return `${ref.nodeId}\u0000${ref.input}`
}

function workflowAssetRefs(graph: Record<string, ComfyWorkflowNode>): ComfyWorkflowInputRef[] {
  const refs: ComfyWorkflowInputRef[] = []
  for (const [nodeId, node] of Object.entries(graph)) {
    for (const [input, value] of Object.entries(node.inputs)) {
      if (
        typeof value === 'string' &&
        value.toLowerCase().endsWith('.safetensors') &&
        isModelLoaderInputName(input)
      ) {
        refs.push({ nodeId, input })
      }
    }
  }
  return refs
}

function safeAssetId(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9._-]{1,128}$/.test(value)
}

function safeRelativeModelPath(value: unknown): value is string {
  if (typeof value !== 'string' || !value || value.length > 512 || /[\\\u0000]/.test(value)) return false
  if (value.startsWith('/') || /^[A-Za-z]:/.test(value) || !value.toLowerCase().endsWith('.safetensors')) return false
  return value.split('/').every((part) => part && part !== '.' && part !== '..')
}

function parseAssetBindings(
  value: unknown,
  graph: Record<string, ComfyWorkflowNode>,
  { allowMissing = false }: { allowMissing?: boolean } = {}
): ComfyWorkflowAssetBinding[] | null {
  if (value === undefined && allowMissing) return []
  if (!Array.isArray(value) || value.length > MAX_NODES) return null
  const expected = new Map(workflowAssetRefs(graph).map((ref) => [workflowRefKey(ref), ref]))
  const bindings: ComfyWorkflowAssetBinding[] = []
  const seen = new Set<string>()
  for (const item of value) {
    const raw = asObject(item)
    if (!raw || Object.keys(raw).length !== 6) return null
    const { nodeId, input, assetId, sha256, relativePath, comfyName } = raw
    if (
      typeof nodeId !== 'string' || !NODE_ID_RE.test(nodeId) ||
      typeof input !== 'string' || !INPUT_NAME_RE.test(input) ||
      !safeAssetId(assetId) ||
      typeof sha256 !== 'string' || !/^[a-f0-9]{64}$/i.test(sha256) ||
      !safeRelativeModelPath(relativePath) ||
      !safeRelativeModelPath(comfyName)
    ) return null
    const key = `${nodeId}\u0000${input}`
    const ref = expected.get(key)
    if (seen.has(key) || !ref || graph[nodeId]?.inputs[input] !== comfyName) return null
    seen.add(key)
    bindings.push({
      nodeId,
      input,
      assetId,
      sha256: sha256.toLowerCase(),
      relativePath,
      comfyName
    })
  }
  return bindings
}

function makeTemplate(
  graph: Record<string, ComfyWorkflowNode>,
  bindings: Record<ComfyWorkflowBindingTarget, ComfyWorkflowInputRef[]>,
  assetBindings: ComfyWorkflowAssetBinding[],
  sourceFileName: string,
  importedAt: number
): ComfyWorkflowTemplate {
  const sha256 = contentHash(graph, bindings, assetBindings)
  return {
    schemaVersion: 1,
    id: `user.${sha256.slice(0, 20)}.txt2img.v1`,
    sourceFileName,
    sha256,
    graph,
    bindings,
    assetBindings,
    importedAt
  }
}

function emptyBindings(): Record<ComfyWorkflowBindingTarget, ComfyWorkflowInputRef[]> {
  return {
    positivePrompt: [],
    negativePrompt: [],
    seed: [],
    width: [],
    height: [],
    steps: [],
    cfg: [],
    sampler: [],
    scheduler: [],
    filenamePrefix: []
  }
}

function addBinding(
  bindings: Record<ComfyWorkflowBindingTarget, ComfyWorkflowInputRef[]>,
  target: ComfyWorkflowBindingTarget,
  nodeId: string,
  input: string
): void {
  if (!bindings[target].some((ref) => ref.nodeId === nodeId && ref.input === input)) {
    bindings[target].push({ nodeId, input })
  }
}

function markerTarget(value: unknown): ComfyWorkflowBindingTarget | null {
  if (typeof value !== 'string') return null
  const normalized = value.trim().toLowerCase()
  const markers: Record<string, ComfyWorkflowBindingTarget> = {
    '{{prompt}}': 'positivePrompt',
    '{{positive_prompt}}': 'positivePrompt',
    aiso_prompt: 'positivePrompt',
    '{{negative_prompt}}': 'negativePrompt',
    aiso_negative_prompt: 'negativePrompt',
    '{{seed}}': 'seed',
    '{{width}}': 'width',
    '{{height}}': 'height',
    '{{steps}}': 'steps',
    '{{cfg}}': 'cfg',
    '{{sampler}}': 'sampler',
    '{{scheduler}}': 'scheduler',
    '{{filename_prefix}}': 'filenamePrefix'
  }
  return markers[normalized] ?? null
}

function inferBindings(
  graph: Record<string, ComfyWorkflowNode>,
  titles: Record<string, string>
): Record<ComfyWorkflowBindingTarget, ComfyWorkflowInputRef[]> {
  const bindings = emptyBindings()
  const promptCandidates: Array<{ nodeId: string; input: string; negative: boolean; positive: boolean }> = []

  for (const [nodeId, node] of Object.entries(graph)) {
    const title = `${titles[nodeId] ?? ''} ${node.class_type}`.toLowerCase()
    for (const [input, value] of Object.entries(node.inputs)) {
      const explicit = markerTarget(value)
      if (explicit) {
        addBinding(bindings, explicit, nodeId, input)
        continue
      }
      const normalizedInput = input.toLowerCase()
      const numericTarget: ComfyWorkflowBindingTarget | null = {
        seed: 'seed',
        noise_seed: 'seed',
        width: 'width',
        height: 'height',
        steps: 'steps',
        cfg: 'cfg',
        guidance: 'cfg'
      }[normalizedInput] as ComfyWorkflowBindingTarget | undefined ?? null
      if (numericTarget && typeof value === 'number') addBinding(bindings, numericTarget, nodeId, input)
      if (normalizedInput === 'sampler_name' && typeof value === 'string') {
        addBinding(bindings, 'sampler', nodeId, input)
      }
      if (normalizedInput === 'scheduler' && typeof value === 'string') {
        addBinding(bindings, 'scheduler', nodeId, input)
      }
      if (normalizedInput === 'filename_prefix' && node.class_type === 'SaveImage') {
        addBinding(bindings, 'filenamePrefix', nodeId, input)
      }
      if (
        typeof value === 'string' &&
        ['text', 'prompt', 'positive_prompt', 'negative_prompt'].includes(normalizedInput)
      ) {
        promptCandidates.push({
          nodeId,
          input,
          negative: normalizedInput.includes('negative') || /negative|neg prompt|부정/.test(title),
          positive: normalizedInput.includes('positive') || /positive|pos prompt|긍정/.test(title)
        })
      }
    }
  }

  if (bindings.positivePrompt.length === 0) {
    const explicitPositive = promptCandidates.filter((candidate) => candidate.positive && !candidate.negative)
    const selected = explicitPositive.length > 0
      ? explicitPositive
      : promptCandidates.filter((candidate) => !candidate.negative).slice(0, 1)
    for (const candidate of selected) {
      addBinding(bindings, 'positivePrompt', candidate.nodeId, candidate.input)
    }
  }
  if (bindings.negativePrompt.length === 0) {
    let selected = promptCandidates.filter((candidate) => candidate.negative)
    if (selected.length === 0 && promptCandidates.length === 2) selected = promptCandidates.slice(1)
    for (const candidate of selected) {
      addBinding(bindings, 'negativePrompt', candidate.nodeId, candidate.input)
    }
  }
  return bindings
}

function sanitizeGraph(rawValue: unknown): {
  graph: Record<string, ComfyWorkflowNode>
  titles: Record<string, string>
} {
  const root = asObject(rawValue)
  if (!root) throw new Error('워크플로 JSON 루트는 객체여야 합니다.')
  const promptWrapper = asObject(root.prompt)
  const wrapperLooksLikeGraph = promptWrapper && Object.values(promptWrapper).every((value) => {
    const node = asObject(value)
    return node && typeof node.class_type === 'string' && asObject(node.inputs)
  })
  const rawGraph = wrapperLooksLikeGraph ? promptWrapper : root
  if (Array.isArray(rawGraph.nodes)) {
    throw new Error('ComfyUI의 "Save (API Format)"으로 내보낸 워크플로 JSON을 선택해 주세요.')
  }
  const entries = Object.entries(rawGraph)
  if (entries.length === 0 || entries.length > MAX_NODES) {
    throw new Error(`워크플로 노드는 1~${MAX_NODES}개여야 합니다.`)
  }
  const graph: Record<string, ComfyWorkflowNode> = {}
  const titles: Record<string, string> = {}
  for (const [nodeId, rawNodeValue] of entries) {
    if (!NODE_ID_RE.test(nodeId)) throw new Error(`워크플로 노드 ID가 올바르지 않습니다: ${nodeId}`)
    const rawNode = asObject(rawNodeValue)
    const classType = rawNode?.class_type
    const rawInputs = asObject(rawNode?.inputs)
    if (!rawNode || typeof classType !== 'string' || !NODE_CLASS_RE.test(classType) || !rawInputs) {
      throw new Error(`ComfyUI API 노드 형식이 올바르지 않습니다: ${nodeId}`)
    }
    const inputEntries = Object.entries(rawInputs)
    if (inputEntries.length > MAX_INPUTS_PER_NODE) throw new Error(`${classType} 노드 입력이 너무 많습니다.`)
    const inputs: Record<string, ComfyWorkflowInputValue> = {}
    for (const [input, value] of inputEntries) {
      if (!INPUT_NAME_RE.test(input)) throw new Error(`${classType} 노드 입력 이름이 올바르지 않습니다.`)
      const sanitized = sanitizeInputValue(value)
      if (BOUNDED_LITERAL_INPUTS.has(input) && Array.isArray(sanitized)) {
        throw new Error(`${classType}.${input}은 Agent 제한을 적용할 수 있는 직접 값이어야 합니다.`)
      }
      if (
        isModelLoaderInputName(input) &&
        (typeof sanitized !== 'string' || !sanitized.toLowerCase().endsWith('.safetensors'))
      ) {
        throw new Error(`${classType}.${input}은 등록 가능한 SafeTensors 모델 값을 직접 사용해야 합니다.`)
      }
      inputs[input] = sanitized
    }
    graph[nodeId] = { class_type: classType, inputs }
    const meta = asObject(rawNode._meta)
    if (typeof meta?.title === 'string' && meta.title.length <= 200) titles[nodeId] = meta.title
  }
  const nodeIds = new Set(Object.keys(graph))
  for (const node of Object.values(graph)) {
    for (const value of Object.values(node.inputs)) {
      if (
        Array.isArray(value) && value.length === 2 && typeof value[0] === 'string' &&
        typeof value[1] === 'number' && Number.isInteger(value[1]) &&
        !nodeIds.has(value[0])
      ) throw new Error(`존재하지 않는 노드 연결이 있습니다: ${value[0]}`)
    }
  }
  if (!Object.values(graph).some((node) => node.class_type === 'SaveImage')) {
    throw new Error('결과를 Aiso에 전달하려면 워크플로에 기본 SaveImage 노드가 있어야 합니다.')
  }
  const saveImageCount = Object.values(graph).filter((node) => node.class_type === 'SaveImage').length
  if (saveImageCount > MAX_SAVE_IMAGE_NODES) {
    throw new Error(`Agent 워크플로의 SaveImage 출력은 최대 ${MAX_SAVE_IMAGE_NODES}개까지 허용됩니다.`)
  }
  if (new Set(Object.values(graph).map((node) => node.class_type)).size > MAX_NODE_CLASSES) {
    throw new Error(`Agent 워크플로의 서로 다른 노드 종류는 최대 ${MAX_NODE_CLASSES}개까지 허용됩니다.`)
  }
  for (const node of Object.values(graph)) {
    for (const [input, value] of Object.entries(node.inputs)) {
      if (input === 'batch_size' && value !== 1) {
        throw new Error('Agent 워크플로의 batch_size는 1이어야 합니다.')
      }
      if ((input === 'width' || input === 'height') && typeof value === 'number') {
        if (!Number.isInteger(value) || value < 256 || value > 2048 || value % 64 !== 0) {
          throw new Error(`Agent 워크플로의 ${input}은 256~2048 범위의 64 배수여야 합니다.`)
        }
      }
      if (input === 'steps' && typeof value === 'number' && (!Number.isInteger(value) || value < 1 || value > 60)) {
        throw new Error('Agent 워크플로의 steps는 1~60 범위여야 합니다.')
      }
      if ((input === 'cfg' || input === 'guidance') && typeof value === 'number' && (value < 0 || value > 30)) {
        throw new Error(`Agent 워크플로의 ${input}은 0~30 범위여야 합니다.`)
      }
    }
  }
  return { graph, titles }
}

function suggestedDefaults(
  graph: Record<string, ComfyWorkflowNode>,
  bindings: Record<ComfyWorkflowBindingTarget, ComfyWorkflowInputRef[]>
): Partial<ComfyGenerationDefaults> {
  const result: Partial<ComfyGenerationDefaults> = {}
  const targets: Array<keyof ComfyGenerationDefaults> = ['width', 'height', 'steps', 'cfg', 'sampler', 'scheduler']
  for (const target of targets) {
    const ref = bindings[target][0]
    if (!ref) continue
    const value = graph[ref.nodeId]?.inputs[ref.input]
    if ((target === 'sampler' || target === 'scheduler') && typeof value === 'string') result[target] = value
    if (target === 'cfg' && typeof value === 'number') result.cfg = value
    if ((target === 'width' || target === 'height' || target === 'steps') && typeof value === 'number') {
      result[target] = Math.round(value)
    }
  }
  return result
}

export function parseComfyWorkflowTemplate(
  rawValue: unknown,
  sourcePath: string,
  importedAt = Date.now()
): ParsedComfyWorkflowTemplate {
  const encoded = Buffer.byteLength(JSON.stringify(rawValue), 'utf8')
  if (encoded > MAX_TEMPLATE_BYTES) throw new Error('워크플로 JSON은 1MB 이하여야 합니다.')
  const { graph, titles } = sanitizeGraph(rawValue)
  const bindings = inferBindings(graph, titles)
  if (bindings.positivePrompt.length === 0) {
    throw new Error('주 프롬프트 입력을 찾지 못했습니다. 프롬프트 입력값을 {{prompt}}로 지정해 다시 내보내 주세요.')
  }
  if (bindings.filenamePrefix.length === 0) {
    throw new Error('기본 SaveImage 노드의 filename_prefix 입력을 찾지 못했습니다.')
  }
  const sourceFileName = basename(sourcePath)
  if (!sourceFileName || sourceFileName.length > 200 || /[\u0000-\u001f]/.test(sourceFileName)) {
    throw new Error('워크플로 파일 이름이 올바르지 않습니다.')
  }
  return {
    // Asset bindings are resolved when this template is attached to a model
    // profile.  This keeps import-first and model-first flows both possible.
    template: makeTemplate(graph, bindings, [], sourceFileName, importedAt),
    suggestedDefaults: suggestedDefaults(graph, bindings)
  }
}

function normalizedModelName(value: string): string {
  return value.replace(/\\/g, '/').replace(/^\.\//, '').toLocaleLowerCase('en-US')
}

function matchingAssetForLoader(value: string, assets: readonly ComfyModelAsset[]): ComfyModelAsset | null {
  const expected = normalizedModelName(value)
  const relativeMatches = assets.filter((asset) => normalizedModelName(asset.relativePath) === expected)
  if (relativeMatches.length === 1) return relativeMatches[0]
  if (relativeMatches.length > 1) return null
  const comfyMatches = assets.filter((asset) => normalizedModelName(asset.comfyName) === expected)
  return comfyMatches.length === 1 ? comfyMatches[0] : null
}

/**
 * Rebuild the immutable workflow contract from its literal loader values and
 * the profile's current assets.  Ambiguous or absent matches stay unbound and
 * are consequently not Agent-ready; they are never guessed by basename.
 */
export function bindComfyWorkflowTemplateAssets(
  template: ComfyWorkflowTemplate,
  assets: readonly ComfyModelAsset[]
): ComfyWorkflowTemplate {
  const assetBindings: ComfyWorkflowAssetBinding[] = []
  for (const ref of workflowAssetRefs(template.graph)) {
    const value = template.graph[ref.nodeId]?.inputs[ref.input]
    if (typeof value !== 'string') continue
    const asset = matchingAssetForLoader(value, assets)
    if (!asset) continue
    assetBindings.push({
      nodeId: ref.nodeId,
      input: ref.input,
      assetId: asset.id,
      sha256: asset.sha256,
      relativePath: asset.relativePath,
      comfyName: value
    })
  }
  return makeTemplate(
    template.graph,
    template.bindings,
    assetBindings,
    template.sourceFileName,
    template.importedAt
  )
}

export function parseStoredComfyWorkflowTemplate(value: unknown): ComfyWorkflowTemplate | null {
  const raw = asObject(value)
  if (!raw || raw.schemaVersion !== 1 || typeof raw.sourceFileName !== 'string') return null
  try {
    const { graph } = sanitizeGraph(raw.graph)
    const rawBindings = asObject(raw.bindings)
    if (
      !rawBindings ||
      Object.keys(rawBindings).length !== COMFY_WORKFLOW_BINDING_TARGETS.length ||
      COMFY_WORKFLOW_BINDING_TARGETS.some((target) => !(target in rawBindings)) ||
      !raw.sourceFileName || raw.sourceFileName.length > 200 ||
      /[\\/\u0000-\u001f]/.test(raw.sourceFileName)
    ) return null
    const bindings = emptyBindings()
    for (const target of COMFY_WORKFLOW_BINDING_TARGETS) {
      const refs = rawBindings[target]
      if (!Array.isArray(refs) || refs.length > MAX_NODES) return null
      for (const rawRef of refs) {
        const ref = asObject(rawRef)
        if (
          !ref || typeof ref.nodeId !== 'string' || typeof ref.input !== 'string' ||
          !NODE_ID_RE.test(ref.nodeId) || !INPUT_NAME_RE.test(ref.input) ||
          graph[ref.nodeId]?.inputs[ref.input] === undefined
        ) return null
        addBinding(bindings, target, ref.nodeId, ref.input)
      }
    }
    if (bindings.positivePrompt.length === 0 || bindings.filenamePrefix.length === 0) return null
    const isLegacy = raw.assetBindings === undefined
    const assetBindings = parseAssetBindings(raw.assetBindings, graph, { allowMissing: isLegacy })
    if (assetBindings === null) return null
    const sha256 = isLegacy
      ? legacyContentHash(graph, bindings)
      : contentHash(graph, bindings, assetBindings)
    const id = `user.${sha256.slice(0, 20)}.txt2img.v1`
    if (
      typeof raw.id !== 'string' || raw.id !== id ||
      typeof raw.sha256 !== 'string' || raw.sha256 !== sha256 ||
      !Number.isFinite(raw.importedAt)
    ) return null
    // Legacy records intentionally come back as an unbound template.  The
    // model registry will immediately re-bind it against registered assets
    // before it is considered ready or written again.
    if (isLegacy) {
      return {
        schemaVersion: 1,
        id,
        sourceFileName: raw.sourceFileName,
        sha256,
        graph,
        bindings,
        assetBindings: [],
        importedAt: Number(raw.importedAt)
      }
    }
    return makeTemplate(graph, bindings, assetBindings, raw.sourceFileName, Number(raw.importedAt))
  } catch {
    return null
  }
}

export const COMFY_WORKFLOW_TEMPLATE_MAX_BYTES = MAX_TEMPLATE_BYTES
