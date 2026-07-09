import type { AppSettings } from '../shared/settings'
import type { PingResult } from '../shared/ipc'
import type { BackendInfo } from '../shared/backend'

export interface AisoAPI {
  ping: () => Promise<PingResult>
  settings: {
    get: () => Promise<AppSettings>
    set: (patch: Partial<AppSettings>) => Promise<AppSettings>
  }
  backend: {
    info: () => Promise<BackendInfo>
    onStatus: (cb: (info: BackendInfo) => void) => () => void
  }
  pickWorkspace: () => Promise<string | null>
  setWindowTheme: (mode: 'dark' | 'light') => Promise<void>
}

declare global {
  interface Window {
    api: AisoAPI
  }
}
