import { contextBridge, ipcRenderer } from 'electron'
import type { AppSettings } from '../shared/settings'
import type { BackendInfo } from '../shared/backend'
import type { UpdateStatus } from '../shared/update'
import type { UsageSummary } from '../shared/usage'
import type {
  Conversation,
  ConversationKind,
  ConversationMeta,
  ConversationSave
} from '../shared/conversation'

// 렌더러에 노출할 Aiso 전용 API (여기에 점점 기능을 추가한다)
// sandbox: true 환경이라 preload는 'electron' 모듈만 사용한다 (외부 npm require 불가)
const api = {
  ping: () => ipcRenderer.invoke('ping'),
  settings: {
    get: () => ipcRenderer.invoke('settings:get'),
    set: (patch: Partial<AppSettings>) => ipcRenderer.invoke('settings:set', patch)
  },
  backend: {
    info: () => ipcRenderer.invoke('backend:info'),
    onStatus: (cb: (info: BackendInfo) => void) => {
      const listener = (_e: unknown, info: BackendInfo): void => cb(info)
      ipcRenderer.on('backend:status', listener)
      return () => {
        ipcRenderer.removeListener('backend:status', listener)
      }
    }
  },
  pickWorkspace: (): Promise<string | null> => ipcRenderer.invoke('workspace:pick'),
  setWindowTheme: (mode: 'dark' | 'light') => ipcRenderer.invoke('window:set-theme', mode),
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
    remove: (id: string): Promise<void> => ipcRenderer.invoke('conv:delete', id)
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
