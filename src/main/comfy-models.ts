import { app, BrowserWindow, dialog } from 'electron'
import { createHash, randomUUID } from 'crypto'
import {
  createReadStream,
  createWriteStream,
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  rmSync,
  statfsSync,
  statSync,
  writeFileSync
} from 'fs'
import { basename, dirname, extname, isAbsolute, join, relative, resolve, sep } from 'path'
import { Transform } from 'stream'
import { pipeline } from 'stream/promises'
import { isDeepStrictEqual } from 'util'
import { appDataFrozen } from './appdata-guard'
import {
  COMFY_WORKFLOW_TEMPLATE_MAX_BYTES,
  bindComfyWorkflowTemplateAssets,
  parseComfyWorkflowTemplate,
  parseStoredComfyWorkflowTemplate
} from './comfy-workflow-template'
import {
  inferComfyModelAssetFromHeader,
  inferComfyModelFamilyFromSafeTensors,
  readSafeTensorsHeader
} from './comfy-model-analysis'
import {
  COMFY_ASSET_KINDS,
  COMFY_ASSET_SLOTS,
  COMFY_ASSET_SLOT_LABELS,
  COMFY_MODEL_CAPABILITIES,
  COMFY_MODEL_FAMILIES,
  DEFAULT_COMFY_GENERATION,
  getComfyAgentReadiness,
  getComfyWorkflowAssetBindingStatus,
  getComfyGenerationDefaults,
  getComfyRequiredSlots,
  type ComfyAutomaticAssetKind,
  type ComfyAssetKind,
  type ComfyAssetSlot,
  type ComfyGenerationDefaults,
  type ComfyModelAsset,
  type ComfyModelCapability,
  type ComfyModelFamily,
  type ComfyModelImportProgress,
  type ComfyModelImportRequest,
  type ComfyModelImportResult,
  type ComfyModelProfile,
  type ComfyModelProfilePatch,
  type ComfyModelRegistry
} from '../shared/comfy-model'

const REGISTRY_VERSION = 1 as const
// Pickle 기반 ckpt/pt/pth/bin은 로드 시 코드 실행 위험이 있고 GGUF는 이 가져오기 범위가 아니다.
const MODEL_EXTENSIONS = new Set(['.safetensors'])
const KIND_SET = new Set<string>(COMFY_ASSET_KINDS)
const SLOT_SET = new Set<string>(COMFY_ASSET_SLOTS)
const FAMILY_SET = new Set<string>(COMFY_MODEL_FAMILIES)
const CAPABILITY_SET = new Set<string>(COMFY_MODEL_CAPABILITIES)
const MAX_REGISTRY_BACKUP_BYTES = 16 * 1024 * 1024
const STALE_PARTIAL_AGE_MS = 24 * 60 * 60 * 1000
const MAX_PARTIAL_SCAN_ENTRIES = 10_000
const MAX_PARTIAL_SCAN_DEPTH = 8
const AISO_PARTIAL_FILE_RE = /\.safetensors\.[A-Za-z0-9_-]{1,100}\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.partial$/i

const DESTINATION_BY_KIND: Record<ComfyAutomaticAssetKind, string> = {
  checkpoint: 'checkpoints',
  diffusion_model: 'diffusion_models',
  text_encoder: 'text_encoders',
  vae: 'vae',
  lora: 'loras',
  controlnet: 'controlnet'
}

const SLOT_KIND: Record<ComfyAssetSlot, ComfyAutomaticAssetKind> = {
  checkpoint: 'checkpoint',
  diffusion_model: 'diffusion_model',
  clip_l: 'text_encoder',
  t5xxl: 'text_encoder',
  qwen3: 'text_encoder',
  vae: 'vae',
  lora: 'lora',
  controlnet: 'controlnet'
}

const SINGLETON_SLOTS = new Set<ComfyAssetSlot>([
  'checkpoint',
  'diffusion_model',
  'clip_l',
  't5xxl',
  'qwen3',
  'vae'
])

type ProgressCallback = (progress: ComfyModelImportProgress) => void
type InstallPathCurrentCheck = () => boolean
type JsonObject = Record<string, unknown>

class ComfyModelImportCancelledError extends Error {
  constructor() {
    super('모델 파일 가져오기가 취소되었습니다.')
  }
}

interface PlannedImportAsset {
  source: string
  fileName: string
  size: number
  kind: ComfyAssetKind
  slot?: ComfyAssetSlot
  agentFamilies: readonly ComfyModelFamily[]
  /** ComfyUI/models 기준 POSIX 상대 폴더. */
  destinationFolderName: string
}

let importBusy = false
let registryRevision = 0
let registryRecovery: { fingerprint: string; error: Error } | undefined
let activeImportOperationId: string | undefined
let activeImportCancelled = false

function assertInstallPathCurrent(check?: InstallPathCurrentCheck): void {
  if (check?.() === false) {
    throw new Error('ComfyUI 설치 폴더가 작업 중 변경되었습니다. 현재 설치 폴더를 확인한 뒤 다시 시도해 주세요.')
  }
}

function assertRegistryCurrent(revision: number): void {
  if (registryRevision !== revision) {
    throw new Error('모델 목록이 다른 작업으로 변경되었습니다. 최신 목록을 확인한 뒤 다시 시도해 주세요.')
  }
}

function assertImportNotCancelled(operationId: string): void {
  if (activeImportOperationId === operationId && activeImportCancelled) {
    throw new ComfyModelImportCancelledError()
  }
}

/** IPC may call this while a large SafeTensors copy/hash is in progress. */
export function cancelComfyModelImport(operationId: unknown): boolean {
  if (typeof operationId !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(operationId)) return false
  if (activeImportOperationId !== operationId) return false
  activeImportCancelled = true
  return true
}

function registryPath(): string {
  return join(app.getPath('userData'), 'comfy-models.json')
}

function emptyRegistry(): ComfyModelRegistry {
  return { schemaVersion: REGISTRY_VERSION, profiles: [] }
}

function registryFingerprint(file: string): string {
  const stat = statSync(file)
  return `${stat.size}:${stat.mtimeMs}`
}

function registryRecoveryError(file: string, cause: unknown): Error {
  let fingerprint = 'unavailable'
  try {
    fingerprint = registryFingerprint(file)
    if (registryRecovery?.fingerprint === fingerprint) return registryRecovery.error
  } catch {
    // The caller will surface the original read error below.
  }

  let recoveryPath = ''
  try {
    const stat = statSync(file)
    if (stat.isFile() && stat.size <= MAX_REGISTRY_BACKUP_BYTES) {
      recoveryPath = `${file}.corrupt-${Date.now()}-${randomUUID()}.json`
      copyFileSync(file, recoveryPath)
    }
  } catch (backupError) {
    console.error('[comfy-models] failed to preserve corrupt registry:', backupError)
  }
  const error = new Error(
    recoveryPath
      ? `ComfyUI 모델 목록 파일이 손상되어 변경을 중단했습니다. 원본 사본을 보존했습니다: ${recoveryPath}`
      : 'ComfyUI 모델 목록 파일이 손상되어 변경을 중단했습니다. 파일을 복구하거나 설정의 모델 목록 초기화를 사용해 주세요.'
  )
  console.error('[comfy-models] registry recovery required:', cause)
  registryRecovery = { fingerprint, error }
  return error
}

function asObject(value: unknown): JsonObject | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as JsonObject)
    : null
}

function finiteNumber(value: unknown, fallback: number, min: number, max: number): number {
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.min(max, Math.max(min, value))
    : fallback
}

function positiveInteger(value: unknown, fallback: number, min: number, max: number): number {
  return Math.round(finiteNumber(value, fallback, min, max))
}

function cleanName(value: unknown): string {
  if (typeof value !== 'string') throw new Error('모델 이름이 필요합니다.')
  const name = value.trim()
  if (!name || name.length > 120 || /[\u0000-\u001f\u007f]/.test(name)) {
    throw new Error('모델 이름은 1~120자의 일반 텍스트여야 합니다.')
  }
  return name
}

function cleanFamily(value: unknown): ComfyModelFamily {
  if (typeof value !== 'string' || !FAMILY_SET.has(value)) {
    throw new Error('지원하지 않는 모델 계열입니다.')
  }
  return value as ComfyModelFamily
}

function cleanCapabilities(value: unknown): ComfyModelCapability[] {
  if (value === undefined) return ['txt2img']
  if (!Array.isArray(value)) throw new Error('모델 작업 유형 형식이 올바르지 않습니다.')
  const capabilities = [...new Set(value.filter((item): item is string => typeof item === 'string'))]
  if (capabilities.some((item) => !CAPABILITY_SET.has(item))) {
    throw new Error('지원하지 않는 모델 작업 유형이 포함되어 있습니다.')
  }
  return (capabilities.length ? capabilities : ['txt2img']) as ComfyModelCapability[]
}

function cleanTags(value: unknown): string[] {
  if (value === undefined) return []
  if (!Array.isArray(value)) throw new Error('모델 태그 형식이 올바르지 않습니다.')
  const tags = value
    .filter((item): item is string => typeof item === 'string')
    .map((item) => item.trim())
    .filter(Boolean)
  return [...new Set(tags)].slice(0, 20).map((tag) => tag.slice(0, 48))
}

function cleanDefaults(
  value: unknown,
  base: ComfyGenerationDefaults = DEFAULT_COMFY_GENERATION
): ComfyGenerationDefaults {
  const raw = asObject(value) ?? {}
  const sampler = typeof raw.sampler === 'string' && raw.sampler.trim()
    ? raw.sampler.trim().slice(0, 80)
    : base.sampler
  const scheduler = typeof raw.scheduler === 'string' && raw.scheduler.trim()
    ? raw.scheduler.trim().slice(0, 80)
    : base.scheduler
  const width = positiveInteger(raw.width, base.width, 256, 2048)
  const height = positiveInteger(raw.height, base.height, 256, 2048)
  if (width % 64 !== 0 || height % 64 !== 0) {
    throw new Error('기본 이미지 너비와 높이는 256~2048 범위의 64 배수여야 합니다.')
  }
  return {
    width,
    height,
    steps: positiveInteger(raw.steps, base.steps, 1, 60),
    cfg: finiteNumber(raw.cfg, base.cfg, 0, 30),
    ...(sampler ? { sampler } : {}),
    ...(scheduler ? { scheduler } : {})
  }
}

function cleanPriority(value: unknown, fallback = 0): number {
  return positiveInteger(value, fallback, -100, 100)
}

function cleanWorkflowTemplate(value: unknown, family: ComfyModelFamily): string {
  if (value === undefined || value === '') return `${family}.txt2img.v1`
  if (typeof value !== 'string') throw new Error('워크플로 템플릿 형식이 올바르지 않습니다.')
  const template = value.trim()
  if (!template || template.length > 120 || !/^[a-zA-Z0-9._-]+$/.test(template)) {
    throw new Error('워크플로 템플릿 ID가 올바르지 않습니다.')
  }
  return template
}

function cleanOperationId(value: unknown): string {
  if (typeof value !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(value)) {
    throw new Error('모델 가져오기 작업 ID가 올바르지 않습니다.')
  }
  return value
}

function normalizeImportRequest(value: unknown): ComfyModelImportRequest {
  const raw = asObject(value)
  if (!raw) throw new Error('모델 가져오기 요청 형식이 올바르지 않습니다.')
  if (raw.family !== undefined || raw.workflowTemplateId !== undefined || raw.assetKind !== undefined || raw.assetSlot !== undefined) {
    throw new Error('모델 구조와 파일 역할은 연결한 파일을 Aiso가 분석해 내부적으로 결정합니다.')
  }
  const profileId = raw.profileId === undefined
    ? undefined
    : typeof raw.profileId === 'string' && /^[a-zA-Z0-9_-]{1,100}$/.test(raw.profileId)
      ? raw.profileId
      : (() => { throw new Error('모델 프로필 ID가 올바르지 않습니다.') })()
  return {
    operationId: cleanOperationId(raw.operationId),
    ...(profileId ? { profileId } : {}),
    name: cleanName(raw.name),
    capabilities: cleanCapabilities(raw.capabilities),
    tags: cleanTags(raw.tags)
  }
}

function validRelativePath(value: string): boolean {
  if (!value || isAbsolute(value) || value.includes('\\')) return false
  const parts = value.split('/')
  return parts.every((part) => part && part !== '.' && part !== '..')
}

function parseStoredAsset(value: unknown): ComfyModelAsset | null {
  const raw = asObject(value)
  if (!raw) return null
  if (
    typeof raw.id !== 'string' || !raw.id ||
    typeof raw.kind !== 'string' || !KIND_SET.has(raw.kind) ||
    typeof raw.fileName !== 'string' || basename(raw.fileName) !== raw.fileName ||
    typeof raw.comfyName !== 'string' || !validRelativePath(raw.comfyName) ||
    typeof raw.relativePath !== 'string' || !validRelativePath(raw.relativePath) ||
    typeof raw.size !== 'number' || !Number.isSafeInteger(raw.size) || raw.size < 1 || raw.size > 2 ** 50 ||
    typeof raw.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(raw.sha256) ||
    typeof raw.importedAt !== 'number' || !Number.isFinite(raw.importedAt)
  ) return null
  const kind = raw.kind as ComfyAssetKind
  let slot: ComfyAssetSlot | undefined
  if (raw.slot !== undefined) {
    if (typeof raw.slot !== 'string' || !SLOT_SET.has(raw.slot)) return null
    slot = raw.slot as ComfyAssetSlot
    if (SLOT_KIND[slot] !== kind) return null
  }
  let agentFamilies: ComfyModelFamily[] | undefined
  if (raw.agentFamilies !== undefined) {
    if (!Array.isArray(raw.agentFamilies) || raw.agentFamilies.some((family) => (
      typeof family !== 'string' || !FAMILY_SET.has(family)
    ))) return null
    agentFamilies = [...new Set(raw.agentFamilies)] as ComfyModelFamily[]
  }
  if (kind === 'custom') {
    // 직접 연결 파일은 어떤 loader 폴더인지 Aiso가 추측하지 않는다. 다만 models
    // 루트 바로 아래에는 둘 수 없고, Agent 슬롯·호환 표시는 절대 부여하지 않는다.
    if (
      slot !== undefined ||
      agentFamilies === undefined ||
      agentFamilies.length !== 0 ||
      !raw.relativePath.includes('/') ||
      !raw.relativePath.endsWith(`/${raw.fileName}`)
    ) return null
  } else if (!raw.relativePath.startsWith(`${DESTINATION_BY_KIND[kind]}/`)) {
    return null
  }
  return {
    id: raw.id,
    kind,
    ...(slot ? { slot } : {}),
    ...(agentFamilies === undefined ? {} : { agentFamilies }),
    fileName: raw.fileName,
    comfyName: raw.comfyName,
    relativePath: raw.relativePath,
    size: raw.size,
    sha256: raw.sha256,
    importedAt: raw.importedAt
  }
}

function parseStoredProfile(value: unknown): ComfyModelProfile | null {
  const raw = asObject(value)
  if (!raw || typeof raw.id !== 'string' || !raw.id) return null
  try {
    const family = cleanFamily(raw.family)
    const parsedAssets = Array.isArray(raw.assets) ? raw.assets.map(parseStoredAsset) : []
    if (parsedAssets.some((asset) => asset === null)) return null
    const assets = parsedAssets as ComfyModelAsset[]
    const parsedWorkflowTemplate = raw.workflowTemplate === undefined
      ? undefined
      : parseStoredComfyWorkflowTemplate(raw.workflowTemplate)
    if (raw.workflowTemplate !== undefined && !parsedWorkflowTemplate) return null
    // Re-resolve every literal loader value against this profile's registered
    // assets.  Legacy records are migrated in memory; ambiguous matches stay
    // unbound and cannot become Agent-ready.
    const workflowTemplate = parsedWorkflowTemplate
      ? bindComfyWorkflowTemplateAssets(parsedWorkflowTemplate, assets)
      : undefined
    // 직접 연결 파일은 모델 계열을 추측하지 않는다. 다만 사용자가 검증된 API
    // 워크플로를 명시적으로 연결했다면 그 고정 그래프를 통해서만 Agent가 사용한다.
    const hasDirectAsset = assets.some((asset) => asset.kind === 'custom')
    const effectiveFamily: ComfyModelFamily = hasDirectAsset ? 'custom' : family
    const createdAt = typeof raw.createdAt === 'number' && Number.isFinite(raw.createdAt)
      ? raw.createdAt
      : Date.now()
    const updatedAt = typeof raw.updatedAt === 'number' && Number.isFinite(raw.updatedAt)
      ? raw.updatedAt
      : createdAt
    const storedTemplateId = cleanWorkflowTemplate(raw.workflowTemplateId, effectiveFamily)
    const profile: ComfyModelProfile = {
      id: raw.id,
      name: cleanName(raw.name),
      family: effectiveFamily,
      capabilities: cleanCapabilities(raw.capabilities),
      tags: cleanTags(raw.tags),
      assets,
      workflowTemplateId: workflowTemplate?.id ?? (hasDirectAsset ? 'custom.txt2img.v1' : storedTemplateId),
      ...(workflowTemplate ? { workflowTemplate } : {}),
      defaults: cleanDefaults(raw.defaults),
      agentEnabled: false,
      priority: cleanPriority(raw.priority),
      createdAt,
      updatedAt
    }
    return {
      ...profile,
      // A stale/legacy template may still be displayed and edited, but Agent
      // must never trust it until all model loader inputs are explicitly bound.
      agentEnabled: raw.agentEnabled === true && getComfyAgentReadiness(profile).ready
    }
  } catch {
    return null
  }
}

function loadRegistry(): ComfyModelRegistry {
  const file = registryPath()
  if (!existsSync(file)) {
    registryRecovery = undefined
    return emptyRegistry()
  }
  try {
    const fingerprint = registryFingerprint(file)
    if (registryRecovery?.fingerprint === fingerprint) throw registryRecovery.error
    const raw = asObject(JSON.parse(readFileSync(file, 'utf-8')))
    if (!raw || raw.schemaVersion !== REGISTRY_VERSION || !Array.isArray(raw.profiles)) {
      throw new Error('지원하지 않는 레지스트리 형식')
    }
    const profiles = raw.profiles.map(parseStoredProfile)
    if (profiles.some((profile) => profile === null)) {
      throw new Error('모델 프로필 중 하나 이상이 손상되었습니다.')
    }
    registryRecovery = undefined
    return {
      schemaVersion: REGISTRY_VERSION,
      profiles: profiles as ComfyModelProfile[]
    }
  } catch (error) {
    if (error instanceof Error && registryRecovery?.error === error) throw error
    throw registryRecoveryError(file, error)
  }
}

function saveRegistry(registry: ComfyModelRegistry): void {
  if (appDataFrozen()) throw new Error('공장초기화 진행 중에는 모델 목록을 변경할 수 없습니다.')
  const file = registryPath()
  mkdirSync(dirname(file), { recursive: true })
  const temporary = `${file}.${randomUUID()}.tmp`
  try {
    writeFileSync(temporary, JSON.stringify(registry, null, 2), { encoding: 'utf-8', flag: 'wx' })
    renameSync(temporary, file)
    registryRevision += 1
    registryRecovery = undefined
  } finally {
    rmSync(temporary, { force: true })
  }
}

function resolvePortableRoot(selectedPath: string): string | null {
  if (!selectedPath.trim()) return null
  const selected = resolve(selectedPath.trim())
  const candidates = [selected]
  if (basename(selected).toLowerCase() === 'comfyui') candidates.push(dirname(selected))
  for (const root of candidates) {
    if (
      existsSync(join(root, 'python_embeded', 'python.exe')) &&
      existsSync(join(root, 'ComfyUI', 'main.py'))
    ) return root
  }
  return null
}

function resolveModelsRoot(installPath: string): string {
  if (!installPath.trim()) {
    throw new Error(
      'ComfyUI 설치 경로가 없어 모델을 가져올 수 없습니다. 외부 서버 연결만 사용하는 경우에는 파일을 직접 해당 서버에 설치해야 합니다.'
    )
  }
  const portableRoot = resolvePortableRoot(installPath)
  if (!portableRoot) {
    throw new Error('등록된 ComfyUI Windows Portable 폴더가 올바르지 않습니다. 설정에서 설치 폴더를 다시 선택해 주세요.')
  }
  const modelsRoot = join(portableRoot, 'ComfyUI', 'models')
  return modelsRoot
}

function cleanupStalePartials(modelsRoot: string, olderThanMs: number): number {
  const cutoff = Date.now() - olderThanMs
  const root = realpathSync(modelsRoot)
  const directories: Array<{ path: string; depth: number }> = [{ path: root, depth: 0 }]
  let removed = 0
  let inspected = 0
  while (directories.length > 0) {
    const { path: directory, depth } = directories.pop()!
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      inspected += 1
      if (inspected > MAX_PARTIAL_SCAN_ENTRIES) {
        console.warn('[comfy-models] stale partial cleanup stopped at scan limit')
        return removed
      }
      const target = join(directory, entry.name)
      let stat
      try {
        stat = lstatSync(target)
      } catch {
        continue
      }
      // Never follow junctions/symlinks while clearing stale transient files.
      if (stat.isSymbolicLink()) continue
      if (stat.isDirectory()) {
        if (depth < MAX_PARTIAL_SCAN_DEPTH) directories.push({ path: target, depth: depth + 1 })
        continue
      }
      if (
        stat.isFile() &&
        AISO_PARTIAL_FILE_RE.test(entry.name) &&
        stat.mtimeMs <= cutoff
      ) {
        try {
          rmSync(target, { force: true })
          removed += 1
        } catch (error) {
          console.warn('[comfy-models] stale partial cleanup failed:', target, error)
        }
      }
    }
  }
  return removed
}

/**
 * Removes only Aiso's UUID-tagged, old partial imports.  Call after loading a
 * configured Portable install; actual model files and arbitrary .partial files
 * are deliberately preserved.
 */
export function cleanupStaleComfyModelPartials(
  installPath: string,
  olderThanMs = STALE_PARTIAL_AGE_MS
): number {
  if (!installPath.trim() || !Number.isFinite(olderThanMs) || olderThanMs < 0) return 0
  try {
    return cleanupStalePartials(resolveModelsRoot(installPath), olderThanMs)
  } catch (error) {
    console.warn('[comfy-models] stale partial cleanup skipped:', error)
    return 0
  }
}

function assertInside(root: string, target: string): void {
  const rel = relative(resolve(root), resolve(target))
  if (rel === '' || (!rel.startsWith(`..${sep}`) && rel !== '..' && !isAbsolute(rel))) return
  throw new Error('ComfyUI 모델 폴더 밖의 경로는 사용할 수 없습니다.')
}

function assertDestinationDirectory(modelsRoot: string, directory: string): void {
  assertInside(modelsRoot, directory)
  const comfyRoot = dirname(modelsRoot)
  const realComfyRoot = realpathSync(comfyRoot)
  const pathParts = relative(comfyRoot, directory).split(sep).filter(Boolean)
  let current = comfyRoot
  for (const part of pathParts) {
    current = join(current, part)
    if (!existsSync(current)) mkdirSync(current)
    const linkStat = lstatSync(current)
    if (!linkStat.isDirectory() || linkStat.isSymbolicLink()) {
      throw new Error('심볼릭 링크, 연결 지점 또는 일반 폴더가 아닌 모델 경로에는 가져올 수 없습니다.')
    }
    const realCurrent = realpathSync(current)
    assertInside(realComfyRoot, realCurrent)
  }
}

function manualDestinationRelativePath(modelsRoot: string, selectedDirectory: string): string {
  const destination = resolve(selectedDirectory)
  assertInside(modelsRoot, destination)
  const relativeDirectory = relative(resolve(modelsRoot), destination).split(sep).join('/')
  if (!validRelativePath(relativeDirectory)) {
    throw new Error('직접 연결 위치는 ComfyUI/models 아래의 하위 폴더여야 합니다.')
  }
  // 선택 대화상자의 경로가 junction/symlink를 따라 models 밖으로 나가지 않는지 다시 확인한다.
  assertDestinationDirectory(modelsRoot, destination)
  return relativeDirectory
}

async function chooseManualDestination(
  owner: BrowserWindow,
  modelsRoot: string,
  fileName: string
): Promise<string | null> {
  const choice = await dialog.showMessageBox(owner, {
    type: 'info',
    title: '직접 연결 위치 선택',
    message: `${fileName}의 모델 역할을 Aiso가 안전하게 확인하지 못했습니다.`,
    detail:
      '모델 배포 문서에 지정된 ComfyUI/models 하위 실제 폴더를 선택하면, 파일을 그 위치에 직접 연결합니다. ' +
      '새 하위 폴더가 필요하면 다음 창의 새 폴더 기능으로 만든 뒤 선택하세요. ' +
      'Aiso는 이 파일의 역할을 추측하지 않으며 Agent 자동 선택에도 사용하지 않습니다.',
    buttons: ['폴더 선택', '가져오기 취소'],
    defaultId: 0,
    cancelId: 1,
    noLink: true
  })
  if (choice.response !== 0) return null

  const selection = await dialog.showOpenDialog(owner, {
    title: 'ComfyUI/models 하위 폴더 선택',
    defaultPath: modelsRoot,
    properties: ['openDirectory', 'createDirectory', 'promptToCreate']
  })
  if (selection.canceled || selection.filePaths.length === 0) return null
  return manualDestinationRelativePath(modelsRoot, selection.filePaths[0])
}

function assertAvailableDiskSpace(directory: string, selectedBytes: number): void {
  const disk = statfsSync(directory)
  const available = Number(disk.bavail) * Number(disk.bsize)
  const safetyMargin = Math.max(256 * 1024 ** 2, Math.ceil(selectedBytes * 0.05))
  const required = selectedBytes + safetyMargin
  if (!Number.isFinite(available) || available < required) {
    const needGiB = (required / 1024 ** 3).toFixed(2)
    const freeGiB = Number.isFinite(available) ? (available / 1024 ** 3).toFixed(2) : '확인 불가'
    throw new Error(`모델 복사 공간이 부족합니다. 필요 약 ${needGiB} GB, 사용 가능 ${freeGiB} GB입니다.`)
  }
}

function validateSourceFile(source: string): { fileName: string; size: number } {
  const stat = lstatSync(source)
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('일반 모델 파일만 가져올 수 있습니다.')
  if (!Number.isSafeInteger(stat.size) || stat.size < 1 || stat.size > 2 ** 50) {
    throw new Error('모델 파일 크기가 올바르지 않습니다.')
  }
  const fileName = basename(source)
  const extension = extname(fileName).toLowerCase()
  if (!MODEL_EXTENSIONS.has(extension)) {
    throw new Error(`${fileName}: 지원하는 모델 파일 형식이 아닙니다.`)
  }
  if (
    !fileName || fileName === '.' || fileName === '..' ||
    /[<>:"/\\|?*\u0000-\u001f]/.test(fileName) ||
    /[. ]$/.test(fileName)
  ) throw new Error(`${fileName}: Windows에서 안전하게 사용할 수 없는 파일 이름입니다.`)
  const stem = fileName.slice(0, fileName.length - extension.length).toUpperCase()
  if (/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/.test(stem)) {
    throw new Error(`${fileName}: Windows 예약 파일 이름은 사용할 수 없습니다.`)
  }
  return { fileName, size: stat.size }
}

function makeReporter(
  callback: ProgressCallback,
  operationId: string,
  phase: ComfyModelImportProgress['phase'],
  fileName: string,
  totalBytes: number
): (completedBytes: number, force?: boolean) => void {
  let lastSent = 0
  return (completedBytes, force = false) => {
    const now = Date.now()
    if (!force && completedBytes !== totalBytes && now - lastSent < 100) return
    lastSent = now
    callback({ operationId, phase, fileName, completedBytes, totalBytes })
  }
}

async function sha256File(
  path: string,
  totalBytes: number,
  report?: (completedBytes: number, force?: boolean) => void,
  assertNotCancelled?: () => void
): Promise<string> {
  const hash = createHash('sha256')
  let completed = 0
  const stream = createReadStream(path)
  for await (const chunk of stream) {
    assertNotCancelled?.()
    hash.update(chunk as Buffer)
    completed += (chunk as Buffer).length
    report?.(completed)
  }
  report?.(completed, true)
  if (completed !== totalBytes) throw new Error('모델 파일 크기가 읽는 도중 변경되었습니다.')
  return hash.digest('hex')
}

async function copyAndHash(
  source: string,
  destination: string,
  totalBytes: number,
  report: (completedBytes: number, force?: boolean) => void,
  assertNotCancelled?: () => void
): Promise<string> {
  const hash = createHash('sha256')
  let completed = 0
  const observer = new Transform({
    transform(chunk: Buffer, _encoding, callback) {
      try {
        assertNotCancelled?.()
        hash.update(chunk)
        completed += chunk.length
        report(completed)
        callback(null, chunk)
      } catch (error) {
        callback(error as Error)
      }
    }
  })
  await pipeline(
    createReadStream(source),
    observer,
    createWriteStream(destination, { flags: 'wx' })
  )
  report(completed, true)
  if (completed !== totalBytes) throw new Error('모델 파일 크기가 복사 도중 변경되었습니다.')
  return hash.digest('hex')
}

function pathForRelative(modelsRoot: string, relativePath: string): string {
  if (!validRelativePath(relativePath)) throw new Error('저장된 모델 상대 경로가 올바르지 않습니다.')
  const full = resolve(modelsRoot, ...relativePath.split('/'))
  assertInside(modelsRoot, full)
  return full
}

async function findReusableAsset(
  registry: ComfyModelRegistry,
  modelsRoot: string,
  kind: ComfyAssetKind,
  sha256: string,
  size: number,
  expectedRelativePath?: string,
  assertNotCancelled?: () => void
): Promise<ComfyModelAsset | null> {
  for (const profile of registry.profiles) {
    for (const asset of profile.assets) {
      if (asset.kind !== kind || asset.sha256 !== sha256 || asset.size !== size) continue
      if (expectedRelativePath !== undefined && asset.relativePath !== expectedRelativePath) continue
      try {
        const candidate = pathForRelative(modelsRoot, asset.relativePath)
        if (!existsSync(candidate) || lstatSync(candidate).isSymbolicLink() || !statSync(candidate).isFile()) continue
        if (await sha256File(candidate, size, undefined, assertNotCancelled) === sha256) return asset
      } catch {
        // 다른 설치본의 오래된 메타데이터이거나 사라진 파일이면 다음 후보를 확인한다.
      }
    }
  }
  return null
}

function cloneAssetForSlot(
  asset: ComfyModelAsset,
  slot: ComfyAssetSlot | undefined,
  agentFamilies?: readonly ComfyModelFamily[]
): ComfyModelAsset {
  return {
    ...asset,
    id: randomUUID(),
    ...(slot ? { slot } : {}),
    ...(agentFamilies === undefined ? {} : { agentFamilies: [...agentFamilies] }),
    importedAt: Date.now()
  }
}

function ensureSlotAvailable(profile: ComfyModelProfile, asset: ComfyModelAsset): boolean {
  const identical = profile.assets.find((item) => (
    item.kind === asset.kind &&
    item.slot === asset.slot &&
    item.sha256 === asset.sha256 &&
    item.relativePath === asset.relativePath
  ))
  if (identical) {
    if (asset.agentFamilies !== undefined && !isDeepStrictEqual(identical.agentFamilies, asset.agentFamilies)) {
      identical.agentFamilies = [...asset.agentFamilies]
    }
    return false
  }
  if (!asset.slot || !SINGLETON_SLOTS.has(asset.slot)) return true
  const current = profile.assets.find((item) => item.slot === asset.slot)
  if (!current) return true
  throw new Error(`${asset.slot} 슬롯에는 이미 다른 모델 자산이 등록되어 있습니다.`)
}

function assertCompatiblePrimaryAsset(
  profile: ComfyModelProfile,
  asset: Pick<PlannedImportAsset, 'slot'>
): void {
  if (asset.slot !== 'checkpoint' && asset.slot !== 'diffusion_model') return
  const existing = profile.assets.find((item) => item.slot === 'checkpoint' || item.slot === 'diffusion_model')
  if (existing && existing.slot !== asset.slot) {
    throw new Error('한 모델 프로필에는 하나의 생성 모델만 연결할 수 있습니다. 다른 생성 모델은 새 모델로 연결해 주세요.')
  }
}

async function planImportAsset(
  owner: BrowserWindow,
  modelsRoot: string,
  source: string,
  fileName: string,
  size: number,
  allowDirectConnection: boolean
): Promise<PlannedImportAsset | null> {
  const header = readSafeTensorsHeader(source)
  if (!header) {
    throw new Error(`${fileName}: 유효한 SafeTensors 헤더를 확인할 수 없습니다.`)
  }
  const detected = inferComfyModelAssetFromHeader(header)
  if (detected) {
    return {
      source,
      fileName,
      size,
      kind: detected.kind,
      slot: detected.slot,
      agentFamilies: detected.agentFamilies,
      destinationFolderName: DESTINATION_BY_KIND[detected.kind]
    }
  }

  if (!allowDirectConnection) {
    throw new Error(
      `${fileName}: 직접 연결 파일은 현재 자동 워크플로 프로필에 추가할 수 없습니다. ` +
      '새 모델 연결로 별도의 수동 ComfyUI 프로필을 만들어 주세요.'
    )
  }
  const destinationFolderName = await chooseManualDestination(owner, modelsRoot, fileName)
  if (!destinationFolderName) return null
  return {
    source,
    fileName,
    size,
    kind: 'custom',
    agentFamilies: [],
    destinationFolderName
  }
}

function makeImportedAsset(
  plan: PlannedImportAsset,
  sourceHash: string,
  importedAt = Date.now()
): ComfyModelAsset {
  return {
    id: randomUUID(),
    kind: plan.kind,
    ...(plan.slot ? { slot: plan.slot } : {}),
    agentFamilies: [...plan.agentFamilies],
    fileName: plan.fileName,
    comfyName: plan.fileName,
    relativePath: `${plan.destinationFolderName}/${plan.fileName}`,
    size: plan.size,
    sha256: sourceHash,
    importedAt
  }
}

function assertDirectImportProfile(profile: ComfyModelProfile): void {
  if (profile.family !== 'custom') {
    throw new Error(
      '직접 연결 파일은 Agent 자동 워크플로 프로필에 섞을 수 없습니다. ' +
      '새 모델 연결로 별도의 수동 ComfyUI 프로필을 만들어 주세요.'
    )
  }
}

function profileFromRequest(request: ComfyModelImportRequest, previous?: ComfyModelProfile): ComfyModelProfile {
  const now = Date.now()
  // 새 등록은 미확정 상태에서 시작하고, 파일 분석이 완료된 뒤에만 내부 계열을 연결한다.
  // 이미 실행 가능한 프로필은 추가 파일을 연결해도 기존 워크플로 계약을 보존한다.
  const family = previous?.family ?? 'custom'
  return {
    id: previous?.id ?? randomUUID(),
    name: previous?.name ?? request.name,
    family,
    capabilities: request.capabilities ?? previous?.capabilities ?? ['txt2img'],
    tags: request.tags ?? previous?.tags ?? [],
    assets: previous?.assets.slice() ?? [],
    workflowTemplateId: previous?.workflowTemplateId ?? `${family}.txt2img.v1`,
    ...(previous?.workflowTemplate ? { workflowTemplate: previous.workflowTemplate } : {}),
    defaults: previous?.defaults ?? getComfyGenerationDefaults(family),
    agentEnabled: previous?.agentEnabled ?? false,
    priority: previous?.priority ?? 0,
    createdAt: previous?.createdAt ?? now,
    updatedAt: now
  }
}

function inferFamilyFromProfileAssets(profile: ComfyModelProfile, modelsRoot: string): ComfyModelFamily {
  const checkpoint = profile.assets.find((asset) => asset.slot === 'checkpoint')
  if (checkpoint) {
    const family = inferComfyModelFamilyFromSafeTensors(pathForRelative(modelsRoot, checkpoint.relativePath))
    if (family === 'sd15' || family === 'sdxl') return family
    return 'custom'
  }

  const diffusionModel = profile.assets.find((asset) => asset.slot === 'diffusion_model')
  if (diffusionModel) {
    const family = inferComfyModelFamilyFromSafeTensors(pathForRelative(modelsRoot, diffusionModel.relativePath))
    if (family === 'flux1' || family === 'flux2') return family
  }
  return 'custom'
}

function applyAutomaticProfileAnalysis(
  profile: ComfyModelProfile,
  modelsRoot: string,
  previous?: ComfyModelProfile
): ComfyModelProfile {
  // 기존에 확정되어 실행 중인 프로필은 파일 추가만으로 재분류하지 않는다.
  if (previous && previous.family !== 'custom' && previous.family !== 'flux2') return profile

  const family = inferFamilyFromProfileAssets(profile, modelsRoot)
  if (family === profile.family) return profile
  const usesDefaultTemplate = profile.workflowTemplateId === `${profile.family}.txt2img.v1`
  return {
    ...profile,
    family,
    workflowTemplateId: usesDefaultTemplate ? `${family}.txt2img.v1` : profile.workflowTemplateId,
    defaults: getComfyGenerationDefaults(family),
    // 새 파일 분석 결과만으로 Agent 자동 선택을 켜지 않는다.
    agentEnabled: false,
    updatedAt: Date.now()
  }
}

/**
 * 설치 폴더가 바뀐 뒤에도 예전 레지스트리만 보고 Agent를 재활성화하지 않도록,
 * 현재 Portable 설치본 안에 등록 당시와 SHA-256이 같은 일반 파일이 실제로 있는지 확인한다.
 */
async function assertAgentAssetsAvailableAtInstall(profile: ComfyModelProfile, installPath: string): Promise<void> {
  const modelsRoot = resolveModelsRoot(installPath)
  let realModelsRoot: string
  try {
    realModelsRoot = realpathSync(modelsRoot)
  } catch {
    throw new Error('현재 ComfyUI 설치 폴더에 models 경로가 없습니다. 설치 폴더를 다시 선택해 주세요.')
  }
  const unavailable: string[] = []

  const assetsToVerify = profile.workflowTemplate
    ? getComfyWorkflowAssetBindingStatus(profile.workflowTemplate, profile.assets)
      .boundAssets
      .map((asset) => ({ label: asset.comfyName, asset }))
    : getComfyRequiredSlots(profile.family).map((slot) => ({
        label: COMFY_ASSET_SLOT_LABELS[slot],
        asset: profile.assets.find((item) => item.slot === slot)
      }))
  for (const { label, asset } of assetsToVerify) {
    if (!asset) {
      unavailable.push(label)
      continue
    }
    try {
      const candidate = pathForRelative(modelsRoot, asset.relativePath)
      const stat = lstatSync(candidate)
      if (!stat.isFile() || stat.isSymbolicLink() || stat.size !== asset.size) {
        unavailable.push(label)
        continue
      }
      assertInside(realModelsRoot, realpathSync(candidate))
      if (await sha256File(candidate, asset.size) !== asset.sha256) {
        unavailable.push(label)
      }
    } catch {
      unavailable.push(label)
    }
  }

  if (unavailable.length > 0) {
    throw new Error(
      `현재 ComfyUI 설치 폴더에서 필수 구성 파일을 확인할 수 없습니다: ` +
      `${unavailable.join(', ')}. ` +
      '해당 파일을 다시 연결한 뒤 Agent 자동 선택을 켜 주세요.'
    )
  }
}

export function listComfyModelProfiles(): ComfyModelRegistry {
  return structuredClone(loadRegistry())
}

export async function pickAndImportComfyModelAssets(
  owner: BrowserWindow,
  installPath: string,
  rawRequest: unknown,
  onProgress: ProgressCallback,
  isInstallPathCurrent?: InstallPathCurrentCheck
): Promise<ComfyModelImportResult> {
  if (importBusy) throw new Error('다른 모델을 가져오는 중입니다. 완료된 뒤 다시 시도해 주세요.')
  importBusy = true
  const partialFiles: string[] = []
  const finalizedFiles: string[] = []
  try {
    const request = normalizeImportRequest(rawRequest)
    activeImportOperationId = request.operationId
    activeImportCancelled = false
    const modelsRoot = resolveModelsRoot(installPath)
    cleanupStalePartials(modelsRoot, STALE_PARTIAL_AGE_MS)
    const registry = loadRegistry()
    const registryRevisionAtLoad = registryRevision
    const previous = request.profileId
      ? registry.profiles.find((profile) => profile.id === request.profileId)
      : undefined
    if (request.profileId && !previous) throw new Error('추가할 모델 프로필을 찾을 수 없습니다.')

    const selection = await dialog.showOpenDialog(owner, {
      title: `${request.name} 모델 파일 가져오기`,
      properties: ['openFile', 'multiSelections'],
      filters: [
        {
          name: 'SafeTensors 모델',
          extensions: ['safetensors']
        }
      ]
    })
    if (selection.canceled || selection.filePaths.length === 0) {
      return { canceled: true, imported: [], reused: [] }
    }
    assertImportNotCancelled(request.operationId)
    assertInstallPathCurrent(isInstallPathCurrent)

    let profile = profileFromRequest(request, previous)
    const imported: ComfyModelAsset[] = []
    const reused: ComfyModelAsset[] = []
    const selectedFiles: PlannedImportAsset[] = []
    const allowDirectConnection = profile.family === 'custom'
    for (const source of selection.filePaths) {
      assertImportNotCancelled(request.operationId)
      const { fileName, size } = validateSourceFile(source)
      const planned = await planImportAsset(owner, modelsRoot, source, fileName, size, allowDirectConnection)
      if (!planned) return { canceled: true, imported: [], reused: [] }
      selectedFiles.push(planned)
    }
    assertInstallPathCurrent(isInstallPathCurrent)

    // 직접 연결 파일과 자동 분석 파일은 함께 연결할 수 있다. 다만 하나라도 직접
    // 연결이면 전체 프로필을 수동 ComfyUI 전용으로 보존해 Agent가 소비하지 못하게 한다.
    const includesDirectAsset = profile.assets.some((asset) => asset.kind === 'custom') ||
      selectedFiles.some((file) => file.kind === 'custom')
    if (includesDirectAsset) assertDirectImportProfile(profile)
    const primarySlots = new Set(
      profile.assets
        .map((asset) => asset.slot)
        .filter((slot): slot is 'checkpoint' | 'diffusion_model' => slot === 'checkpoint' || slot === 'diffusion_model')
    )
    for (const file of selectedFiles) {
      if (file.slot !== 'checkpoint' && file.slot !== 'diffusion_model') continue
      if (primarySlots.size > 0 && !primarySlots.has(file.slot)) {
        throw new Error('한 모델 프로필에는 하나의 생성 모델만 연결할 수 있습니다. 다른 생성 모델은 새 모델로 연결해 주세요.')
      }
      primarySlots.add(file.slot)
    }
    const plannedByHash = new Map<string, ComfyModelAsset>()
    const plannedByDestination = new Map<string, string>()

    for (const plan of selectedFiles) {
      assertImportNotCancelled(request.operationId)
      assertInstallPathCurrent(isInstallPathCurrent)
      const { source, fileName, size, kind, slot, destinationFolderName } = plan
      const destinationDirectory = join(modelsRoot, destinationFolderName)
      assertDestinationDirectory(modelsRoot, destinationDirectory)
      const hashReport = makeReporter(onProgress, request.operationId, 'hashing', fileName, size)
      const sourceHash = await sha256File(
        source,
        size,
        hashReport,
        () => assertImportNotCancelled(request.operationId)
      )
      const relativePath = `${destinationFolderName}/${fileName}`
      const duplicateKey = `${kind}:${slot ?? ''}:${destinationFolderName.toLowerCase()}:${sourceHash}:${size}`
      const alreadyPlanned = plannedByHash.get(duplicateKey)
      if (alreadyPlanned) {
        const asset = cloneAssetForSlot(alreadyPlanned, slot, plan.agentFamilies)
        assertCompatiblePrimaryAsset(profile, plan)
        if (ensureSlotAvailable(profile, asset)) {
          profile.assets.push(asset)
          reused.push(asset)
        }
        continue
      }

      const known = await findReusableAsset(
        registry,
        modelsRoot,
        kind,
        sourceHash,
        size,
        kind === 'custom' ? relativePath : undefined,
        () => assertImportNotCancelled(request.operationId)
      )
      if (known) {
        const asset = cloneAssetForSlot(known, slot, plan.agentFamilies)
        assertCompatiblePrimaryAsset(profile, plan)
        if (ensureSlotAvailable(profile, asset)) {
          profile.assets.push(asset)
          reused.push(asset)
        }
        plannedByHash.set(duplicateKey, asset)
        continue
      }

      const destination = join(destinationDirectory, fileName)
      assertInside(modelsRoot, destination)
      const plannedHash = plannedByDestination.get(destination.toLowerCase())
      if (plannedHash && plannedHash !== sourceHash) {
        throw new Error(`${fileName}: 선택한 파일끼리 이름이 같지만 내용이 다릅니다.`)
      }
      if (existsSync(destination)) {
        if (lstatSync(destination).isSymbolicLink()) {
          throw new Error(`${fileName}: 같은 이름의 심볼릭 링크 또는 연결 지점이 존재합니다.`)
        }
        const destinationStat = statSync(destination)
        if (!destinationStat.isFile()) throw new Error(`${fileName}: 같은 이름의 폴더 또는 특수 파일이 존재합니다.`)
        const existingHash = await sha256File(
          destination,
          destinationStat.size,
          undefined,
          () => assertImportNotCancelled(request.operationId)
        )
        if (destinationStat.size !== size || existingHash !== sourceHash) {
          throw new Error(`${fileName}: 같은 이름의 다른 모델 파일이 이미 존재합니다. 기존 파일은 덮어쓰지 않았습니다.`)
        }
        const asset = makeImportedAsset(plan, sourceHash)
        assertCompatiblePrimaryAsset(profile, plan)
        if (ensureSlotAvailable(profile, asset)) {
          profile.assets.push(asset)
          reused.push(asset)
        }
        plannedByHash.set(duplicateKey, asset)
        plannedByDestination.set(destination.toLowerCase(), sourceHash)
        continue
      }

      const partial = `${destination}.${request.operationId}.${randomUUID()}.partial`
      // 이미 설치됐거나 같은 해시로 재사용하는 파일은 공간을 쓰지 않는다. 실제 복사 직전에만 확인한다.
      assertAvailableDiskSpace(destinationDirectory, size)
      partialFiles.push(partial)
      const copyReport = makeReporter(onProgress, request.operationId, 'copying', fileName, size)
      const copiedHash = await copyAndHash(
        source,
        partial,
        size,
        copyReport,
        () => assertImportNotCancelled(request.operationId)
      )
      onProgress({
        operationId: request.operationId,
        phase: 'verifying',
        fileName,
        completedBytes: size,
        totalBytes: size
      })
      if (copiedHash !== sourceHash || statSync(partial).size !== size) {
        throw new Error(`${fileName}: 복사한 파일의 SHA-256 또는 크기가 원본과 일치하지 않습니다.`)
      }
      if (existsSync(destination)) {
        if (lstatSync(destination).isSymbolicLink()) {
          throw new Error(`${fileName}: 복사 중 같은 이름의 심볼릭 링크 또는 연결 지점이 생성되었습니다.`)
        }
        const destinationStat = statSync(destination)
        const existingHash = destinationStat.isFile()
          ? await sha256File(
            destination,
            destinationStat.size,
            undefined,
            () => assertImportNotCancelled(request.operationId)
          )
          : ''
        if (destinationStat.size !== size || existingHash !== sourceHash) {
          throw new Error(`${fileName}: 복사 중 같은 이름의 다른 파일이 생성되어 작업을 중단했습니다.`)
        }
        rmSync(partial, { force: true })
        partialFiles.splice(partialFiles.indexOf(partial), 1)
      } else {
        renameSync(partial, destination)
        partialFiles.splice(partialFiles.indexOf(partial), 1)
        finalizedFiles.push(destination)
      }
      const asset = makeImportedAsset(plan, sourceHash)
      assertCompatiblePrimaryAsset(profile, plan)
      if (ensureSlotAvailable(profile, asset)) profile.assets.push(asset)
      plannedByHash.set(duplicateKey, asset)
      plannedByDestination.set(destination.toLowerCase(), sourceHash)
      imported.push(asset)
      onProgress({
        operationId: request.operationId,
        phase: 'complete',
        fileName,
        completedBytes: size,
        totalBytes: size
      })
    }

    assertInstallPathCurrent(isInstallPathCurrent)
    assertRegistryCurrent(registryRevisionAtLoad)
    if (profile.workflowTemplate) {
      const workflowTemplate = bindComfyWorkflowTemplateAssets(profile.workflowTemplate, profile.assets)
      profile = {
        ...profile,
        workflowTemplateId: workflowTemplate.id,
        workflowTemplate
      }
    }
    profile = includesDirectAsset
      ? {
          ...profile,
          family: 'custom',
          workflowTemplateId: profile.workflowTemplate?.id ?? 'custom.txt2img.v1',
          defaults: profile.workflowTemplate ? profile.defaults : getComfyGenerationDefaults('custom'),
          agentEnabled: false,
          updatedAt: Date.now()
        }
      : applyAutomaticProfileAnalysis(profile, modelsRoot, previous)
    const existingIndex = registry.profiles.findIndex((item) => item.id === profile.id)
    if (existingIndex >= 0) registry.profiles[existingIndex] = profile
    else registry.profiles.push(profile)
    saveRegistry(registry)
    return { canceled: false, profile: structuredClone(profile), imported, reused }
  } catch (error) {
    for (const path of partialFiles) rmSync(path, { force: true })
    // 레지스트리가 저장되기 전에 만든 파일만 정리한다. 기존 파일과 재사용 파일은 건드리지 않는다.
    for (const path of finalizedFiles) rmSync(path, { force: true })
    if (error instanceof ComfyModelImportCancelledError) {
      return { canceled: true, imported: [], reused: [] }
    }
    throw error
  } finally {
    importBusy = false
    activeImportOperationId = undefined
    activeImportCancelled = false
  }
}

export async function pickAndImportComfyWorkflowTemplate(
  owner: BrowserWindow,
  id: unknown
): Promise<import('../shared/comfy-model').ComfyWorkflowImportResult> {
  if (typeof id !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(id)) {
    throw new Error('모델 프로필 ID가 올바르지 않습니다.')
  }
  const selection = await dialog.showOpenDialog(owner, {
    title: 'ComfyUI API 워크플로 연결',
    properties: ['openFile'],
    filters: [{ name: 'ComfyUI API 워크플로', extensions: ['json'] }]
  })
  if (selection.canceled || selection.filePaths.length === 0) return { canceled: true }
  const source = selection.filePaths[0]
  const stat = lstatSync(source)
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size < 2 || stat.size > COMFY_WORKFLOW_TEMPLATE_MAX_BYTES) {
    throw new Error('워크플로 JSON은 1MB 이하의 일반 파일이어야 합니다.')
  }
  let raw: unknown
  try {
    raw = JSON.parse(readFileSync(source, 'utf8'))
  } catch {
    throw new Error('워크플로 JSON을 읽을 수 없습니다.')
  }
  const parsed = parseComfyWorkflowTemplate(raw, source)
  const registry = loadRegistry()
  const index = registry.profiles.findIndex((profile) => profile.id === id)
  if (index < 0) throw new Error('모델 프로필을 찾을 수 없습니다.')
  const current = registry.profiles[index]
  const workflowTemplate = bindComfyWorkflowTemplateAssets(parsed.template, current.assets)
  const defaults = cleanDefaults({ ...current.defaults, ...parsed.suggestedDefaults }, current.defaults)
  const profile: ComfyModelProfile = {
    ...current,
    workflowTemplateId: workflowTemplate.id,
    workflowTemplate,
    defaults,
    // 새 워크플로는 사용자가 내용을 확인한 뒤 명시적으로 Agent 사용을 켠다.
    agentEnabled: false,
    updatedAt: Date.now()
  }
  registry.profiles[index] = profile
  saveRegistry(registry)
  return { canceled: false, profile: structuredClone(profile) }
}

export function removeComfyWorkflowTemplate(id: unknown): ComfyModelProfile {
  if (typeof id !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(id)) {
    throw new Error('모델 프로필 ID가 올바르지 않습니다.')
  }
  const registry = loadRegistry()
  const index = registry.profiles.findIndex((profile) => profile.id === id)
  if (index < 0) throw new Error('모델 프로필을 찾을 수 없습니다.')
  const current = registry.profiles[index]
  const profile: ComfyModelProfile = {
    ...current,
    workflowTemplateId: `${current.family}.txt2img.v1`,
    workflowTemplate: undefined,
    defaults: getComfyGenerationDefaults(current.family),
    agentEnabled: false,
    updatedAt: Date.now()
  }
  registry.profiles[index] = profile
  saveRegistry(registry)
  return structuredClone(profile)
}

function normalizePatch(value: unknown, current: ComfyModelProfile): ComfyModelProfilePatch {
  const raw = asObject(value)
  if (!raw) throw new Error('모델 프로필 변경 형식이 올바르지 않습니다.')
  if (raw.family !== undefined || raw.workflowTemplateId !== undefined) {
    throw new Error('모델 구조와 워크플로는 연결한 파일을 Aiso가 분석해 내부적으로 결정합니다.')
  }
  return {
    ...(raw.name === undefined ? {} : { name: cleanName(raw.name) }),
    ...(raw.capabilities === undefined ? {} : { capabilities: cleanCapabilities(raw.capabilities) }),
    ...(raw.tags === undefined ? {} : { tags: cleanTags(raw.tags) }),
    ...(raw.defaults === undefined ? {} : { defaults: cleanDefaults(raw.defaults, current.defaults) }),
    ...(raw.agentEnabled === undefined ? {} : { agentEnabled: raw.agentEnabled === true }),
    ...(raw.priority === undefined ? {} : { priority: cleanPriority(raw.priority, current.priority) })
  }
}

export async function updateComfyModelProfile(
  id: unknown,
  rawPatch: unknown,
  installPath = '',
  isInstallPathCurrent?: InstallPathCurrentCheck
): Promise<ComfyModelProfile> {
  if (typeof id !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(id)) {
    throw new Error('모델 프로필 ID가 올바르지 않습니다.')
  }
  let registry = loadRegistry()
  let registryRevisionAtLoad = registryRevision
  let index = registry.profiles.findIndex((profile) => profile.id === id)
  if (index < 0) throw new Error('모델 프로필을 찾을 수 없습니다.')
  const current = registry.profiles[index]
  const patch = normalizePatch(rawPatch, current)
  const next: ComfyModelProfile = {
    ...current,
    ...patch,
    defaults: patch.defaults ? cleanDefaults(patch.defaults, current.defaults) : current.defaults,
    updatedAt: Date.now()
  }
  if (patch.agentEnabled === true && !current.agentEnabled && !getComfyAgentReadiness(next).ready) {
    throw new Error(`Agent 자동 선택을 켤 수 없습니다. ${getComfyAgentReadiness(next).detail}`)
  }
  if (patch.agentEnabled === true && !current.agentEnabled) {
    await assertAgentAssetsAvailableAtInstall(next, installPath)
    assertInstallPathCurrent(isInstallPathCurrent)
    const latestRegistry = loadRegistry()
    const latestIndex = latestRegistry.profiles.findIndex((profile) => profile.id === id)
    if (latestIndex < 0 || !isDeepStrictEqual(latestRegistry.profiles[latestIndex], current)) {
      throw new Error('모델 목록이 파일 확인 중 변경되었습니다. 최신 목록을 확인한 뒤 다시 시도해 주세요.')
    }
    registry = latestRegistry
    index = latestIndex
    registryRevisionAtLoad = registryRevision
  }
  assertRegistryCurrent(registryRevisionAtLoad)
  registry.profiles[index] = next
  saveRegistry(registry)
  return structuredClone(next)
}

/**
 * ComfyUI Portable 설치본이 바뀌면 기존 레지스트리의 파일 경로 계약을 신뢰할 수 없다.
 * 실제 파일은 삭제하지 않고 Agent 자동 선택만 안전하게 해제한다.
 */
export function disableComfyAgentProfiles(): number {
  const registry = loadRegistry()
  const now = Date.now()
  let disabled = 0
  registry.profiles = registry.profiles.map((profile) => {
    if (!profile.agentEnabled) return profile
    disabled += 1
    return { ...profile, agentEnabled: false, updatedAt: now }
  })
  if (disabled > 0) saveRegistry(registry)
  return disabled
}

export function unregisterComfyModelProfile(id: unknown): boolean {
  if (typeof id !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(id)) {
    throw new Error('모델 프로필 ID가 올바르지 않습니다.')
  }
  const registry = loadRegistry()
  const next = registry.profiles.filter((profile) => profile.id !== id)
  if (next.length === registry.profiles.length) return false
  // 등록만 해제한다. ComfyUI 모델 파일은 어떤 경우에도 삭제하지 않는다.
  saveRegistry({ schemaVersion: REGISTRY_VERSION, profiles: next })
  return true
}

/** 공장초기화 시 Aiso 메타데이터만 제거한다. ComfyUI 모델 파일에는 접근하지 않는다. */
export function clearComfyModelRegistry(): void {
  try {
    rmSync(registryPath(), { force: true })
    registryRevision += 1
    registryRecovery = undefined
  } catch (error) {
    console.error('[comfy-models] 모델 레지스트리 초기화 실패:', error)
  }
}
