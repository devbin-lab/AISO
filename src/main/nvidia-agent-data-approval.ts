import { createHash, randomBytes } from 'node:crypto'
import { isIP } from 'node:net'
import type { ApprovalMode } from '../shared/agent.ts'
import type { AppSettings } from '../shared/settings.ts'
import { NVIDIA_SUPPORTED_AGENT_TOOL_IDS } from '../shared/tool-policy.ts'
import { getComfyAgentReadiness, type ComfyModelProfile } from '../shared/comfy-model.ts'
import {
  canonicalizeNvidiaBinding,
  type NvidiaAgentDataManifest,
  type NvidiaAgentDataScopeRequest,
  type NvidiaAgentManifestDecisionInput,
  type NvidiaAgentManifestDescribeInput,
  type NvidiaCredentialBinding
} from '../shared/nvidia.ts'

export const NVIDIA_AGENT_BASE_TOOLS = ['update_plan', 'get_system_time'] as const
export const NVIDIA_AGENT_TODO_TOOLS = ['list_calendar_events', 'create_calendar_event', 'manage_calendar_event'] as const
export const NVIDIA_AGENT_MYDB_TOOLS = [
  'list_mydb_library', 'list_mydb_history', 'list_mydb_trash', 'restore_mydb_trash_node'
] as const
export const NVIDIA_AGENT_WORKSPACE_TOOLS = [
  'list_dir', 'list_tree', 'read_file', 'grep', 'glob', 'create_dir', 'move', 'convert_document', 'analyze_document_calendar',
  'write_file', 'edit_file', 'multi_edit',
  'write_code_file', 'edit_code_file', 'multi_edit_code_file',
  'delete_file', 'delete_dir', 'run_web', 'run_code', 'run_command'
] as const
export const NVIDIA_AGENT_RAG_TOOLS = ['search_docs'] as const
export const NVIDIA_AGENT_IMAGE_TOOLS = ['generate_image'] as const
export const NVIDIA_AGENT_MANIFEST_TTL_MS = 10 * 60 * 1000

const NVIDIA_SUPPORTED_TOOL_SET = new Set<string>(NVIDIA_SUPPORTED_AGENT_TOOL_IDS)
const DECLARED_NVIDIA_TOOL_SET = new Set<string>([
  ...NVIDIA_AGENT_BASE_TOOLS,
  ...NVIDIA_AGENT_TODO_TOOLS,
  ...NVIDIA_AGENT_MYDB_TOOLS,
  ...NVIDIA_AGENT_WORKSPACE_TOOLS,
  ...NVIDIA_AGENT_RAG_TOOLS,
  ...NVIDIA_AGENT_IMAGE_TOOLS
])
if (
  NVIDIA_SUPPORTED_TOOL_SET.size !== DECLARED_NVIDIA_TOOL_SET.size ||
  [...NVIDIA_SUPPORTED_TOOL_SET].some((name) => !DECLARED_NVIDIA_TOOL_SET.has(name))
) {
  throw new Error('NVIDIA Agent 도구 계약이 일치하지 않습니다.')
}

export interface NvidiaAgentExecutionScope {
  fingerprint: string
  approvalMode: ApprovalMode
  workspace: string
  ragEnabled: boolean
  ollamaHost: string
  ragTopK: number
  allowedTools: string[]
  comfy: {
    enabled: boolean
    baseUrl: string
    profiles: ComfyModelProfile[]
    selectionMode: 'auto' | 'manual'
    selectedProfileId: string | null
  }
}

export interface NvidiaAgentManifestAuthority {
  manifest: Omit<NvidiaAgentDataManifest, 'manifestId' | 'expiresInSeconds'>
  request: NvidiaAgentDataScopeRequest
  target: NvidiaCredentialBinding & { model: string }
  executionScope: NvidiaAgentExecutionScope
}

export interface NvidiaAgentSettingsFenceDeps {
  clearApprovals(): void
  revokeAgentGrants(): Promise<void>
}

/** 설정 후속 작업보다 먼저 Main 승인과 sidecar bearer를 모두 폐기한다. */
export async function fenceNvidiaAgentSettingsMutation(
  scopeChanged: boolean,
  afterFence: () => Promise<void>,
  deps: NvidiaAgentSettingsFenceDeps
): Promise<void> {
  if (scopeChanged) {
    deps.clearApprovals()
    await deps.revokeAgentGrants()
  }
  await afterFence()
}

/** Renderer capability toggles 없이 현재 저장 설정과 private registry로 범위를 결정한다. */
export function buildAutomaticNvidiaAgentDataScope(
  settings: AppSettings,
  profiles: ComfyModelProfile[],
  selectedComfyModelId?: string
): NvidiaAgentDataScopeRequest {
  const enabled = new Set(settings.agentToolPolicy.nvidia)
  const hasWorkspaceTool = NVIDIA_AGENT_WORKSPACE_TOOLS.some(
    (toolId) => enabled.has(toolId)
  )
  const rag = Boolean(settings.workspace.trim()) && settings.ragEnabled && enabled.has('search_docs')
  const workspace = Boolean(settings.workspace.trim()) && (hasWorkspaceTool || rag)
  // 목록을 손으로 다시 적으면 NVIDIA_AGENT_TODO_TOOLS와 갈라진다. 실제로 갈라져 있었고,
  // manage_calendar_event만 켠 정책에서 todos=false가 되어 캘린더 도구 3종이 모두 빠졌다.
  const todos = NVIDIA_AGENT_TODO_TOOLS.some((toolId) => enabled.has(toolId))
  const myDb = NVIDIA_AGENT_MYDB_TOOLS.some((toolId) => enabled.has(toolId))
  const selectionMode = settings.comfyModelSelectionMode === 'manual' ? 'manual' : 'auto'

  const imageToolEnabled = enabled.has('generate_image')
  if (imageToolEnabled && selectionMode === 'auto' && selectedComfyModelId) {
    throw new Error('자동 모델 선택에서는 ComfyUI 프로필 ID를 직접 지정할 수 없습니다.')
  }
  if (imageToolEnabled && selectionMode === 'manual' && selectedComfyModelId) {
    const selected = profiles.find((profile) => profile.id === selectedComfyModelId)
    if (!selected || !getComfyAgentReadiness(selected).ready) {
      throw new Error('선택한 ComfyUI 모델은 Agent 실행 준비 상태가 아닙니다.')
    }
  }

  const image = imageToolEnabled && Boolean(settings.comfyBaseUrl.trim()) && (
    selectionMode === 'manual'
      ? Boolean(selectedComfyModelId)
      : profiles.some((profile) => profile.agentEnabled && getComfyAgentReadiness(profile).ready)
  )
  return {
    workspace,
    rag,
    image,
    todos,
    myDb,
    ...(image && selectionMode === 'manual' && selectedComfyModelId
      ? { selectedComfyModelId }
      : {})
  }
}

interface PendingRecord extends NvidiaAgentManifestAuthority {
  manifestId: string
  expiresAt: number
}

interface ApprovedRecord extends NvidiaAgentManifestAuthority {
  approvedAt: number
  expiresAt: number
}

function validSessionId(value: unknown): value is string {
  return typeof value === 'string' && value.length >= 16 && value.length <= 256 &&
    /^[A-Za-z0-9._:-]+$/.test(value)
}

function validateScope(raw: unknown): NvidiaAgentDataScopeRequest {
  if (!raw || typeof raw !== 'object') throw new Error('NVIDIA Agent 전송 범위를 확인할 수 없습니다.')
  const value = raw as Record<string, unknown>
  if (
    typeof value.workspace !== 'boolean' ||
    typeof value.rag !== 'boolean' ||
    typeof value.image !== 'boolean' ||
    (value.todos !== undefined && typeof value.todos !== 'boolean') ||
    (value.myDb !== undefined && typeof value.myDb !== 'boolean')
  ) {
    throw new Error('NVIDIA Agent 전송 범위 형식이 올바르지 않습니다.')
  }
  if (value.rag && !value.workspace) {
    throw new Error('RAG 결과 전송은 작업 폴더 전송 승인과 함께 선택해야 합니다.')
  }
  let selectedComfyModelId: string | undefined
  if (value.selectedComfyModelId !== undefined) {
    if (
      typeof value.selectedComfyModelId !== 'string' ||
      !/^[A-Za-z0-9._-]{1,128}$/.test(value.selectedComfyModelId)
    ) {
      throw new Error('ComfyUI 선택 정보가 올바르지 않습니다.')
    }
    selectedComfyModelId = value.selectedComfyModelId
  }
  return {
    workspace: value.workspace,
    rag: value.rag,
    image: value.image,
    todos: value.todos === true,
    myDb: value.myDb === true,
    ...(selectedComfyModelId ? { selectedComfyModelId } : {})
  }
}

export function validateManifestDescribeInput(raw: unknown): NvidiaAgentManifestDescribeInput {
  if (!raw || typeof raw !== 'object') throw new Error('NVIDIA Agent 세션 정보가 필요합니다.')
  const value = raw as Record<string, unknown>
  if (!validSessionId(value.sessionId)) throw new Error('NVIDIA Agent 세션 형식이 올바르지 않습니다.')
  return { sessionId: value.sessionId, scope: validateScope(value.scope) }
}

export function validateManifestDecisionInput(raw: unknown): NvidiaAgentManifestDecisionInput {
  if (!raw || typeof raw !== 'object') throw new Error('NVIDIA Agent 승인 정보가 필요합니다.')
  const value = raw as Record<string, unknown>
  if (
    !validSessionId(value.sessionId) ||
    typeof value.manifestId !== 'string' ||
    !/^[A-Za-z0-9_-]{32,128}$/.test(value.manifestId) ||
    typeof value.approved !== 'boolean'
  ) {
    throw new Error('NVIDIA Agent 승인 형식이 올바르지 않습니다.')
  }
  return {
    sessionId: value.sessionId,
    manifestId: value.manifestId,
    approved: value.approved
  }
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`)
      .join(',')}}`
  }
  return JSON.stringify(value)
}

function fingerprint(value: unknown): string {
  return createHash('sha256').update(canonical(value), 'utf8').digest('hex')
}

function requireLocalOllamaHost(value: string): string {
  let url: URL
  try {
    url = new URL(value.trim())
  } catch {
    throw new Error('로컬 Ollama 주소가 올바르지 않습니다.')
  }
  const host = url.hostname.toLowerCase()
  const loopback = host === 'localhost' || host === '[::1]' || host === '::1' ||
    (isIP(host) === 4 && host.split('.')[0] === '127')
  if (
    !loopback || (url.protocol !== 'http:' && url.protocol !== 'https:') ||
    url.username || url.password || url.search || url.hash || (url.pathname && url.pathname !== '/')
  ) {
    throw new Error('NVIDIA Agent의 RAG는 로컬 Ollama 주소만 사용할 수 있습니다.')
  }
  return url.toString().replace(/\/$/, '')
}

export function buildNvidiaAgentManifestAuthority(
  settings: AppSettings,
  sessionId: string,
  request: NvidiaAgentDataScopeRequest,
  profiles: ComfyModelProfile[],
  approvalMode: ApprovalMode = 'read'
): NvidiaAgentManifestAuthority {
  if (settings.activeLlmProvider !== 'nvidia' || !settings.nvidiaModel.trim()) {
    throw new Error('현재 저장된 NVIDIA Agent 대상이 없습니다.')
  }
  const target = {
    ...canonicalizeNvidiaBinding({
      deploymentMode: settings.nvidiaDeploymentMode,
      endpoint: settings.nvidiaDeploymentMode === 'nim' ? settings.nvidiaNimEndpoint : undefined
    }),
    model: settings.nvidiaModel.trim()
  }
  if (request.workspace && !settings.workspace.trim()) {
    throw new Error('전송할 작업 폴더가 선택되지 않았습니다.')
  }
  if (request.rag && (!request.workspace || !settings.ragEnabled)) {
    throw new Error('RAG 결과 전송을 사용하려면 작업 폴더와 RAG 사용을 먼저 켜 주세요.')
  }
  const ollamaHost = request.rag ? requireLocalOllamaHost(settings.ollamaHost) : ''
  const ragTopK = request.rag ? Math.max(1, Math.min(20, Math.trunc(settings.ragTopK))) : 0
  const savedTodos = request.todos === true
  const myDb = request.myDb === true

  const selectionMode = settings.comfyModelSelectionMode === 'manual' ? 'manual' : 'auto'
  const selectedProfileId = request.image && selectionMode === 'manual'
    ? request.selectedComfyModelId ?? null
    : null
  let imageProfiles: ComfyModelProfile[] = []
  if (request.image) {
    if (!settings.comfyBaseUrl.trim()) {
      throw new Error('NVIDIA Agent에서 사용할 수 있는 로컬 ComfyUI 구성이 없습니다.')
    }
    if (selectionMode === 'manual') {
      const selected = selectedProfileId
        ? profiles.find((profile) => profile.id === selectedProfileId)
        : undefined
      if (!selected || !getComfyAgentReadiness(selected).ready) {
        throw new Error('선택한 ComfyUI 모델이 Agent 실행 준비 상태가 아닙니다.')
      }
      imageProfiles = [structuredClone(selected)]
    } else {
      imageProfiles = structuredClone(profiles.filter(
        (profile) => profile.agentEnabled && getComfyAgentReadiness(profile).ready
      ))
      if (imageProfiles.length === 0) {
        throw new Error('Agent 자동 선택에 사용할 준비 완료 ComfyUI 모델이 없습니다.')
      }
    }
  }

  const allowedTools = [
    ...NVIDIA_AGENT_BASE_TOOLS.filter((name) => settings.agentToolPolicy.nvidia.includes(name)),
    ...(savedTodos
      ? NVIDIA_AGENT_TODO_TOOLS.filter((name) => settings.agentToolPolicy.nvidia.includes(name))
      : []),
    ...(myDb
      ? NVIDIA_AGENT_MYDB_TOOLS.filter((name) => settings.agentToolPolicy.nvidia.includes(name))
      : []),
    ...(request.workspace
      ? NVIDIA_AGENT_WORKSPACE_TOOLS.filter((name) => settings.agentToolPolicy.nvidia.includes(name))
      : []),
    ...(request.rag
      ? NVIDIA_AGENT_RAG_TOOLS.filter((name) => settings.agentToolPolicy.nvidia.includes(name))
      : []),
    ...(request.image
      ? NVIDIA_AGENT_IMAGE_TOOLS.filter((name) => settings.agentToolPolicy.nvidia.includes(name))
      : [])
  ] as string[]
  const baseToolsSent = allowedTools.some((name) => NVIDIA_AGENT_BASE_TOOLS.includes(
    name as typeof NVIDIA_AGENT_BASE_TOOLS[number]
  ))
  const todoToolsSent = allowedTools.some((name) => NVIDIA_AGENT_TODO_TOOLS.includes(
    name as typeof NVIDIA_AGENT_TODO_TOOLS[number]
  ))
  const myDbToolsSent = allowedTools.some((name) => NVIDIA_AGENT_MYDB_TOOLS.includes(
    name as typeof NVIDIA_AGENT_MYDB_TOOLS[number]
  ))
  const workspaceToolsSent = allowedTools.some((name) => NVIDIA_AGENT_WORKSPACE_TOOLS.includes(
    name as typeof NVIDIA_AGENT_WORKSPACE_TOOLS[number]
  ))
  const privateScope = {
    target,
    approvalMode,
    workspace: request.workspace ? settings.workspace.trim() : '',
    ragEnabled: request.rag,
    ollamaHost,
    ragTopK,
    savedTodos,
    myDb,
    allowedTools,
    comfy: {
      enabled: request.image,
      baseUrl: request.image ? settings.comfyBaseUrl.trim() : '',
      profiles: imageProfiles,
      selectionMode,
      selectedProfileId
    } satisfies NvidiaAgentExecutionScope['comfy']
  }
  const scopeFingerprint = fingerprint(privateScope)
  const localOnly = [
    'NVIDIA API 키와 승인 토큰',
    'ComfyUI 설치 경로·모델/체크포인트 경로·등록 정보·workflow JSON',
    ...(request.workspace ? [] : ['작업 폴더와 파일 내용']),
    ...(request.rag ? [] : ['로컬 RAG 검색 결과']),
    ...(request.todos ? [] : ['Aiso에 저장된 문서 ToDo 목록']),
    ...(request.myDb ? [] : ['My DB 라이브러리 메타데이터·변경 이력·휴지통 목록']),
    ...(request.image ? ['로컬 이미지 파일과 상세 생성 메타데이터'] : ['ComfyUI 이미지 생성 정보']),
    'Discord 데이터와 사용자 스킬'
  ]
  return {
    request: structuredClone(request),
    target,
    executionScope: {
      fingerprint: scopeFingerprint,
      approvalMode: privateScope.approvalMode,
      workspace: privateScope.workspace,
        ragEnabled: privateScope.ragEnabled,
      ollamaHost: privateScope.ollamaHost,
      ragTopK: privateScope.ragTopK,
      allowedTools: [...allowedTools],
      comfy: structuredClone(privateScope.comfy)
    },
    manifest: {
      schemaVersion: 1,
      sessionId,
      model: target.model,
      deploymentMode: target.deploymentMode,
      sends: {
        conversation: true,
        workspace: request.workspace,
        rag: request.rag,
        imagePrompt: request.image,
        savedTodos,
        myDb,
        toolResults: [...allowedTools],
        toolResultDetails: [
          ...(baseToolsSent ? ['설정에서 허용한 계획·시각 도구 호출과 결과'] : []),
          ...(todoToolsSent ? ['Aiso에 저장된 문서 ToDo 목록'] : []),
          ...(myDbToolsSent ? ['My DB의 코어·파일 메타데이터, 관계, 변경 이력 또는 휴지통 복구 결과'] : []),
          ...(workspaceToolsSent ? ['설정에서 허용한 작업 폴더·코드 도구 호출과 결과'] : []),
          ...(request.rag ? ['로컬 Ollama RAG 검색 결과'] : []),
          ...(request.image ? ['이미지 생성 성공 여부와 크기 정보'] : [])
        ]
      },
      scopeDetails: {
        workspacePath: request.workspace ? settings.workspace.trim() : null,
        rag: { enabled: request.rag, localOllama: true, topK: ragTopK },
        image: { enabled: request.image, selectionMode },
        savedTodos,
        myDb
      },
      localOnly,
      allowedTools: [...allowedTools]
    }
  }
}

export class NvidiaAgentDataApprovalStore {
  private readonly pending = new Map<string, PendingRecord>()
  private readonly approved = new Map<string, ApprovedRecord>()
  private readonly now: () => number
  private revision = 0

  constructor(now: () => number = Date.now) {
    this.now = now
  }

  private prune(): void {
    const now = this.now()
    for (const [id, record] of this.pending) {
      if (record.expiresAt <= now) this.pending.delete(id)
    }
    for (const [sessionId, record] of this.approved) {
      if (record.expiresAt <= now) this.approved.delete(sessionId)
    }
    while (this.pending.size > 256) this.pending.delete(this.pending.keys().next().value!)
    while (this.approved.size > 256) this.approved.delete(this.approved.keys().next().value!)
  }

  private enforceRecordLimit(records: Map<string, unknown>): void {
    while (records.size > 256) records.delete(records.keys().next().value!)
  }

  snapshot(): number {
    return this.revision
  }

  isCurrent(snapshot: number): boolean {
    return snapshot === this.revision
  }

  clearAll(): void {
    this.pending.clear()
    this.approved.clear()
    this.revision++
  }

  clearSession(sessionId: string): void {
    for (const [id, record] of this.pending) {
      if (record.manifest.sessionId === sessionId) this.pending.delete(id)
    }
    this.approved.delete(sessionId)
    this.revision++
  }

  describe(authority: NvidiaAgentManifestAuthority): NvidiaAgentDataManifest {
    this.prune()
    this.clearSession(authority.manifest.sessionId)
    const manifestId = randomBytes(32).toString('base64url')
    const expiresAt = this.now() + NVIDIA_AGENT_MANIFEST_TTL_MS
    this.pending.set(manifestId, {
      ...structuredClone(authority),
      manifestId,
      expiresAt
    })
    this.enforceRecordLimit(this.pending)
    return {
      ...structuredClone(authority.manifest),
      manifestId,
      expiresInSeconds: NVIDIA_AGENT_MANIFEST_TTL_MS / 1000
    }
  }

  /**
   * 반복 경고창 대신 현재 설정과 Agent 권한 모드로 Main이 산출한 exact scope를
   * 짧은 수명의 실행 권한으로 등록한다. Renderer가 capability boolean을 고를 수 없다.
   */
  authorizePolicy(authority: NvidiaAgentManifestAuthority): void {
    this.prune()
    this.clearSession(authority.manifest.sessionId)
    this.approved.set(authority.manifest.sessionId, {
      ...structuredClone(authority),
      approvedAt: this.now(),
      expiresAt: this.now() + NVIDIA_AGENT_MANIFEST_TTL_MS
    })
    this.enforceRecordLimit(this.approved)
  }

  decide(input: NvidiaAgentManifestDecisionInput): { approved: boolean } {
    this.prune()
    const pending = this.pending.get(input.manifestId)
    this.pending.delete(input.manifestId)
    this.revision++
    if (!pending || pending.manifest.sessionId !== input.sessionId || pending.expiresAt <= this.now()) {
      this.approved.delete(input.sessionId)
      throw new Error('NVIDIA Agent 전송 승인이 만료되었거나 현재 세션과 일치하지 않습니다.')
    }
    if (!input.approved) {
      this.approved.delete(input.sessionId)
      return { approved: false }
    }
    this.approved.set(input.sessionId, {
      request: structuredClone(pending.request),
      target: structuredClone(pending.target),
      executionScope: structuredClone(pending.executionScope),
      manifest: structuredClone(pending.manifest),
      approvedAt: this.now(),
      expiresAt: pending.expiresAt
    })
    this.enforceRecordLimit(this.approved)
    return { approved: true }
  }

  approvedRequest(sessionId: string): NvidiaAgentDataScopeRequest {
    this.prune()
    const record = this.approved.get(sessionId)
    if (!record) throw new Error('이 NVIDIA Agent 세션의 전송 범위가 승인되지 않았습니다.')
    return structuredClone(record.request)
  }

  approvedApprovalMode(sessionId: string): ApprovalMode {
    this.prune()
    const record = this.approved.get(sessionId)
    if (!record) throw new Error('이 NVIDIA Agent 세션의 실행 정책이 준비되지 않았습니다.')
    return record.executionScope.approvalMode
  }

  requireExact(sessionId: string, authority: NvidiaAgentManifestAuthority): NvidiaAgentExecutionScope {
    this.prune()
    const record = this.approved.get(sessionId)
    if (
      !record ||
      record.executionScope.fingerprint !== authority.executionScope.fingerprint ||
      record.target.deploymentMode !== authority.target.deploymentMode ||
      record.target.endpoint !== authority.target.endpoint ||
      record.target.model !== authority.target.model
    ) {
      throw new Error('NVIDIA Agent 전송 범위가 변경되어 다시 승인이 필요합니다.')
    }
    return structuredClone(authority.executionScope)
  }

  consumeExact(sessionId: string, authority: NvidiaAgentManifestAuthority): NvidiaAgentExecutionScope {
    const scope = this.requireExact(sessionId, authority)
    this.approved.delete(sessionId)
    this.revision++
    return scope
  }
}
