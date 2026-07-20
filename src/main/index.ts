import { app, shell, BrowserWindow, ipcMain, nativeTheme, dialog, Tray, Menu, nativeImage } from 'electron'
import { join } from 'path'
import { electronApp, optimizer, is } from '@electron-toolkit/utils'
import trayIconAsset from '../../build/icon.png?asset'
import { loadSettings, saveSettings, resetSettings } from './settings'
import { startBackend, stopBackend, backendInfo, backendToken, onBackendChange } from './backend'
import { initUpdater, checkForUpdates, downloadUpdate, quitAndInstall } from './updater'
import { recordUsage, usageSummary, clearUsage } from './usage'
import { listSkills, deleteSkill } from './skills'
import {
  saveDiscordToken,
  hasDiscordToken,
  applyDiscordConfig,
  discordStatus,
  discordSchedules,
  discordScheduleRemove,
  clearDiscordData
} from './discord'
import { freezeAppDataWrites } from './appdata-guard'
import {
  destroyComfySurface,
  reloadComfySurface,
  setComfySurface,
  startComfyUI,
  stopManagedComfyUI
} from './comfy'
import {
  clearComfyModelRegistry,
  listComfyModelProfiles,
  pickAndImportComfyModelAssets,
  unregisterComfyModelProfile,
  updateComfyModelProfile
} from './comfy-models'
import {
  listConversations,
  getConversation,
  saveConversation,
  setConversationPinned,
  deleteConversation,
  clearAllConversations
} from './conversations'
import type { AppSettings } from '../shared/settings'
import type { ComfySurfaceRequest } from '../shared/comfy'
import type { ConversationKind, ConversationSave } from '../shared/conversation'

// 로컬 LLM 앱: Chromium GPU 합성이 VRAM ~2.7GB를 점유해 16GB 카드에서
// 무거운 모델(최대 gpt-oss:20b·13.8GB) 콜드 로드가 OOM(CUDA crash) 날 수 있다.
// UI는 단순 텍스트뿐이라 하드웨어 가속을 끄고 VRAM을 전부 모델에 양보한다.
// (기본 모델 gemma4:12b는 여유가 있으나, 사용자가 gpt-oss로 바꿔도 안전하도록 상시 비활성.)
app.disableHardwareAcceleration()

// ── 백그라운드 상주(트레이) + 로그인 자동 실행 ───────────────────────────
// 창을 닫아도 앱(=사이드카=디스코드 봇·예약)이 트레이에 남아 계속 돌게 한다.
// '완전 종료'(트레이 메뉴/자동 업데이트)만 실제로 앱을 끝낸다.
let mainWindow: BrowserWindow | null = null
let tray: Tray | null = null
let isQuitting = false
// 로그인 자동 실행으로 켜졌으면 창을 띄우지 않고 트레이로만 시작한다.
const startedHidden = process.argv.includes('--hidden') || app.getLoginItemSettings().wasOpenedAtLogin

function showMainWindow(): void {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore()
    mainWindow.show()
    mainWindow.focus()
  } else {
    createWindow()
  }
}

function quitApp(): void {
  isQuitting = true
  app.quit()
}

/** 백그라운드 상주 여부 — 트레이 생성·창 닫기→숨김·종료 회피·숨겨 시작의 '단일 기준'.
 *  자동 실행(autoLaunch)은 "부팅 후에도 봇을 유지"가 목적이므로 상주를 함의한다(트레이만 있고
 *  창 닫으면 종료되는 불일치를 제거). 이 기준을 ensureTray/close/window-all-closed/ready-to-show가 공유한다. */
function residencyOn(): boolean {
  const s = loadSettings()
  return s.trayResident || s.autoLaunch
}

/** 트레이 아이콘을 상황에 맞게 생성/제거한다(상주 또는 자동실행이 켜져 있으면 존재). */
function ensureTray(): void {
  const want = residencyOn()
  if (want && !tray) {
    const img = nativeImage.createFromPath(trayIconAsset)
    tray = new Tray(img.isEmpty() ? nativeImage.createEmpty() : img)
    tray.setToolTip('Aiso — 백그라운드 실행 중')
    tray.setContextMenu(
      Menu.buildFromTemplate([
        { label: 'Aiso 열기', click: () => showMainWindow() },
        { type: 'separator' },
        { label: '완전 종료', click: () => quitApp() }
      ])
    )
    tray.on('double-click', () => showMainWindow())
  } else if (!want && tray) {
    tray.destroy()
    tray = null
  }
}

/** 로그인 시 자동 실행(트레이로 숨겨서) 등록/해제 — 설정 변경·시작 시 호출.
 *  개발 모드에선 실제 등록하지 않는다(개발용 electron 경로가 로그인 항목에 남는 것을 방지). */
function applyStartupSettings(): void {
  if (is.dev) return
  const s = loadSettings()
  app.setLoginItemSettings({ openAtLogin: s.autoLaunch, args: ['--hidden'] })
}

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

  const win = new BrowserWindow({
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
  mainWindow = win

  win.on('ready-to-show', () => {
    // 로그인 자동 실행(--hidden)으로 켜진 첫 창은 상주 모드일 때 숨긴 채 트레이로만 시작한다.
    if (!(startedHidden && residencyOn())) win.show()
  })

  // 상주 모드: 창 닫기(X)는 종료가 아니라 트레이로 숨기기. '완전 종료'만 실제 종료.
  win.on('close', (e) => {
    if (!isQuitting && residencyOn()) {
      e.preventDefault()
      win.hide()
    }
  })
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null
  })

  // 외부 링크는 기본 브라우저로
  win.webContents.setWindowOpenHandler((details) => {
    shell.openExternal(details.url)
    return { action: 'deny' }
  })

  // 최상위 창은 앱 화면 밖으로 절대 네비게이트되지 않는다 — 프리뷰 iframe 등이 top 프레임을
  // 악성 페이지로 이동시켜 window.api(대화기록·설정 IPC)를 노출하는 것을 원천 차단한다.
  const isAppUrl = (url: string): boolean => {
    const dev = process.env['ELECTRON_RENDERER_URL']
    return (!!dev && url.startsWith(dev)) || url.startsWith('file://')
  }
  const blockOffAppNav = (e: Electron.Event, url: string): void => {
    if (isAppUrl(url)) return
    e.preventDefault()
    if (url.startsWith('http://') || url.startsWith('https://')) {
      void shell.openExternal(url).catch(() => {})
    }
  }
  win.webContents.on('will-navigate', blockOffAppNav)
  win.webContents.on('will-redirect', blockOffAppNav)

  // dev: Vite dev 서버 URL / prod: 빌드된 html
  if (is.dev && process.env['ELECTRON_RENDERER_URL']) {
    win.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    win.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

// 단일 인스턴스 — 트레이 상주 중 아이콘을 다시 눌러 두 번째 인스턴스가 뜨면 사이드카·봇이
// 중복 기동(같은 토큰으로 게이트웨이 충돌)된다. 두 번째 실행은 기존 창을 띄우고 종료한다.
// 개발(npm run dev)에선 락을 걸지 않는다 — 사용자의 재실행 워크플로를 방해하지 않도록.
const gotSingleInstanceLock = is.dev ? true : app.requestSingleInstanceLock()
if (!gotSingleInstanceLock) {
  app.quit()
} else {
  app.on('second-instance', () => showMainWindow())
}

app.whenReady().then(() => {
  if (!gotSingleInstanceLock) return
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
    const next = saveSettings(patch)
    // 상주/자동실행 토글이 바뀌면 트레이·로그인 항목을 즉시 반영(재시작 불필요).
    if ('trayResident' in patch || 'autoLaunch' in patch) {
      ensureTray()
      applyStartupSettings()
    }
    // 디스코드 봇 On/Off를 공용 '저장'으로 바꿔도 런타임 봇이 즉시 시작/중지되도록 재적용
    // (예전엔 '연결/적용' 버튼으로만 반영돼, 토글 후 저장하면 플래그만 바뀌고 봇 상태는 그대로였다).
    if ('discordEnabled' in patch) void applyDiscordConfig()
    return next
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

  // ---- IPC: 사용자가 설치한 ComfyUI Windows Portable 선택·실행 ----
  ipcMain.handle('comfy:pick-install', async (e) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    const res = await dialog.showOpenDialog(win!, {
      title: 'ComfyUI Windows Portable 폴더 선택',
      properties: ['openDirectory']
    })
    if (res.canceled || res.filePaths.length === 0) return null
    return res.filePaths[0]
  })
  ipcMain.handle('comfy:start', () => {
    const settings = loadSettings()
    return startComfyUI(settings.comfyInstallPath, settings.comfyBaseUrl)
  })
  ipcMain.handle('comfy:surface:set', (e, request: ComfySurfaceRequest) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win !== mainWindow) throw new Error('ComfyUI 화면을 표시할 창을 찾을 수 없습니다.')
    setComfySurface(win, request)
  })
  ipcMain.handle('comfy:surface:reload', () => reloadComfySurface())
  ipcMain.handle('comfy:models:list', (e) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win !== mainWindow) throw new Error('모델 목록을 요청한 Aiso 창을 확인할 수 없습니다.')
    return listComfyModelProfiles()
  })
  ipcMain.handle('comfy:models:import', async (e, request: unknown) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win !== mainWindow) throw new Error('모델을 가져올 Aiso 창을 확인할 수 없습니다.')
    const settings = loadSettings()
    return pickAndImportComfyModelAssets(win, settings.comfyInstallPath, request, (progress) => {
      if (!win.isDestroyed() && !win.webContents.isDestroyed()) {
        win.webContents.send('comfy:model-import-progress', progress)
      }
    })
  })
  ipcMain.handle('comfy:models:update', (e, id: unknown, patch: unknown) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win !== mainWindow) throw new Error('모델을 변경할 Aiso 창을 확인할 수 없습니다.')
    return updateComfyModelProfile(id, patch)
  })
  ipcMain.handle('comfy:models:unregister', (e, id: unknown) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win !== mainWindow) throw new Error('모델 등록을 해제할 Aiso 창을 확인할 수 없습니다.')
    return unregisterComfyModelProfile(id)
  })

  // ---- IPC: FastAPI 사이드카 상태 ----
  ipcMain.handle('backend:info', () => backendInfo())
  // 사이드카 인증 토큰 — preload가 초기화 시 동기 조회해 렌더러 fetch 헤더에 싣는다.
  ipcMain.on('backend:token', (e) => {
    e.returnValue = backendToken()
  })
  onBackendChange((i) => {
    BrowserWindow.getAllWindows().forEach((w) => w.webContents.send('backend:status', i))
    // 백엔드가 준비되면 항상 디스코드 설정을 적용한다. enabled=false여도 apply는 예약 저장소를
    // configure(data_dir)로 초기화하므로, 봇을 껐어도 설정 탭 예약 목록이 디스크값을 반영한다
    // (봇 미활성 시 apply_config는 봇을 시작하지 않고 저장소만 준비하고 반환).
    if (i.state === 'ready') {
      void applyDiscordConfig()
    }
  })

  // ---- IPC: 토큰 사용량 (userData/usage.json) ----
  ipcMain.handle('usage:record', (_e, tokens: number) => recordUsage(tokens))
  ipcMain.handle('usage:summary', () => usageSummary())

  // ---- IPC: 스킬 (userData/skills/<이름>/) — 에이전트가 만든 자동화 도구 관리 ----
  ipcMain.handle('skills:list', () => listSkills())
  ipcMain.handle('skills:delete', (_e, name: string) => deleteSkill(name))

  // ---- IPC: 디스코드 봇 (MVP: 기본 채팅) ----
  ipcMain.handle('discord:has-token', () => hasDiscordToken())
  ipcMain.handle('discord:save-token', (_e, token: string) => saveDiscordToken(token))
  ipcMain.handle('discord:apply', () => applyDiscordConfig())
  ipcMain.handle('discord:status', () => discordStatus())
  ipcMain.handle('discord:schedules', () => discordSchedules())
  ipcMain.handle('discord:schedule-remove', (_e, id: string) => discordScheduleRemove(id))

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
    clearComfyModelRegistry() // Aiso 메타데이터만 삭제하며 ComfyUI의 실제 모델 파일은 보존한다.
    // 디스코드 비밀·상태·예약도 지우고(리셋 후 설정은 기본값=봇 꺼짐), 실행 중 봇을 중지한다.
    clearDiscordData()
    void applyDiscordConfig() // 설정 기본값(discordEnabled=false)+토큰 삭제 → 사이드카 봇 중지
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
  ensureTray() // 상주/자동실행 켜져 있으면 트레이 생성
  applyStartupSettings() // 로그인 자동 실행 등록 상태를 설정과 동기화

  // FastAPI 사이드카 시작 (실패해도 앱은 뜨고, 상태는 UI에 표시된다)
  startBackend(loadSettings().ollamaHost).catch((err) => {
    console.error('[backend] 시작 실패:', err)
  })

  app.on('activate', () => {
    // 상주 중엔 창이 숨겨진(파괴되지 않은) 상태일 수 있으므로 있으면 보여주고 없으면 생성.
    showMainWindow()
  })
})

app.on('before-quit', () => {
  isQuitting = true // close 핸들러가 트레이로 숨기지 않고 실제 종료를 통과시키도록
})

app.on('will-quit', () => {
  destroyComfySurface()
  stopManagedComfyUI()
  stopBackend()
})

app.on('window-all-closed', () => {
  // 상주 모드에선 창을 닫아도(=숨겨도) 앱을 살려 봇·예약을 유지한다.
  // (close 핸들러가 창을 hide로 막으므로 상주 시엔 이 이벤트가 거의 안 오지만, 안전 가드로 둔다.)
  if (residencyOn()) return
  if (process.platform !== 'darwin') {
    app.quit()
  }
})
