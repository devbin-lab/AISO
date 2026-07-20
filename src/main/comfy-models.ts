import { app, BrowserWindow, dialog } from 'electron'
import { createHash, randomUUID } from 'crypto'
import {
  createReadStream,
  createWriteStream,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
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
import { appDataFrozen } from './appdata-guard'
import {
  COMFY_ASSET_KINDS,
  COMFY_ASSET_SLOTS,
  COMFY_MODEL_CAPABILITIES,
  COMFY_MODEL_FAMILIES,
  DEFAULT_COMFY_GENERATION,
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

const DESTINATION_BY_KIND: Record<ComfyAssetKind, string> = {
  checkpoint: 'checkpoints',
  diffusion_model: 'diffusion_models',
  text_encoder: 'text_encoders',
  vae: 'vae',
  lora: 'loras',
  controlnet: 'controlnet'
}

const DEFAULT_SLOT_BY_KIND: Partial<Record<ComfyAssetKind, ComfyAssetSlot>> = {
  checkpoint: 'checkpoint',
  diffusion_model: 'diffusion_model',
  vae: 'vae',
  lora: 'lora',
  controlnet: 'controlnet'
}

const SLOT_KIND: Record<ComfyAssetSlot, ComfyAssetKind> = {
  checkpoint: 'checkpoint',
  diffusion_model: 'diffusion_model',
  clip_l: 'text_encoder',
  t5xxl: 'text_encoder',
  vae: 'vae',
  lora: 'lora',
  controlnet: 'controlnet'
}

const SINGLETON_SLOTS = new Set<ComfyAssetSlot>([
  'checkpoint',
  'diffusion_model',
  'clip_l',
  't5xxl',
  'vae'
])

type ProgressCallback = (progress: ComfyModelImportProgress) => void
type JsonObject = Record<string, unknown>

let importBusy = false

function registryPath(): string {
  return join(app.getPath('userData'), 'comfy-models.json')
}

function emptyRegistry(): ComfyModelRegistry {
  return { schemaVersion: REGISTRY_VERSION, profiles: [] }
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

function cleanKind(value: unknown): ComfyAssetKind {
  if (typeof value !== 'string' || !KIND_SET.has(value)) {
    throw new Error('지원하지 않는 모델 자산 종류입니다.')
  }
  return value as ComfyAssetKind
}

function cleanSlot(kind: ComfyAssetKind, value: unknown): ComfyAssetSlot {
  const fallback = DEFAULT_SLOT_BY_KIND[kind]
  if (value === undefined && fallback) return fallback
  if (typeof value !== 'string' || !SLOT_SET.has(value)) {
    if (kind === 'text_encoder') {
      throw new Error('텍스트 인코더는 CLIP-L 또는 T5XXL 역할을 선택해야 합니다.')
    }
    throw new Error('모델 자산 슬롯이 올바르지 않습니다.')
  }
  const slot = value as ComfyAssetSlot
  if (SLOT_KIND[slot] !== kind) throw new Error('자산 종류와 슬롯이 일치하지 않습니다.')
  return slot
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
  const family = cleanFamily(raw.family)
  const kind = cleanKind(raw.assetKind)
  const profileId = raw.profileId === undefined
    ? undefined
    : typeof raw.profileId === 'string' && /^[a-zA-Z0-9_-]{1,100}$/.test(raw.profileId)
      ? raw.profileId
      : (() => { throw new Error('모델 프로필 ID가 올바르지 않습니다.') })()
  return {
    operationId: cleanOperationId(raw.operationId),
    ...(profileId ? { profileId } : {}),
    name: cleanName(raw.name),
    family,
    capabilities: cleanCapabilities(raw.capabilities),
    tags: cleanTags(raw.tags),
    workflowTemplateId: cleanWorkflowTemplate(raw.workflowTemplateId, family),
    defaults: cleanDefaults(raw.defaults),
    agentEnabled: raw.agentEnabled === true,
    priority: cleanPriority(raw.priority),
    assetKind: kind,
    assetSlot: cleanSlot(kind, raw.assetSlot)
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
  if (!raw.relativePath.startsWith(`${DESTINATION_BY_KIND[kind]}/`)) return null
  return {
    id: raw.id,
    kind,
    ...(slot ? { slot } : {}),
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
    const assets = Array.isArray(raw.assets)
      ? raw.assets.map(parseStoredAsset).filter((asset): asset is ComfyModelAsset => asset !== null)
      : []
    const createdAt = typeof raw.createdAt === 'number' && Number.isFinite(raw.createdAt)
      ? raw.createdAt
      : Date.now()
    const updatedAt = typeof raw.updatedAt === 'number' && Number.isFinite(raw.updatedAt)
      ? raw.updatedAt
      : createdAt
    return {
      id: raw.id,
      name: cleanName(raw.name),
      family,
      capabilities: cleanCapabilities(raw.capabilities),
      tags: cleanTags(raw.tags),
      assets,
      workflowTemplateId: cleanWorkflowTemplate(raw.workflowTemplateId, family),
      defaults: cleanDefaults(raw.defaults),
      agentEnabled: raw.agentEnabled === true,
      priority: cleanPriority(raw.priority),
      createdAt,
      updatedAt
    }
  } catch {
    return null
  }
}

function loadRegistry(): ComfyModelRegistry {
  const file = registryPath()
  if (!existsSync(file)) return emptyRegistry()
  try {
    const raw = asObject(JSON.parse(readFileSync(file, 'utf-8')))
    if (!raw || raw.schemaVersion !== REGISTRY_VERSION || !Array.isArray(raw.profiles)) {
      throw new Error('지원하지 않는 레지스트리 형식')
    }
    return {
      schemaVersion: REGISTRY_VERSION,
      profiles: raw.profiles
        .map(parseStoredProfile)
        .filter((profile): profile is ComfyModelProfile => profile !== null)
    }
  } catch (error) {
    console.error('[comfy-models] 레지스트리 읽기 실패:', error)
    return emptyRegistry()
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
  report?: (completedBytes: number, force?: boolean) => void
): Promise<string> {
  const hash = createHash('sha256')
  let completed = 0
  const stream = createReadStream(path)
  for await (const chunk of stream) {
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
  report: (completedBytes: number, force?: boolean) => void
): Promise<string> {
  const hash = createHash('sha256')
  let completed = 0
  const observer = new Transform({
    transform(chunk: Buffer, _encoding, callback) {
      hash.update(chunk)
      completed += chunk.length
      report(completed)
      callback(null, chunk)
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
  size: number
): Promise<ComfyModelAsset | null> {
  for (const profile of registry.profiles) {
    for (const asset of profile.assets) {
      if (asset.kind !== kind || asset.sha256 !== sha256 || asset.size !== size) continue
      try {
        const candidate = pathForRelative(modelsRoot, asset.relativePath)
        if (!existsSync(candidate) || lstatSync(candidate).isSymbolicLink() || !statSync(candidate).isFile()) continue
        if (await sha256File(candidate, size) === sha256) return asset
      } catch {
        // 다른 설치본의 오래된 메타데이터이거나 사라진 파일이면 다음 후보를 확인한다.
      }
    }
  }
  return null
}

function cloneAssetForSlot(asset: ComfyModelAsset, slot: ComfyAssetSlot): ComfyModelAsset {
  return { ...asset, id: randomUUID(), slot, importedAt: Date.now() }
}

function ensureSlotAvailable(profile: ComfyModelProfile, asset: ComfyModelAsset): boolean {
  if (!asset.slot || !SINGLETON_SLOTS.has(asset.slot)) return true
  const current = profile.assets.find((item) => item.slot === asset.slot)
  if (!current) return true
  if (current.sha256 === asset.sha256 && current.relativePath === asset.relativePath) return false
  throw new Error(`${asset.slot} 슬롯에는 이미 다른 모델 자산이 등록되어 있습니다.`)
}

function profileFromRequest(request: ComfyModelImportRequest, previous?: ComfyModelProfile): ComfyModelProfile {
  const now = Date.now()
  return {
    id: previous?.id ?? randomUUID(),
    name: request.name,
    family: request.family,
    capabilities: request.capabilities ?? previous?.capabilities ?? ['txt2img'],
    tags: request.tags ?? previous?.tags ?? [],
    assets: previous?.assets.slice() ?? [],
    workflowTemplateId: request.workflowTemplateId ?? previous?.workflowTemplateId ?? `${request.family}.txt2img.v1`,
    defaults: cleanDefaults(request.defaults, previous?.defaults ?? DEFAULT_COMFY_GENERATION),
    agentEnabled: request.agentEnabled ?? previous?.agentEnabled ?? false,
    priority: request.priority ?? previous?.priority ?? 0,
    createdAt: previous?.createdAt ?? now,
    updatedAt: now
  }
}

export function listComfyModelProfiles(): ComfyModelRegistry {
  return structuredClone(loadRegistry())
}

export async function pickAndImportComfyModelAssets(
  owner: BrowserWindow,
  installPath: string,
  rawRequest: unknown,
  onProgress: ProgressCallback
): Promise<ComfyModelImportResult> {
  if (importBusy) throw new Error('다른 모델을 가져오는 중입니다. 완료된 뒤 다시 시도해 주세요.')
  const request = normalizeImportRequest(rawRequest)
  const modelsRoot = resolveModelsRoot(installPath)
  const registry = loadRegistry()
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

  importBusy = true
  const partialFiles: string[] = []
  const finalizedFiles: string[] = []
  try {
    const profile = profileFromRequest(request, previous)
    const imported: ComfyModelAsset[] = []
    const reused: ComfyModelAsset[] = []
    const destinationFolderName = DESTINATION_BY_KIND[request.assetKind]
    const destinationDirectory = join(modelsRoot, destinationFolderName)
    assertDestinationDirectory(modelsRoot, destinationDirectory)
    const selectedFiles = selection.filePaths.map((source) => ({ source, ...validateSourceFile(source) }))
    const plannedByHash = new Map<string, ComfyModelAsset>()
    const plannedByDestination = new Map<string, string>()

    for (const { source, fileName, size } of selectedFiles) {
      const hashReport = makeReporter(onProgress, request.operationId, 'hashing', fileName, size)
      const sourceHash = await sha256File(source, size, hashReport)
      const duplicateKey = `${request.assetKind}:${sourceHash}:${size}`
      const alreadyPlanned = plannedByHash.get(duplicateKey)
      if (alreadyPlanned) {
        const asset = cloneAssetForSlot(alreadyPlanned, request.assetSlot!)
        if (ensureSlotAvailable(profile, asset)) profile.assets.push(asset)
        reused.push(asset)
        continue
      }

      const known = await findReusableAsset(registry, modelsRoot, request.assetKind, sourceHash, size)
      if (known) {
        const asset = cloneAssetForSlot(known, request.assetSlot!)
        if (ensureSlotAvailable(profile, asset)) profile.assets.push(asset)
        plannedByHash.set(duplicateKey, asset)
        reused.push(asset)
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
        const existingHash = await sha256File(destination, destinationStat.size)
        if (destinationStat.size !== size || existingHash !== sourceHash) {
          throw new Error(`${fileName}: 같은 이름의 다른 모델 파일이 이미 존재합니다. 기존 파일은 덮어쓰지 않았습니다.`)
        }
        const asset: ComfyModelAsset = {
          id: randomUUID(),
          kind: request.assetKind,
          slot: request.assetSlot,
          fileName,
          comfyName: fileName,
          relativePath: `${destinationFolderName}/${fileName}`,
          size,
          sha256: sourceHash,
          importedAt: Date.now()
        }
        if (ensureSlotAvailable(profile, asset)) profile.assets.push(asset)
        plannedByHash.set(duplicateKey, asset)
        plannedByDestination.set(destination.toLowerCase(), sourceHash)
        reused.push(asset)
        continue
      }

      const partial = `${destination}.${request.operationId}.${randomUUID()}.partial`
      // 이미 설치됐거나 같은 해시로 재사용하는 파일은 공간을 쓰지 않는다. 실제 복사 직전에만 확인한다.
      assertAvailableDiskSpace(destinationDirectory, size)
      partialFiles.push(partial)
      const copyReport = makeReporter(onProgress, request.operationId, 'copying', fileName, size)
      const copiedHash = await copyAndHash(source, partial, size, copyReport)
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
          ? await sha256File(destination, destinationStat.size)
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
      const asset: ComfyModelAsset = {
        id: randomUUID(),
        kind: request.assetKind,
        slot: request.assetSlot,
        fileName,
        comfyName: fileName,
        relativePath: `${destinationFolderName}/${fileName}`,
        size,
        sha256: sourceHash,
        importedAt: Date.now()
      }
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

    const existingIndex = registry.profiles.findIndex((item) => item.id === profile.id)
    if (existingIndex >= 0) registry.profiles[existingIndex] = profile
    else registry.profiles.push(profile)
    saveRegistry(registry)
    return { canceled: false, profile: structuredClone(profile), imported, reused }
  } catch (error) {
    for (const path of partialFiles) rmSync(path, { force: true })
    // 레지스트리가 저장되기 전에 만든 파일만 정리한다. 기존 파일과 재사용 파일은 건드리지 않는다.
    for (const path of finalizedFiles) rmSync(path, { force: true })
    throw error
  } finally {
    importBusy = false
  }
}

function normalizePatch(value: unknown, current: ComfyModelProfile): ComfyModelProfilePatch {
  const raw = asObject(value)
  if (!raw) throw new Error('모델 프로필 변경 형식이 올바르지 않습니다.')
  const nextFamily = raw.family === undefined ? current.family : cleanFamily(raw.family)
  const resetDefaultWorkflow = raw.family !== undefined && raw.workflowTemplateId === undefined &&
    current.workflowTemplateId === `${current.family}.txt2img.v1`
  return {
    ...(raw.name === undefined ? {} : { name: cleanName(raw.name) }),
    ...(raw.family === undefined ? {} : { family: nextFamily }),
    ...(raw.capabilities === undefined ? {} : { capabilities: cleanCapabilities(raw.capabilities) }),
    ...(raw.tags === undefined ? {} : { tags: cleanTags(raw.tags) }),
    ...(raw.workflowTemplateId === undefined
      ? resetDefaultWorkflow ? { workflowTemplateId: `${nextFamily}.txt2img.v1` } : {}
      : { workflowTemplateId: cleanWorkflowTemplate(raw.workflowTemplateId, nextFamily) }),
    ...(raw.defaults === undefined ? {} : { defaults: cleanDefaults(raw.defaults, current.defaults) }),
    ...(raw.agentEnabled === undefined ? {} : { agentEnabled: raw.agentEnabled === true }),
    ...(raw.priority === undefined ? {} : { priority: cleanPriority(raw.priority, current.priority) })
  }
}

export function updateComfyModelProfile(id: unknown, rawPatch: unknown): ComfyModelProfile {
  if (typeof id !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(id)) {
    throw new Error('모델 프로필 ID가 올바르지 않습니다.')
  }
  const registry = loadRegistry()
  const index = registry.profiles.findIndex((profile) => profile.id === id)
  if (index < 0) throw new Error('모델 프로필을 찾을 수 없습니다.')
  const current = registry.profiles[index]
  const patch = normalizePatch(rawPatch, current)
  const next: ComfyModelProfile = {
    ...current,
    ...patch,
    defaults: patch.defaults ? cleanDefaults(patch.defaults, current.defaults) : current.defaults,
    updatedAt: Date.now()
  }
  registry.profiles[index] = next
  saveRegistry(registry)
  return structuredClone(next)
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
  } catch (error) {
    console.error('[comfy-models] 모델 레지스트리 초기화 실패:', error)
  }
}
