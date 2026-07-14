import { app, shell, BrowserWindow, ipcMain, nativeTheme, dialog } from 'electron'
import { join } from 'path'
import { electronApp, optimizer, is } from '@electron-toolkit/utils'
import { loadSettings, saveSettings, resetSettings } from './settings'
import { startBackend, stopBackend, backendInfo, backendToken, onBackendChange } from './backend'
import { initUpdater, checkForUpdates, downloadUpdate, quitAndInstall } from './updater'
import { recordUsage, usageSummary, clearUsage } from './usage'
import { freezeAppDataWrites } from './appdata-guard'
import {
  listConversations,
  getConversation,
  saveConversation,
  setConversationPinned,
  deleteConversation,
  clearAllConversations
} from './conversations'
import type { AppSettings } from '../shared/settings'
import type { ConversationKind, ConversationSave } from '../shared/conversation'

// 로컬 LLM 앱: Chromium GPU 합성이 VRAM ~2.7GB를 점유해 16GB 카드에서
// 무거운 모델(최대 gpt-oss:20b·13.8GB) 콜드 로드가 OOM(CUDA crash) 날 수 있다.
// UI는 단순 텍스트뿐이라 하드웨어 가속을 끄고 VRAM을 전부 모델에 양보한다.
// (기본 모델 gemma4:12b는 여유가 있으나, 사용자가 gpt-oss로 바꿔도 안전하도록 상시 비활성.)
app.disableHardwareAcceleration()

const TITLEBAR_H = 38
const TITLEBAR_COLORS = {
  dark: { color: '#0b0c0f', symbolColor: '#9a9ea9' },
  light: { color: '#fafafa', symbolColor: '#5c6067' }
} as const

function resolvedThemeMode(): 'dark' | 'light' {
  const s = loadSettings()
  if (s.theme === 'system') return nativeTheme.shouldUseDarkColors ? 'dark' : 'light'
  return s.theme
}

function createWindow(): void {
  const mode = resolvedThemeMode()
  const tb = TITLEBAR_COLORS[mode]

  const mainWindow = new BrowserWindow({
    width: 1200,
    height: 780,
    minWidth: 940,
    minHeight: 600,
    show: false,
    autoHideMenuBar: true,
    title: 'Aiso',
    backgroundColor: tb.color,
    // 프레임리스 + 네이티브 창 버튼 오버레이 (커스텀 타이틀바)
    titleBarStyle: 'hidden',
    titleBarOverlay: { ...tb, height: TITLEBAR_H },
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false
    }
  })

  mainWindow.on('ready-to-show', () => {
    mainWindow.show()
  })

  // 외부 링크는 기본 브라우저로
  mainWindow.webContents.setWindowOpenHandler((details) => {
    shell.openExternal(details.url)
    return { action: 'deny' }
  })

  // dev: Vite dev 서버 URL / prod: 빌드된 html
  if (is.dev && process.env['ELECTRON_RENDERER_URL']) {
    mainWindow.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

app.whenReady().then(() => {
  electronApp.setAppUserModelId('com.aiso.app')

  app.on('browser-window-created', (_, window) => {
    optimizer.watchWindowShortcuts(window)
  })

  // ---- IPC: main ↔ renderer 기본 왕복 확인 ----
  ipcMain.handle('ping', () => {
    console.log('[ipc] ping 수신 → pong 응답')
    return {
      message: 'pong',
      time: new Date().toISOString(),
      versions: {
        electron: process.versions.electron,
        chrome: process.versions.chrome,
        node: process.versions.node,
        v8: process.versions.v8
      }
    }
  })

  // ---- IPC: 설정 저장/불러오기 (userData/settings.json) ----
  ipcMain.handle('settings:get', () => {
    console.log('[ipc] settings:get')
    return loadSettings()
  })
  ipcMain.handle('settings:set', (_e, patch: Partial<AppSettings>) => {
    console.log('[ipc] settings:set', Object.keys(patch ?? {}))
    return saveSettings(patch)
  })

  // ---- IPC: 에이전트 작업 폴더 선택 ----
  ipcMain.handle('workspace:pick', async (e) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    const res = await dialog.showOpenDialog(win!, {
      title: '에이전트 작업 폴더 선택',
      properties: ['openDirectory', 'createDirectory']
    })
    if (res.canceled || res.filePaths.length === 0) return null
    return res.filePaths[0]
  })

  // ---- IPC: FastAPI 사이드카 상태 ----
  ipcMain.handle('backend:info', () => backendInfo())
  // 사이드카 인증 토큰 — preload가 초기화 시 동기 조회해 렌더러 fetch 헤더에 싣는다.
  ipcMain.on('backend:token', (e) => {
    e.returnValue = backendToken()
  })
  onBackendChange((i) => {
    BrowserWindow.getAllWindows().forEach((w) => w.webContents.send('backend:status', i))
  })

  // ---- IPC: 토큰 사용량 (userData/usage.json) ----
  ipcMain.handle('usage:record', (_e, tokens: number) => recordUsage(tokens))
  ipcMain.handle('usage:summary', () => usageSummary())

  // ---- IPC: 대화방 (userData/conversations.json) ----
  ipcMain.handle('conv:list', (_e, kind: ConversationKind) => listConversations(kind))
  ipcMain.handle('conv:get', (_e, id: string) => getConversation(id))
  ipcMain.handle('conv:save', (_e, c: ConversationSave) => saveConversation(c))
  ipcMain.handle('conv:pin', (_e, id: string, pinned: boolean) => setConversationPinned(id, pinned))
  ipcMain.handle('conv:delete', (_e, id: string) => deleteConversation(id))

  // ---- IPC: 공장초기화 (개발자 모드) — userData의 앱 데이터 삭제(설정·대화·사용량) ----
  ipcMain.handle('app:factory-reset', () => {
    // 삭제 전에 쓰기를 잠근다 — 진행 중이던 스트림의 지연 저장이 지운 파일을 되살리지 않게(리로드 시간 확보)
    freezeAppDataWrites()
    resetSettings()
    clearAllConversations()
    clearUsage()
  })

  // ---- IPC: 자동 업데이트 (GitHub 릴리스 기반) ----
  ipcMain.handle('app:version', () => app.getVersion())
  ipcMain.handle('update:check', () => checkForUpdates())
  ipcMain.handle('update:download', () => downloadUpdate())
  ipcMain.handle('update:install', () => quitAndInstall())
  initUpdater()

  // ---- IPC: 테마 변경 시 네이티브 타이틀바(창 버튼) 색 동기화 ----
  ipcMain.handle('window:set-theme', (e, mode: 'dark' | 'light') => {
    const win = BrowserWindow.fromWebContents(e.sender)
    const tb = TITLEBAR_COLORS[mode] ?? TITLEBAR_COLORS.dark
    try {
      win?.setTitleBarOverlay({ ...tb, height: TITLEBAR_H })
      win?.setBackgroundColor(tb.color)
    } catch {
      /* titleBarOverlay 미지원 플랫폼 무시 */
    }
  })

  createWindow()

  // FastAPI 사이드카 시작 (실패해도 앱은 뜨고, 상태는 UI에 표시된다)
  startBackend(loadSettings().ollamaHost).catch((err) => {
    console.error('[backend] 시작 실패:', err)
  })

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('will-quit', () => {
  stopBackend()
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})
