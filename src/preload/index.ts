import { contextBridge, ipcRenderer, webUtils } from 'electron'
import type { AppSettings } from '../shared/settings'
import type {
  NvidiaCredentialBindingInput,
  NvidiaCredentialStatus,
  NvidiaExecutionPrepareResult,
  NvidiaCapabilitySnapshot,
  NvidiaCapabilityTargetInput,
  NvidiaModelListResult,
  NvidiaAgentPrepareInput,
  NvidiaAgentPrepareResult,
  NvidiaAgentSessionFinishInput,
  NvidiaResearchPrepareInput,
  NvidiaResearchPrepareResult
} from '../shared/nvidia'
import type { BackendInfo } from '../shared/backend'
import type { UpdateStatus } from '../shared/update'
import type { UsageSummary } from '../shared/usage'
import type {
  Conversation,
  ConversationKind,
  ConversationMeta,
  ConversationSave,
  AgentProject
} from '../shared/conversation'
import type { SkillMeta } from '../shared/skill'
import type { DiscordStatus, DiscordSchedule } from '../shared/discord'
import type { ComfyLaunchResult, ComfySurfaceRequest } from '../shared/comfy'
import type { AttachmentDropEvent, AttachmentRef } from '../shared/attachments'
import type {
  MyDbDropEvent,
  MyDbImportResult,
  MyDbRelation
} from '../shared/mydb'
import type {
  ComfyModelImportProgress,
  ComfyModelImportRequest,
  ComfyModelImportResult,
  ComfyModelProfile,
  ComfyModelProfilePatch,
  ComfyModelRegistry,
  ComfyWorkflowImportResult
} from '../shared/comfy-model'

// 사이드카 인증 토큰 — preload 초기화 시 1회 동기 조회 후 캐시(세션 내 불변).
// 렌더러 fetch는 이 값을 X-Aiso-Token 헤더로 실어 백엔드 인증을 통과한다.
let backendTokenCache = ''
try {
  backendTokenCache = ipcRenderer.sendSync('backend:token') as string
} catch {
  backendTokenCache = ''
}

// A DOM File must be resolved in the preload's own world. Passing it through
// contextBridge first turns it into a cloned object and webUtils can no longer
// recover the Explorer path reliably.
const attachmentDropListeners = new Set<(event: AttachmentDropEvent) => void>()
const myDbDropListeners = new Set<(event: MyDbDropEvent) => void>()

function attachmentDropTarget(event: DragEvent): string {
  for (const node of event.composedPath()) {
    if (node instanceof HTMLElement) {
      const targetId = node.dataset.aisoAttachmentDropTarget
      if (targetId) return targetId
      if (node.classList.contains('composer')) {
        const picker = node.querySelector<HTMLElement>('[data-aiso-attachment-drop-target]')
        if (picker?.dataset.aisoAttachmentDropTarget) {
          return picker.dataset.aisoAttachmentDropTarget
        }
      }
    }
  }
  return ''
}

function myDbDropTarget(event: DragEvent): string {
  for (const node of event.composedPath()) {
    if (node instanceof HTMLElement) {
      const targetId = node.dataset.aisoMydbDropTarget
      if (targetId) return targetId
    }
  }
  return ''
}

function myDbDropParentId(targetId: string): string | undefined {
  const prefix = 'mydb:'
  if (!targetId.startsWith(prefix)) return undefined
  const id = targetId.slice(prefix.length).trim()
  return id || undefined
}

window.addEventListener('dragover', (event) => {
  if (myDbDropTarget(event) || attachmentDropTarget(event)) event.preventDefault()
}, true)

window.addEventListener('drop', (event) => {
  const myDbTargetId = myDbDropTarget(event)
  if (myDbTargetId) {
    event.preventDefault()
    event.stopImmediatePropagation()
    const paths = Array.from(event.dataTransfer?.files ?? [])
      .map((file) => webUtils.getPathForFile(file))
      .filter((path) => path.length > 0)
    if (paths.length === 0) {
      for (const listener of myDbDropListeners) {
        listener({ targetId: myDbTargetId, status: 'error', error: '파일 또는 폴더를 탐색기에서 끌어 놓아 주세요.' })
      }
      return
    }
    for (const listener of myDbDropListeners) listener({ targetId: myDbTargetId, status: 'start' })
    void ipcRenderer.invoke('mydb:import-dropped', paths, myDbDropParentId(myDbTargetId))
      .then((result: MyDbImportResult) => {
        for (const listener of myDbDropListeners) {
          listener({ targetId: myDbTargetId, status: 'done', result })
        }
      })
      .catch((reason: unknown) => {
        const error = reason instanceof Error ? reason.message : 'My DB에 파일을 추가하지 못했습니다.'
        for (const listener of myDbDropListeners) {
          listener({ targetId: myDbTargetId, status: 'error', error })
        }
      })
    return
  }
  const targetId = attachmentDropTarget(event)
  if (!targetId) return
  event.preventDefault()
  event.stopImmediatePropagation()
  const paths = Array.from(event.dataTransfer?.files ?? [])
    .map((file) => webUtils.getPathForFile(file))
    .filter((path) => path.length > 0)
  if (paths.length === 0) {
    for (const listener of attachmentDropListeners) {
      listener({ targetId, status: 'error', error: 'Explorer에서 파일 또는 폴더를 끌어 놓아 주세요.' })
    }
    return
  }
  for (const listener of attachmentDropListeners) listener({ targetId, status: 'start' })
  void ipcRenderer.invoke('attachments:import-dropped', paths)
    .then((attachments: AttachmentRef[]) => {
      for (const listener of attachmentDropListeners) {
        listener({ targetId, status: 'done', attachments })
      }
    })
    .catch((reason: unknown) => {
      const error = reason instanceof Error ? reason.message : '드롭한 자료를 첨부하지 못했습니다.'
      for (const listener of attachmentDropListeners) {
        listener({ targetId, status: 'error', error })
      }
    })
}, true)

// 렌더러에 노출할 Aiso 전용 API (여기에 점점 기능을 추가한다)
// sandbox: true 환경이라 preload는 'electron' 모듈만 사용한다 (외부 npm require 불가)
const api = {
  ping: () => ipcRenderer.invoke('ping'),
  settings: {
    get: () => ipcRenderer.invoke('settings:get'),
    set: (patch: Partial<AppSettings>) => ipcRenderer.invoke('settings:set', patch),
    recoveryStatus: () => ipcRenderer.invoke('settings:recovery-status')
  },
  nvidia: {
    credential: {
      status: (binding?: NvidiaCredentialBindingInput): Promise<NvidiaCredentialStatus> =>
        ipcRenderer.invoke('nvidia:credential:status', binding),
      save: (binding: NvidiaCredentialBindingInput, apiKey: string): Promise<void> =>
        ipcRenderer.invoke('nvidia:credential:save', binding, apiKey),
      replace: (binding: NvidiaCredentialBindingInput, apiKey: string): Promise<void> =>
        ipcRenderer.invoke('nvidia:credential:replace', binding, apiKey),
      delete: (): Promise<void> => ipcRenderer.invoke('nvidia:credential:delete')
    },
    execution: {
      prepare: (binding: NvidiaCredentialBindingInput): Promise<NvidiaExecutionPrepareResult> =>
        ipcRenderer.invoke('nvidia:execution:prepare', binding)
    },
    research: {
      prepare: (target: NvidiaResearchPrepareInput): Promise<NvidiaResearchPrepareResult> =>
        ipcRenderer.invoke('nvidia:research:prepare', target)
    },
    models: {
      refresh: (binding: NvidiaCredentialBindingInput): Promise<NvidiaModelListResult> =>
        ipcRenderer.invoke('nvidia:models:refresh', binding)
    },
    capabilities: {
      status: (target: NvidiaCapabilityTargetInput): Promise<NvidiaCapabilitySnapshot | null> =>
        ipcRenderer.invoke('nvidia:capabilities:status', target),
      probe: (target: NvidiaCapabilityTargetInput): Promise<NvidiaCapabilitySnapshot> =>
        ipcRenderer.invoke('nvidia:capabilities:probe', target),
      clear: (target: NvidiaCapabilityTargetInput): Promise<void> =>
        ipcRenderer.invoke('nvidia:capabilities:clear', target)
    },
    agent: {
      prepare: (input: NvidiaAgentPrepareInput): Promise<NvidiaAgentPrepareResult> =>
        ipcRenderer.invoke('nvidia:agent:prepare', input),
      finish: (input: NvidiaAgentSessionFinishInput): Promise<void> =>
        ipcRenderer.invoke('nvidia:agent:finish', input)
    }
  },
  backend: {
    info: () => ipcRenderer.invoke('backend:info'),
    /** 사이드카 인증 토큰(fetch의 X-Aiso-Token 헤더용). */
    token: (): string => backendTokenCache,
    onStatus: (cb: (info: BackendInfo) => void) => {
      const listener = (_e: unknown, info: BackendInfo): void => cb(info)
      ipcRenderer.on('backend:status', listener)
      return () => {
        ipcRenderer.removeListener('backend:status', listener)
      }
    }
  },
  pickWorkspace: (): Promise<string | null> => ipcRenderer.invoke('workspace:pick'),
  attachments: {
    pickFiles: (): Promise<AttachmentRef[]> => ipcRenderer.invoke('attachments:pick-files'),
    pickFolder: (): Promise<AttachmentRef[]> => ipcRenderer.invoke('attachments:pick-folder'),
    onDrop: (cb: (event: AttachmentDropEvent) => void): (() => void) => {
      attachmentDropListeners.add(cb)
      return () => attachmentDropListeners.delete(cb)
    }
  },
  myDb: {
    state: () => ipcRenderer.invoke('mydb:state'),
    history: () => ipcRenderer.invoke('mydb:history'),
    restoreGraphCheckpoint: (checkpointId: string) => ipcRenderer.invoke('mydb:restore-graph-checkpoint', checkpointId),
    pickSourceForFile: (itemId: string) => ipcRenderer.invoke('mydb:pick-source-for-file', itemId),
    exportCore: (coreId: string) => ipcRenderer.invoke('mydb:export-core', coreId),
    fileHistory: (itemId: string) => ipcRenderer.invoke('mydb:file-history', itemId),
    compareRevisions: (itemId: string, beforeRevisionId: string, afterRevisionId: string) =>
      ipcRenderer.invoke('mydb:compare-revisions', itemId, beforeRevisionId, afterRevisionId),
    restoreRevision: (itemId: string, revisionId: string) =>
      ipcRenderer.invoke('mydb:restore-revision', itemId, revisionId),
    storageRoot: () => ipcRenderer.invoke('mydb:storage-root'),
    pickStorageRoot: () => ipcRenderer.invoke('mydb:pick-storage-root'),
    clearAll: () => ipcRenderer.invoke('mydb:clear-all'),
    trash: () => ipcRenderer.invoke('mydb:trash'),
    createCore: (title: string, parentCoreId?: string | null) =>
      ipcRenderer.invoke('mydb:create-core', title, parentCoreId),
    renameNode: (id: string, title: string) => ipcRenderer.invoke('mydb:rename-node', { id }, title),
    deleteNode: (id: string, options?: { cascade?: boolean }) =>
      ipcRenderer.invoke('mydb:delete-node', { id }, options),
    restoreNode: (id: string) => ipcRenderer.invoke('mydb:restore-node', { id }),
    link: (sourceId: string, targetId: string, relation?: MyDbRelation) =>
      ipcRenderer.invoke('mydb:link', { id: sourceId }, { id: targetId }, relation),
    unlink: (edgeId: string) => ipcRenderer.invoke('mydb:unlink-edge', edgeId),
    pickFiles: (parentCoreId?: string | null) => ipcRenderer.invoke('mydb:pick-files', parentCoreId),
    pickFolder: (parentCoreId?: string | null) => ipcRenderer.invoke('mydb:pick-folder', parentCoreId),
    importDropped: (paths: string[], parentCoreId?: string | null) =>
      ipcRenderer.invoke('mydb:import-dropped', paths, parentCoreId),
    onDrop: (callback: (event: MyDbDropEvent) => void): (() => void) => {
      myDbDropListeners.add(callback)
      return () => myDbDropListeners.delete(callback)
    },
    openFolder: () => ipcRenderer.invoke('mydb:open-folder'),
    openFile: (id: string) => ipcRenderer.invoke('mydb:open-file', id),
    showInFolder: (id: string) => ipcRenderer.invoke('mydb:show-in-folder', id)
  },
  comfy: {
    pickInstall: (): Promise<string | null> => ipcRenderer.invoke('comfy:pick-install'),
    start: (): Promise<ComfyLaunchResult> => ipcRenderer.invoke('comfy:start'),
    setSurface: (request: ComfySurfaceRequest): Promise<void> =>
      ipcRenderer.invoke('comfy:surface:set', request),
    reloadSurface: (): Promise<void> => ipcRenderer.invoke('comfy:surface:reload'),
    models: {
      list: (): Promise<ComfyModelRegistry> => ipcRenderer.invoke('comfy:models:list'),
      importAssets: (request: ComfyModelImportRequest): Promise<ComfyModelImportResult> =>
        ipcRenderer.invoke('comfy:models:import', request),
      cancelImport: (operationId: string): Promise<boolean> =>
        ipcRenderer.invoke('comfy:models:import:cancel', operationId),
      update: (id: string, patch: ComfyModelProfilePatch): Promise<ComfyModelProfile> =>
        ipcRenderer.invoke('comfy:models:update', id, patch),
      importWorkflow: (id: string): Promise<ComfyWorkflowImportResult> =>
        ipcRenderer.invoke('comfy:models:workflow:import', id),
      removeWorkflow: (id: string): Promise<ComfyModelProfile> =>
        ipcRenderer.invoke('comfy:models:workflow:remove', id),
      unregister: (id: string): Promise<boolean> =>
        ipcRenderer.invoke('comfy:models:unregister', id),
      onImportProgress: (cb: (progress: ComfyModelImportProgress) => void) => {
        const listener = (_e: unknown, progress: ComfyModelImportProgress): void => cb(progress)
        ipcRenderer.on('comfy:model-import-progress', listener)
        return () => {
          ipcRenderer.removeListener('comfy:model-import-progress', listener)
        }
      }
    }
  },
  setWindowTheme: (mode: 'dark' | 'light') => ipcRenderer.invoke('window:set-theme', mode),
  /** 공장초기화 — 설정·대화·사용량 등 앱 데이터 삭제(개발자 모드). 호출 후 창을 리로드하면 최초 상태. */
  factoryReset: (): Promise<void> => ipcRenderer.invoke('app:factory-reset'),
  usage: {
    record: (tokens: number): Promise<void> => ipcRenderer.invoke('usage:record', tokens),
    summary: (): Promise<UsageSummary> => ipcRenderer.invoke('usage:summary')
  },
  conversations: {
    list: (kind: ConversationKind): Promise<ConversationMeta[]> =>
      ipcRenderer.invoke('conv:list', kind),
    get: (id: string): Promise<Conversation | null> => ipcRenderer.invoke('conv:get', id),
    save: (c: ConversationSave): Promise<Conversation> => ipcRenderer.invoke('conv:save', c),
    setPinned: (id: string, pinned: boolean): Promise<ConversationMeta | null> =>
      ipcRenderer.invoke('conv:pin', id, pinned),
    rename: (id: string, title: string): Promise<ConversationMeta | null> =>
      ipcRenderer.invoke('conv:rename', id, title),
    remove: (id: string): Promise<void> => ipcRenderer.invoke('conv:delete', id)
  },
  projects: {
    list: (): Promise<AgentProject[]> => ipcRenderer.invoke('project:list'),
    create: (title: string): Promise<AgentProject> => ipcRenderer.invoke('project:create', title),
    createConversation: (projectId: string, title?: string): Promise<ConversationMeta | null> =>
      ipcRenderer.invoke('project:create-conversation', projectId, title),
    start: (id: string): Promise<AgentProject | null> => ipcRenderer.invoke('project:start', id)
  },
  skills: {
    list: (): Promise<SkillMeta[]> => ipcRenderer.invoke('skills:list'),
    remove: (name: string): Promise<void> => ipcRenderer.invoke('skills:delete', name)
  },
  discord: {
    hasToken: (): Promise<boolean> => ipcRenderer.invoke('discord:has-token'),
    saveToken: (token: string): Promise<void> => ipcRenderer.invoke('discord:save-token', token),
    setLlmProvider: (provider: 'ollama' | 'nvidia'): Promise<AppSettings> =>
      ipcRenderer.invoke('discord:set-llm-provider', provider),
    apply: (): Promise<{ ok: boolean; detail?: string }> => ipcRenderer.invoke('discord:apply'),
    status: (): Promise<DiscordStatus> => ipcRenderer.invoke('discord:status'),
    schedules: (): Promise<{ jobs: DiscordSchedule[] }> => ipcRenderer.invoke('discord:schedules'),
    scheduleRemove: (id: string): Promise<{ ok: boolean }> =>
      ipcRenderer.invoke('discord:schedule-remove', id)
  },
  updates: {
    version: (): Promise<string> => ipcRenderer.invoke('app:version'),
    check: (): Promise<UpdateStatus> => ipcRenderer.invoke('update:check'),
    download: (): Promise<UpdateStatus> => ipcRenderer.invoke('update:download'),
    install: (): Promise<void> => ipcRenderer.invoke('update:install'),
    onStatus: (cb: (s: UpdateStatus) => void) => {
      const listener = (_e: unknown, s: UpdateStatus): void => cb(s)
      ipcRenderer.on('update:status', listener)
      return () => {
        ipcRenderer.removeListener('update:status', listener)
      }
    }
  }
}

if (process.contextIsolated) {
  try {
    contextBridge.exposeInMainWorld('api', api)
  } catch (error) {
    console.error(error)
  }
} else {
  // @ts-ignore (contextIsolation 꺼진 폴백)
  window.api = api
}
