import type { AppSettings, SettingsRecoveryStatus } from '../shared/settings'
import type {
  NvidiaCredentialBindingInput,
  NvidiaCredentialStatus,
  NvidiaExecutionPrepareResult,
  NvidiaCapabilitySnapshot,
  NvidiaCapabilityTargetInput,
  NvidiaModelListResult
} from '../shared/nvidia'
import type { PingResult } from '../shared/ipc'
import type { BackendInfo } from '../shared/backend'
import type { UpdateStatus } from '../shared/update'
import type { UsageSummary } from '../shared/usage'
import type {
  Conversation,
  ConversationKind,
  ConversationMeta,
  ConversationSave
} from '../shared/conversation'
import type { SkillMeta } from '../shared/skill'
import type { DiscordStatus, DiscordSchedule } from '../shared/discord'
import type { ComfyLaunchResult, ComfySurfaceRequest } from '../shared/comfy'
import type {
  ComfyModelImportProgress,
  ComfyModelImportRequest,
  ComfyModelImportResult,
  ComfyModelProfile,
  ComfyModelProfilePatch,
  ComfyModelRegistry,
  ComfyWorkflowImportResult
} from '../shared/comfy-model'

export interface AisoAPI {
  ping: () => Promise<PingResult>
  settings: {
    get: () => Promise<AppSettings>
    set: (patch: Partial<AppSettings>) => Promise<AppSettings>
    recoveryStatus: () => Promise<SettingsRecoveryStatus>
  }
  nvidia: {
    credential: {
      status: (binding?: NvidiaCredentialBindingInput) => Promise<NvidiaCredentialStatus>
      save: (binding: NvidiaCredentialBindingInput, apiKey: string) => Promise<void>
      replace: (binding: NvidiaCredentialBindingInput, apiKey: string) => Promise<void>
      delete: () => Promise<void>
    }
    execution: {
      prepare: (binding: NvidiaCredentialBindingInput) => Promise<NvidiaExecutionPrepareResult>
    }
    models: {
      refresh: (binding: NvidiaCredentialBindingInput) => Promise<NvidiaModelListResult>
    }
    capabilities: {
      status: (target: NvidiaCapabilityTargetInput) => Promise<NvidiaCapabilitySnapshot | null>
      probe: (target: NvidiaCapabilityTargetInput) => Promise<NvidiaCapabilitySnapshot>
      clear: (target: NvidiaCapabilityTargetInput) => Promise<void>
    }
  }
  backend: {
    info: () => Promise<BackendInfo>
    token: () => string
    onStatus: (cb: (info: BackendInfo) => void) => () => void
  }
  pickWorkspace: () => Promise<string | null>
  comfy: {
    pickInstall: () => Promise<string | null>
    start: () => Promise<ComfyLaunchResult>
    setSurface: (request: ComfySurfaceRequest) => Promise<void>
    reloadSurface: () => Promise<void>
    models: {
      list: () => Promise<ComfyModelRegistry>
      importAssets: (request: ComfyModelImportRequest) => Promise<ComfyModelImportResult>
      cancelImport: (operationId: string) => Promise<boolean>
      update: (id: string, patch: ComfyModelProfilePatch) => Promise<ComfyModelProfile>
      importWorkflow: (id: string) => Promise<ComfyWorkflowImportResult>
      removeWorkflow: (id: string) => Promise<ComfyModelProfile>
      unregister: (id: string) => Promise<boolean>
      onImportProgress: (cb: (progress: ComfyModelImportProgress) => void) => () => void
    }
  }
  setWindowTheme: (mode: 'dark' | 'light') => Promise<void>
  factoryReset: () => Promise<void>
  usage: {
    record: (tokens: number) => Promise<void>
    summary: () => Promise<UsageSummary>
  }
  conversations: {
    list: (kind: ConversationKind) => Promise<ConversationMeta[]>
    get: (id: string) => Promise<Conversation | null>
    save: (c: ConversationSave) => Promise<Conversation>
    setPinned: (id: string, pinned: boolean) => Promise<ConversationMeta | null>
    remove: (id: string) => Promise<void>
  }
  skills: {
    list: () => Promise<SkillMeta[]>
    remove: (name: string) => Promise<void>
  }
  discord: {
    hasToken: () => Promise<boolean>
    saveToken: (token: string) => Promise<void>
    apply: () => Promise<{ ok: boolean; detail?: string }>
    status: () => Promise<DiscordStatus>
    schedules: () => Promise<{ jobs: DiscordSchedule[] }>
    scheduleRemove: (id: string) => Promise<{ ok: boolean }>
  }
  updates: {
    version: () => Promise<string>
    check: () => Promise<UpdateStatus>
    download: () => Promise<UpdateStatus>
    install: () => Promise<void>
    onStatus: (cb: (s: UpdateStatus) => void) => () => void
  }
}

declare global {
  interface Window {
    api: AisoAPI
  }
}
