import type { AppSettings } from '../shared/settings'
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
  ComfyModelRegistry
} from '../shared/comfy-model'

export interface AisoAPI {
  ping: () => Promise<PingResult>
  settings: {
    get: () => Promise<AppSettings>
    set: (patch: Partial<AppSettings>) => Promise<AppSettings>
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
      update: (id: string, patch: ComfyModelProfilePatch) => Promise<ComfyModelProfile>
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
