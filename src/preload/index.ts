import { contextBridge, ipcRenderer } from 'electron'
import type { AppSettings } from '../shared/settings'
import type { BackendInfo } from '../shared/backend'

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
  setWindowTheme: (mode: 'dark' | 'light') => ipcRenderer.invoke('window:set-theme', mode)
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
