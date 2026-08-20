import { app, shell, BrowserWindow, ipcMain, nativeTheme, dialog, Tray, Menu, nativeImage } from 'electron'
import { isAbsolute, join } from 'path'
import { mkdirSync } from 'fs'
import { pathToFileURL } from 'url'
import { electronApp, optimizer, is } from '@electron-toolkit/utils'
import trayIconAsset from '../../build/icon.png?asset'
import { loadSettings, saveSettings, resetSettings, getSettingsRecoveryStatus } from './settings'
import {
  startBackend,
  stopBackend,
  backendInfo,
  backendToken,
  onBackendChange,
  clearBackendNvidiaCredential,
  setBackendNvidiaCredential,
  bindBackendNvidiaCredential,
  backendNvidiaCredentialStatus,
  fetchBackendNvidiaModels,
  probeBackendNvidiaCapabilities,
  issueBackendNvidiaAgentGrant,
  clearBackendNvidiaAgentGrants,
  issueBackendNvidiaResearchGrant,
  clearBackendNvidiaResearchGrants,
  issueBackendNvidiaDiscordGrant,
  clearBackendNvidiaDiscordGrants,
  clearBackendTodoWorkspaceRegistry
} from './backend'
import { initUpdater, checkForUpdates, downloadUpdate, quitAndInstall } from './updater'
import {
  clearAttachmentStore,
  importDroppedAttachments,
  pickAttachmentFiles,
  pickAttachmentFolder,
  sweepUnreferencedAttachments
} from './attachments'
import {
  closeMyDbStorage,
  configureMyDbStorageRoot,
  getMyDbStore,
  myDbClearAll,
  myDbCreateCore,
  myDbCompareRevisions,
  myDbDeleteNode,
  myDbEnsurePreviousDayReport,
  myDbExportCore,
  myDbFileHistory,
  myDbHistory,
  myDbImportDropped,
  myDbLink,
  myDbRenameNode,
  myDbRestoreGraphCheckpoint,
  myDbRestoreRevision,
  myDbRestoreNode,
  myDbSetSourcePath,
  myDbState,
  myDbStorageRoot,
  myDbTrash,
  myDbUnlink
} from './mydb'
import { recordUsage, usageSummary, clearUsage } from './usage'
import { listSkills, deleteSkill } from './skills'
import {
  saveDiscordToken,
  hasDiscordToken,
  applyDiscordConfig,
  disableDiscordConfig,
  discordStatus,
  discordSchedules,
  discordScheduleRemove,
  clearDiscordData,
  type NvidiaDiscordRuntime
} from './discord'
import { freezeAppDataWrites } from './appdata-guard'
import {
  deleteNvidiaCredential,
  nvidiaCredentialStatus,
  readNvidiaCredentialForTransfer,
  saveNvidiaCredential
} from './nvidia-credentials'
import { prepareNvidiaExecution } from './nvidia-execution'
import {
  NvidiaCapabilityCache,
  NvidiaCapabilityRevision
} from './nvidia-capability-cache'
import {
  commitNvidiaCapabilityMutation,
  prepareNvidiaAgentAuthorization,
  validateNvidiaAgentPrepareInput,
  type ExactNvidiaAgentTarget
} from './nvidia-agent-authorization'
import {
  NvidiaAgentDataApprovalStore,
  buildAutomaticNvidiaAgentDataScope,
  buildNvidiaAgentManifestAuthority,
  fenceNvidiaAgentSettingsMutation
} from './nvidia-agent-data-approval'
import {
  destroyComfySurface,
  reloadComfySurface,
  setComfySurface,
  startComfyUI,
  stopManagedComfyUI
} from './comfy'
import {
  cancelComfyModelImport,
  clearComfyModelRegistry,
  cleanupStaleComfyModelPartials,
  disableComfyAgentProfiles,
  listComfyModelProfiles,
  pickAndImportComfyModelAssets,
  pickAndImportComfyWorkflowTemplate,
  removeComfyWorkflowTemplate,
  unregisterComfyModelProfile,
  updateComfyModelProfile
} from './comfy-models'
import {
  listConversations,
  getConversation,
  saveConversation,
  setConversationPinned,
  deleteConversation,
  listReferencedAttachmentIds,
  listAgentProjects,
  createAgentProject,
  createAgentProjectConversation,
  startAgentProject,
  clearAllConversations,
  renameConversation
} from './conversations'
import type { AppSettings } from '../shared/settings'
import {
  canonicalizeNvidiaBinding,
  sameNvidiaBinding,
  type NvidiaCapabilityTargetInput,
  type NvidiaCredentialBinding,
  type NvidiaCredentialBindingInput,
  type NvidiaAgentSessionFinishInput
} from '../shared/nvidia'
import type { ComfySurfaceRequest } from '../shared/comfy'
import type { ConversationKind, ConversationSave } from '../shared/conversation'
import {
  clearNvidiaCredentialWhenUnused
} from './nvidia-runtime-demand'
import { capabilityBoundGrantTtlSeconds } from './nvidia-grant-ttl'
import {
  NvidiaDiscordApplyCoordinator,
  assertNvidiaDiscordConsentCurrent,
  type ExactNvidiaDiscordTarget
} from './nvidia-discord-apply'
import { chromiumStoragePaths } from './chromium-storage-paths'

// 로컬 LLM 앱: Chromium GPU 합성이 VRAM ~2.7GB를 점유해 16GB 카드에서
// 무거운 모델(최대 gpt-oss:20b·13.8GB) 콜드 로드가 OOM(CUDA crash) 날 수 있다.
// UI는 단순 텍스트뿐이라 하드웨어 가속을 끄고 VRAM을 전부 모델에 양보한다.
// (기본 모델 gemma4:12b는 여유가 있으나, 사용자가 gpt-oss로 바꿔도 안전하도록 상시 비활성.)
app.disableHardwareAcceleration()

// async safeStorage의 암호 키 메타데이터는 sessionData/Local State에 있으므로 sessionData는
// 재실행 후에도 반드시 같은 경로를 써야 한다. HTTP/GPU 캐시만 개발 PID별로 분리해 동시 실행
// 인스턴스의 Windows cache lock 충돌(0x5)을 피한다.
const chromiumStorage = chromiumStoragePaths({
  userData: app.getPath('userData'),
  temp: app.getPath('temp'),
  isDev: is.dev,
  pid: process.pid
})
try {
  mkdirSync(chromiumStorage.sessionData, { recursive: true })
  mkdirSync(chromiumStorage.diskCache, { recursive: true })
  app.setPath('sessionData', chromiumStorage.sessionData)
  app.commandLine.appendSwitch('disk-cache-dir', chromiumStorage.diskCache)
  // 하드웨어 가속은 이미 꺼져 있지만, Chromium이 별도의 GPU disk cache를 만들지 않게 한다.
  app.commandLine.appendSwitch('disable-gpu-shader-disk-cache')
} catch {
  // 경로 초기화 실패 시 Electron 기본 경로를 사용한다. 앱 시작 자체를 막지는 않는다.
}

// ── 백그라운드 상주(트레이) + 로그인 자동 실행 ───────────────────────────
// 창을 닫아도 앱(=사이드카=디스코드 봇·예약)이 트레이에 남아 계속 돌게 한다.
// '완전 종료'(트레이 메뉴/자동 업데이트)만 실제로 앱을 끝낸다.
let mainWindow: BrowserWindow | null = null
let tray: Tray | null = null
let isQuitting = false
const nvidiaCapabilityRevision = new NvidiaCapabilityRevision()
const nvidiaAgentDataApprovals = new NvidiaAgentDataApprovalStore()
const nvidiaDiscordApplyCoordinator = new NvidiaDiscordApplyCoordinator()
let trustedDiscordNvidiaRuntime: NvidiaDiscordRuntime | null = null
// 로그인 자동 실행으로 켜졌으면 창을 띄우지 않고 트레이로만 시작한다.
const startedHidden = process.argv.includes('--hidden') || app.getLoginItemSettings().wasOpenedAtLogin

function capabilityCache(): NvidiaCapabilityCache {
  return new NvidiaCapabilityCache(join(app.getPath('userData'), 'nvidia-capabilities.json'))
}

function invalidateAllNvidiaCapabilities(): void {
  nvidiaCapabilityRevision.invalidate()
  capabilityCache().clearAll()
}

async function revokeBackendNvidiaAgentTrust(): Promise<void> {
  try {
    await clearBackendNvidiaAgentGrants()
  } catch (error) {
    // A live sidecar with an uncertain revoke result must not keep bearer grants.
    stopBackend()
    throw error
  }
}

async function revokeBackendNvidiaResearchTrust(): Promise<void> {
  try {
    await clearBackendNvidiaResearchGrants()
  } catch (error) {
    stopBackend()
    throw error
  }
}

async function revokeBackendNvidiaDiscordTrust(): Promise<void> {
  try {
    await clearBackendNvidiaDiscordGrants()
  } catch (error) {
    stopBackend()
    throw error
  }
}

async function revokeBackendNvidiaAllToolTrust(): Promise<void> {
  await revokeBackendNvidiaAgentTrust()
  await revokeBackendNvidiaResearchTrust()
  await revokeBackendNvidiaDiscordTrust()
}

function currentApprovedNvidiaAgentScope(
  sessionId: string,
  target: ExactNvidiaAgentTarget,
  consume = false
) {
  const request = nvidiaAgentDataApprovals.approvedRequest(sessionId)
  const approvalMode = nvidiaAgentDataApprovals.approvedApprovalMode(sessionId)
  const settings = loadSettings()
  const registry = listComfyModelProfiles()
  const authority = buildNvidiaAgentManifestAuthority(
    settings,
    sessionId,
    request,
    registry.profiles,
    approvalMode
  )
  if (
    authority.target.deploymentMode !== target.deploymentMode ||
    authority.target.endpoint !== target.endpoint ||
    authority.target.model !== target.model
  ) {
    throw new Error('NVIDIA Agent 대상이 승인된 전송 manifest와 일치하지 않습니다.')
  }
  return consume
    ? nvidiaAgentDataApprovals.consumeExact(sessionId, authority)
    : nvidiaAgentDataApprovals.requireExact(sessionId, authority)
}

async function invalidateNvidiaAgentDataTrust(): Promise<void> {
  nvidiaAgentDataApprovals.clearAll()
  await revokeBackendNvidiaAgentTrust()
}

async function invalidateDiscordNvidiaRuntime(): Promise<void> {
  trustedDiscordNvidiaRuntime = null
  await revokeBackendNvidiaDiscordTrust()
  // Queue the final untrusted configuration behind any in-flight apply. The
  // stale operation also compensates itself, and this queued write wins last.
  await applyTrustedDiscordConfig().catch(() => {})
}

async function saveNvidiaCredentialWithTrustReset(
  binding: NvidiaCredentialBindingInput,
  apiKey: unknown
): Promise<void> {
  nvidiaAgentDataApprovals.clearAll()
  await invalidateDiscordNvidiaRuntime()
  await commitNvidiaCapabilityMutation(
    nvidiaCapabilityRevision,
    async () => {
      await revokeBackendNvidiaAllToolTrust()
      await saveNvidiaCredential(binding, apiKey)
      try {
        await clearBackendNvidiaCredential()
      } catch (error) {
        capabilityCache().clearAll()
        stopBackend()
        throw error
      }
    },
    () => capabilityCache().clearAll(),
    () => capabilityCache().clearAll()
  )
}

async function mutateComfyRegistryWithTrustReset<T>(mutate: () => T | Promise<T>): Promise<T> {
  await invalidateNvidiaAgentDataTrust()
  let changed = false
  try {
    const result = await mutate()
    changed = true
    return result
  } finally {
    // Imports may run for minutes. Revoke any approval created while the old
    // registry was still authoritative before publishing the changed registry.
    await invalidateNvidiaAgentDataTrust()
    // Discord receives an immutable registry snapshot from Electron. Refresh
    // it after a successful registry mutation so Discord image generation
    // never uses a stale profile/workflow list.
    if (changed) await applyTrustedDiscordConfig()
  }
}

function currentNvidiaBinding(settings: AppSettings): NvidiaCredentialBinding | null {
  if (settings.activeLlmProvider !== 'nvidia') return null
  return canonicalizeNvidiaBinding({
    deploymentMode: settings.nvidiaDeploymentMode,
    endpoint: settings.nvidiaDeploymentMode === 'nim' ? settings.nvidiaNimEndpoint : undefined
  })
}

function requireCurrentNvidiaTarget(
  requestedInput: NvidiaCredentialBindingInput,
  requestedModel?: unknown
): { binding: NvidiaCredentialBinding; model?: string } {
  const requested = canonicalizeNvidiaBinding(requestedInput)
  const settings = loadSettings()
  const current = currentNvidiaBinding(settings)
  if (!current || !sameNvidiaBinding(requested, current)) {
    throw new Error('요청한 NVIDIA 대상이 현재 저장된 설정과 일치하지 않습니다.')
  }
  if (requestedModel === undefined) return { binding: current }
  if (typeof requestedModel !== 'string' || !requestedModel.trim() || requestedModel.trim().length > 512) {
    throw new Error('NVIDIA 모델명 형식이 올바르지 않습니다.')
  }
  const model = requestedModel.trim()
  if (model !== settings.nvidiaModel.trim()) {
    throw new Error('검사할 모델이 현재 저장된 NVIDIA 모델과 일치하지 않습니다.')
  }
  return { binding: current, model }
}

function nvidiaPreparationDeps() {
  return {
    loadSettings,
    credentialStatus: nvidiaCredentialStatus,
    readCredential: readNvidiaCredentialForTransfer,
    setSidecarCredential: setBackendNvidiaCredential,
    bindSidecarNim: (endpoint: string) => bindBackendNvidiaCredential('nim', endpoint),
    clearSidecarCredential: clearBackendNvidiaCredential,
    sidecarStatus: backendNvidiaCredentialStatus
  }
}

async function clearNvidiaCredentialWithoutDemand(settings: AppSettings = loadSettings()): Promise<void> {
  try {
    await clearNvidiaCredentialWhenUnused(
      settings,
      trustedDiscordNvidiaRuntime,
      clearBackendNvidiaCredential
    )
  } catch (error) {
    stopBackend()
    throw error
  }
}

function discordNvidiaFenceDeps() {
  return {
    loadSettings,
    revisionIsCurrent: (revision: number) => nvidiaCapabilityRevision.isCurrent(revision),
    getCapability: (target: ExactNvidiaDiscordTarget) => capabilityCache().get(target)
  }
}

async function applyTrustedDiscordConfig() {
  return nvidiaDiscordApplyCoordinator.apply({
    ...discordNvidiaFenceDeps(),
    revisionSnapshot: () => nvidiaCapabilityRevision.snapshot(),
    getTrustedRuntime: () => trustedDiscordNvidiaRuntime,
    clearTrustedRuntimeIf: (expected) => {
      if (trustedDiscordNvidiaRuntime === expected) trustedDiscordNvidiaRuntime = null
    },
    prepareExecution: async (binding) => {
      await prepareNvidiaExecution(binding, nvidiaPreparationDeps(), 'discord')
    },
    issueGrant: ({ deploymentMode, endpoint, model, ttlSeconds }) => issueBackendNvidiaDiscordGrant({
      deploymentMode,
      endpoint,
      model,
      ttlSeconds
    }),
    revokeGrants: revokeBackendNvidiaDiscordTrust,
    applyConfig: applyDiscordConfig,
    disableConfig: disableDiscordConfig,
    failClosed: stopBackend,
    clearCredentialWhenUnused: () => clearNvidiaCredentialWithoutDemand(),
    now: Date.now
  })
}

async function requireUnchangedNvidiaTarget(
  binding: NvidiaCredentialBinding,
  model: string | undefined,
  expectedRevision: number
): Promise<{ binding: NvidiaCredentialBinding; model?: string }> {
  try {
    if (!nvidiaCapabilityRevision.isCurrent(expectedRevision)) throw new Error('capability state changed')
    return requireCurrentNvidiaTarget(binding, model)
  } catch {
    await clearBackendNvidiaCredential().catch(() => {})
    throw new Error(
      model
        ? 'capability 검사 중 NVIDIA 대상 또는 모델이 변경되었습니다.'
        : '모델 조회 중 NVIDIA 대상이 변경되었습니다.'
    )
  }
}

function isExternalHttpUrl(value: string): boolean {
  try {
    const protocol = new URL(value).protocol
    return protocol === 'http:' || protocol === 'https:'
  } catch {
    return false
  }
}

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
    icon: trayIconAsset,
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
    if (isExternalHttpUrl(details.url)) {
      void shell.openExternal(details.url).catch(() => {})
    }
    return { action: 'deny' }
  })

  // 최상위 창은 앱 화면 밖으로 절대 네비게이트되지 않는다 — 프리뷰 iframe 등이 top 프레임을
  // 악성 페이지로 이동시켜 window.api(대화기록·설정 IPC)를 노출하는 것을 원천 차단한다.
  const dev = process.env['ELECTRON_RENDERER_URL']
  const rendererFileUrl = pathToFileURL(join(__dirname, '../renderer/index.html')).href
  const isAppUrl = (url: string): boolean => {
    try {
      if (dev) return new URL(url).origin === new URL(dev).origin
      return url === rendererFileUrl
    } catch {
      return false
    }
  }
  const blockOffAppNav = (e: Electron.Event, url: string): void => {
    if (isAppUrl(url)) return
    e.preventDefault()
    if (isExternalHttpUrl(url)) {
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

/**
 * The models directory can be large. Import recovery is useful, but walking
 * it before the first window is shown makes cold starts feel slower. Delay
 * this best-effort maintenance work until the UI is ready.
 */
/**
 * 참조되지 않은 첨부 폴더를 정리한다.
 *
 * 첨부는 지우는 코드가 없어 무한히 쌓였다 — 대화를 지워도, 공장초기화를 해도,
 * 첨부 칩을 ×로 지워도 남는다. 실측(개발 PC): 23MB가 전부 어떤 대화에서도 참조되지
 * 않는 고아였다.
 *
 * 참조 수집에 실패하면 스윕을 건너뛴다. 빈 집합으로 진행하면 저장소를 통째로 지운다.
 */
function deferUnreferencedAttachmentSweep(): void {
  const runSweep = (): void => {
    setTimeout(() => {
      let live: ReadonlySet<string>
      try {
        live = listReferencedAttachmentIds()
      } catch {
        return // 참조를 못 읽으면 아무것도 지우지 않는다
      }
      void sweepUnreferencedAttachments(live).catch(() => undefined)
    }, 0)
  }

  const win = mainWindow
  if (win && !win.isDestroyed()) {
    win.once('ready-to-show', runSweep)
    return
  }
  runSweep()
}


function deferStaleComfyModelPartialCleanup(): void {
  const runCleanup = (): void => {
    setTimeout(() => {
      cleanupStaleComfyModelPartials(loadSettings().comfyInstallPath)
    }, 0)
  }

  const win = mainWindow
  if (win && !win.isDestroyed()) {
    win.once('ready-to-show', runCleanup)
    return
  }
  runCleanup()
}

// 단일 인스턴스 — 트레이 상주 중 아이콘을 다시 눌러 두 번째 인스턴스가 뜨면 사이드카·봇이
// 중복 기동(같은 토큰으로 게이트웨이 충돌)된다. 두 번째 실행은 기존 창을 띄우고 종료한다.
// 개발(npm run dev)에선 락을 걸지 않는다 — 사용자의 재실행 워크플로를 방해하지 않도록.
// 개발 서버는 설치본과 병렬로 실행할 수 있어야 한다. 그렇지 않으면 설치본이 가진
// Windows 단일 인스턴스 잠금에 개발 프로세스가 연결되어, 현재 소스 대신 설치본 창만
// 다시 활성화되고 개발 프로세스는 바로 종료된다. 배포본만 단일 인스턴스를 강제한다.
const gotSingleInstanceLock = is.dev ? true : app.requestSingleInstanceLock()
if (!gotSingleInstanceLock) {
  app.quit()
} else {
  app.on('second-instance', () => showMainWindow())
}

function myDbStoragePathFor(settings: AppSettings): string {
  const configured = settings.myDbStoragePath.trim()
  return configured && isAbsolute(configured)
    ? configured
    : join(app.getPath('documents'), 'Aiso My DB')
}

function assertMyDbStoragePath(value: unknown): void {
  if (typeof value !== 'string') throw new Error('My DB 저장소 위치를 확인할 수 없습니다.')
  const path = value.trim()
  if (path && !isAbsolute(path)) {
    throw new Error('My DB 저장소 위치는 드라이브 또는 네트워크의 전체 경로여야 합니다.')
  }
}

let myDbDailyReportTimer: ReturnType<typeof setInterval> | null = null
let myDbDailyReportCheckMs = 0

function writeMissingMyDbDailyReport(): void {
  try {
    myDbEnsurePreviousDayReport()
  } catch (error) {
    console.warn('[mydb] 전날 변경 보고서 생성 실패:', error)
  }
}

function startMyDbDailyReportScheduler(settings = loadSettings()): void {
  const nextCheckMs = settings.myDbDailyReportCheckHours * 60 * 60 * 1000
  if (myDbDailyReportTimer && myDbDailyReportCheckMs === nextCheckMs) return
  if (myDbDailyReportTimer) clearInterval(myDbDailyReportTimer)
  writeMissingMyDbDailyReport()
  myDbDailyReportTimer = setInterval(writeMissingMyDbDailyReport, nextCheckMs)
  myDbDailyReportCheckMs = nextCheckMs
}

app.whenReady().then(() => {
  if (!gotSingleInstanceLock) return
  electronApp.setAppUserModelId('com.aiso.app')
  // My DB is a user-owned library, intentionally outside Aiso's resettable
  // application state and completely independent from Agent activity.
  configureMyDbStorageRoot(myDbStoragePathFor(loadSettings()))
  startMyDbDailyReportScheduler(loadSettings())

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
  ipcMain.handle('settings:recovery-status', () => getSettingsRecoveryStatus())
  ipcMain.handle('settings:set', async (_e, patch: Partial<AppSettings>) => {
    console.log('[ipc] settings:set', Object.keys(patch ?? {}))
    const previous = loadSettings()
    if (patch?.discordLlmProvider === 'nvidia' && previous.discordLlmProvider !== 'nvidia') {
      throw new Error('Discord NVIDIA는 전용 전송 범위 확인을 거쳐야 활성화할 수 있습니다.')
    }
    if ('myDbStoragePath' in (patch ?? {})) assertMyDbStoragePath(patch.myDbStoragePath)
    const next = saveSettings(patch)
    if ('myDbStoragePath' in patch && next.myDbStoragePath !== previous.myDbStoragePath) {
      configureMyDbStorageRoot(myDbStoragePathFor(next))
      writeMissingMyDbDailyReport()
    }
    if ('myDbDailyReportCheckHours' in patch && next.myDbDailyReportCheckHours !== previous.myDbDailyReportCheckHours) {
      startMyDbDailyReportScheduler(next)
    }
    if ('comfyInstallPath' in patch && next.comfyInstallPath !== previous.comfyInstallPath) {
      // 설치본이 바뀌면 이전 ComfyUI에만 있던 파일을 새 설치본에도 있다고 가정할 수 없다.
      // 실제 파일은 건드리지 않고, 재확인 전 Agent 자동 선택만 해제한다.
      disableComfyAgentProfiles()
    }
    // 상주/자동실행 토글이 바뀌면 트레이·로그인 항목을 즉시 반영(재시작 불필요).
    if ('trayResident' in patch || 'autoLaunch' in patch) {
      ensureTray()
      applyStartupSettings()
    }
    // 디스코드 봇 On/Off를 공용 '저장'으로 바꿔도 런타임 봇이 즉시 시작/중지되도록 재적용
    // (예전엔 '연결/적용' 버튼으로만 반영돼, 토글 후 저장하면 플래그만 바뀌고 봇 상태는 그대로였다).
    const previousBinding = canonicalizeNvidiaBinding({
      deploymentMode: previous.nvidiaDeploymentMode,
      endpoint: previous.nvidiaDeploymentMode === 'nim' ? previous.nvidiaNimEndpoint : undefined
    })
    const nextBinding = canonicalizeNvidiaBinding({
      deploymentMode: next.nvidiaDeploymentMode,
      endpoint: next.nvidiaDeploymentMode === 'nim' ? next.nvidiaNimEndpoint : undefined
    })
    const nvidiaTargetChanged = (
      previous.nvidiaDeploymentMode !== next.nvidiaDeploymentMode ||
      previous.nvidiaNimEndpoint !== next.nvidiaNimEndpoint ||
      previous.nvidiaModel !== next.nvidiaModel
    )
    const nvidiaAgentProviderChanged = previous.activeLlmProvider !== next.activeLlmProvider
    const nvidiaAgentTrustChanged = nvidiaTargetChanged || nvidiaAgentProviderChanged
    const nvidiaDataScopeChanged = (
      nvidiaAgentTrustChanged ||
      previous.workspace !== next.workspace ||
      previous.ragEnabled !== next.ragEnabled ||
      previous.ragTopK !== next.ragTopK ||
      previous.ollamaHost !== next.ollamaHost ||
      previous.agentToolPolicy.nvidia.join('\u0000') !== next.agentToolPolicy.nvidia.join('\u0000') ||
      previous.comfyBaseUrl !== next.comfyBaseUrl ||
      previous.comfyInstallPath !== next.comfyInstallPath ||
      previous.comfyModelSelectionMode !== next.comfyModelSelectionMode
    )
    const shouldApplyDiscordConfig = (
      'discordEnabled' in patch || 'discordLlmProvider' in patch || nvidiaTargetChanged ||
      'model' in patch || 'ollamaHost' in patch || 'comfyBaseUrl' in patch ||
      'comfyInstallPath' in patch || 'comfyModelSelectionMode' in patch
    )
    await fenceNvidiaAgentSettingsMutation(
      nvidiaDataScopeChanged,
      async () => {
        if (nvidiaTargetChanged) await invalidateDiscordNvidiaRuntime()
        if (shouldApplyDiscordConfig) await applyTrustedDiscordConfig()
      },
      {
        clearApprovals: () => nvidiaAgentDataApprovals.clearAll(),
        revokeAgentGrants: revokeBackendNvidiaAgentTrust
      }
    )
    if (
      previous.discordEnabled && !next.discordEnabled &&
      previous.discordLlmProvider === 'nvidia'
    ) {
      trustedDiscordNvidiaRuntime = null
      await revokeBackendNvidiaDiscordTrust()
      await clearNvidiaCredentialWithoutDemand(next)
    }
    if (nvidiaTargetChanged) {
      nvidiaCapabilityRevision.beginMutation()
      try {
        if (!sameNvidiaBinding(previousBinding, nextBinding)) {
          try {
            await clearBackendNvidiaCredential()
          } catch (error) {
            stopBackend()
            throw error
          }
        }
        await revokeBackendNvidiaAllToolTrust()
        capabilityCache().clearAll()
      } finally {
        nvidiaCapabilityRevision.endMutation()
      }
      await clearNvidiaCredentialWithoutDemand(next)
    } else if (nvidiaAgentProviderChanged) {
      await revokeBackendNvidiaAllToolTrust()
      await clearNvidiaCredentialWithoutDemand(next)
    }
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
  ipcMain.handle('attachments:pick-files', async (e) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win !== mainWindow) throw new Error('첨부 선택 창을 열 수 없습니다.')
    return pickAttachmentFiles(win)
  })
  ipcMain.handle('attachments:pick-folder', async (e) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win !== mainWindow) throw new Error('첨부 폴더 선택 창을 열 수 없습니다.')
    return pickAttachmentFolder(win)
  })
  ipcMain.handle('attachments:import-dropped', async (e, paths: unknown) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win !== mainWindow) throw new Error('드래그한 첨부를 확인할 수 없습니다.')
    return importDroppedAttachments(paths)
  })

  // ---- IPC: My DB — agent history와 분리된 사용자 개인 라이브러리 ----
  const requireMyDbWindow = (sender: Electron.WebContents): BrowserWindow => {
    const win = BrowserWindow.fromWebContents(sender)
    if (!win || win !== mainWindow) throw new Error('My DB를 요청한 Aiso 창을 확인할 수 없습니다.')
    return win
  }
  const myDbId = (value: unknown): string => {
    if (!value || typeof value !== 'object' || typeof (value as { id?: unknown }).id !== 'string') {
      throw new Error('My DB 항목을 확인할 수 없습니다.')
    }
    return (value as { id: string }).id
  }
  const myDbPaths = (value: unknown): string[] => {
    if (!Array.isArray(value) || value.length === 0 || value.some((path) => typeof path !== 'string' || !path.trim())) {
      throw new Error('가져올 파일 또는 폴더 경로를 확인할 수 없습니다.')
    }
    return value
  }
  const myDbParentId = (value: unknown): string | null => {
    if (value === undefined || value === null) return null
    if (typeof value !== 'string' || !value.trim()) throw new Error('대상 코어를 확인할 수 없습니다.')
    return value
  }
  ipcMain.handle('mydb:state', (e) => {
    requireMyDbWindow(e.sender)
    return myDbState()
  })
  ipcMain.handle('mydb:history', (e) => {
    requireMyDbWindow(e.sender)
    return myDbHistory()
  })
  ipcMain.handle('mydb:restore-graph-checkpoint', (e, checkpointId: unknown) => {
    requireMyDbWindow(e.sender)
    if (typeof checkpointId !== 'string' || !checkpointId.trim()) throw new Error('복원할 그래프 시점을 확인할 수 없습니다.')
    return myDbRestoreGraphCheckpoint(checkpointId)
  })
  ipcMain.handle('mydb:pick-source-for-file', async (e, itemId: unknown) => {
    const win = requireMyDbWindow(e.sender)
    const result = await dialog.showOpenDialog(win, {
      title: 'My DB에 반영할 외부 원본 파일 선택',
      properties: ['openFile']
    })
    if (result.canceled || result.filePaths.length === 0) return null
    return myDbSetSourcePath(myDbId({ id: itemId }), result.filePaths[0]!)
  })
  ipcMain.handle('mydb:export-core', async (e, coreId: unknown) => {
    const win = requireMyDbWindow(e.sender)
    const result = await dialog.showOpenDialog(win, {
      title: '포커스한 코어를 저장할 폴더 선택',
      properties: ['openDirectory', 'createDirectory']
    })
    if (result.canceled || result.filePaths.length === 0) return null
    return myDbExportCore(myDbId({ id: coreId }), result.filePaths[0]!)
  })
  ipcMain.handle('mydb:file-history', (e, itemId: unknown) => {
    requireMyDbWindow(e.sender)
    return myDbFileHistory(myDbId({ id: itemId }))
  })
  ipcMain.handle('mydb:compare-revisions', (e, itemId: unknown, beforeRevisionId: unknown, afterRevisionId: unknown) => {
    requireMyDbWindow(e.sender)
    return myDbCompareRevisions(
      myDbId({ id: itemId }),
      myDbId({ id: beforeRevisionId }),
      myDbId({ id: afterRevisionId })
    )
  })
  ipcMain.handle('mydb:restore-revision', (e, itemId: unknown, revisionId: unknown) => {
    requireMyDbWindow(e.sender)
    return myDbRestoreRevision(myDbId({ id: itemId }), myDbId({ id: revisionId }))
  })
  ipcMain.handle('mydb:storage-root', (e) => {
    requireMyDbWindow(e.sender)
    return myDbStorageRoot()
  })
  ipcMain.handle('mydb:pick-storage-root', async (e) => {
    const win = requireMyDbWindow(e.sender)
    const result = await dialog.showOpenDialog(win, {
      title: 'My DB 저장소 폴더 선택',
      defaultPath: myDbStorageRoot(),
      properties: ['openDirectory', 'createDirectory']
    })
    return result.canceled || result.filePaths.length === 0 ? null : result.filePaths[0]
  })
  ipcMain.handle('mydb:clear-all', async (e) => {
    requireMyDbWindow(e.sender)
    await myDbClearAll()
  })
  ipcMain.handle('mydb:trash', (e) => {
    requireMyDbWindow(e.sender)
    return myDbTrash()
  })
  ipcMain.handle('mydb:create-core', (e, title: unknown, parentCoreId?: unknown) => {
    requireMyDbWindow(e.sender)
    if (typeof title !== 'string') throw new Error('코어 이름을 입력해 주세요.')
    return myDbCreateCore(title, myDbParentId(parentCoreId))
  })
  ipcMain.handle('mydb:rename-node', async (e, node: unknown, title: unknown) => {
    requireMyDbWindow(e.sender)
    if (typeof title !== 'string') throw new Error('새 이름을 입력해 주세요.')
    return myDbRenameNode(myDbId(node), title)
  })
  ipcMain.handle('mydb:delete-node', (e, node: unknown, options?: unknown) => {
    requireMyDbWindow(e.sender)
    const cascade = Boolean(
      options && typeof options === 'object' && (options as { cascade?: unknown }).cascade === true
    )
    myDbDeleteNode(myDbId(node), { cascade })
  })
  ipcMain.handle('mydb:restore-node', (e, node: unknown) => {
    requireMyDbWindow(e.sender)
    return myDbRestoreNode(myDbId(node))
  })
  ipcMain.handle('mydb:link', (e, source: unknown, target: unknown, relation?: unknown) => {
    requireMyDbWindow(e.sender)
    if (relation !== undefined && !['contains', 'related', 'references', 'depends_on'].includes(String(relation))) {
      throw new Error('지원하지 않는 관계입니다.')
    }
    return myDbLink(myDbId(source), myDbId(target), relation as never)
  })
  ipcMain.handle('mydb:unlink-edge', (e, edgeId: unknown) => {
    requireMyDbWindow(e.sender)
    if (typeof edgeId !== 'string' || !edgeId) throw new Error('연결을 확인할 수 없습니다.')
    myDbUnlink(edgeId)
  })
  ipcMain.handle('mydb:pick-files', async (e, parentCoreId?: unknown) => {
    const win = requireMyDbWindow(e.sender)
    const result = await dialog.showOpenDialog(win, {
      title: 'My DB에 파일 추가',
      properties: ['openFile', 'multiSelections']
    })
    if (result.canceled || result.filePaths.length === 0) {
      return { createdNodes: [], createdEdges: [], skippedPaths: [] }
    }
    return myDbImportDropped(result.filePaths, myDbParentId(parentCoreId))
  })
  ipcMain.handle('mydb:pick-folder', async (e, parentCoreId?: unknown) => {
    const win = requireMyDbWindow(e.sender)
    const result = await dialog.showOpenDialog(win, {
      title: 'My DB에 폴더 추가',
      properties: ['openDirectory', 'multiSelections']
    })
    if (result.canceled || result.filePaths.length === 0) {
      return { createdNodes: [], createdEdges: [], skippedPaths: [] }
    }
    return myDbImportDropped(result.filePaths, myDbParentId(parentCoreId))
  })
  ipcMain.handle('mydb:import-dropped', async (e, paths: unknown, parentCoreId?: unknown) => {
    requireMyDbWindow(e.sender)
    return myDbImportDropped(myDbPaths(paths), myDbParentId(parentCoreId))
  })
  ipcMain.handle('mydb:open-folder', async (e) => {
    requireMyDbWindow(e.sender)
    const error = await shell.openPath(myDbStorageRoot())
    if (error) throw new Error(error)
  })
  ipcMain.handle('mydb:open-file', async (e, id: unknown) => {
    requireMyDbWindow(e.sender)
    const error = await shell.openPath(getMyDbStore().resolveItemPath(myDbId({ id })))
    if (error) throw new Error(error)
  })
  ipcMain.handle('mydb:show-in-folder', (e, id: unknown) => {
    requireMyDbWindow(e.sender)
    shell.showItemInFolder(getMyDbStore().resolveItemPath(myDbId({ id })))
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
    return mutateComfyRegistryWithTrustReset(() =>
      pickAndImportComfyModelAssets(win, settings.comfyInstallPath, request, (progress) => {
        if (!win.isDestroyed() && !win.webContents.isDestroyed()) {
          win.webContents.send('comfy:model-import-progress', progress)
        }
      }, () => loadSettings().comfyInstallPath === settings.comfyInstallPath)
    )
  })
  ipcMain.handle('comfy:models:import:cancel', (e, operationId: unknown) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win !== mainWindow) throw new Error('모델 가져오기를 취소할 Aiso 창을 확인할 수 없습니다.')
    return cancelComfyModelImport(operationId)
  })
  ipcMain.handle('comfy:models:update', async (e, id: unknown, patch: unknown) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win !== mainWindow) throw new Error('모델을 변경할 Aiso 창을 확인할 수 없습니다.')
    const settings = loadSettings()
    return mutateComfyRegistryWithTrustReset(() =>
      updateComfyModelProfile(
        id,
        patch,
        settings.comfyInstallPath,
        () => loadSettings().comfyInstallPath === settings.comfyInstallPath
      )
    )
  })
  ipcMain.handle('comfy:models:workflow:import', async (e, id: unknown) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win !== mainWindow) throw new Error('워크플로를 연결할 Aiso 창을 확인할 수 없습니다.')
    return mutateComfyRegistryWithTrustReset(() => pickAndImportComfyWorkflowTemplate(win, id))
  })
  ipcMain.handle('comfy:models:workflow:remove', async (e, id: unknown) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win !== mainWindow) throw new Error('워크플로를 변경할 Aiso 창을 확인할 수 없습니다.')
    return mutateComfyRegistryWithTrustReset(() => removeComfyWorkflowTemplate(id))
  })
  ipcMain.handle('comfy:models:unregister', async (e, id: unknown) => {
    const win = BrowserWindow.fromWebContents(e.sender)
    if (!win || win !== mainWindow) throw new Error('모델 등록을 해제할 Aiso 창을 확인할 수 없습니다.')
    return mutateComfyRegistryWithTrustReset(() => unregisterComfyModelProfile(id))
  })

  // ---- IPC: FastAPI 사이드카 상태 ----
  ipcMain.handle('backend:info', () => backendInfo())
  // 사이드카 인증 토큰 — preload가 초기화 시 동기 조회해 렌더러 fetch 헤더에 싣는다.
  ipcMain.on('backend:token', (e) => {
    e.returnValue = backendToken()
  })
  const requireMainRenderer = (sender: Electron.WebContents): void => {
    const win = BrowserWindow.fromWebContents(sender)
    if (!win || win !== mainWindow) throw new Error('요청한 Aiso 창을 확인할 수 없습니다.')
  }
  ipcMain.handle('nvidia:credential:status', (e, binding?: NvidiaCredentialBindingInput) => {
    requireMainRenderer(e.sender)
    return nvidiaCredentialStatus(binding)
  })
  ipcMain.handle(
    'nvidia:credential:save',
    async (e, binding: NvidiaCredentialBindingInput, apiKey: unknown) => {
      requireMainRenderer(e.sender)
      await saveNvidiaCredentialWithTrustReset(binding, apiKey)
    }
  )
  ipcMain.handle(
    'nvidia:credential:replace',
    async (e, binding: NvidiaCredentialBindingInput, apiKey: unknown) => {
      requireMainRenderer(e.sender)
      await saveNvidiaCredentialWithTrustReset(binding, apiKey)
    }
  )
  ipcMain.handle('nvidia:credential:delete', async (e) => {
    requireMainRenderer(e.sender)
    nvidiaAgentDataApprovals.clearAll()
    await invalidateDiscordNvidiaRuntime()
    nvidiaCapabilityRevision.beginMutation()
    try {
      try {
        await revokeBackendNvidiaAllToolTrust()
        await clearBackendNvidiaCredential()
      } catch (error) {
        stopBackend()
        throw error
      } finally {
        deleteNvidiaCredential()
        capabilityCache().clearAll()
      }
    } finally {
      nvidiaCapabilityRevision.endMutation()
    }
  })
  ipcMain.handle(
    'nvidia:execution:prepare',
    async (e, requestedInput: NvidiaCredentialBindingInput) => {
      requireMainRenderer(e.sender)
      return prepareNvidiaExecution(requestedInput, nvidiaPreparationDeps())
    }
  )
  ipcMain.handle('nvidia:research:prepare', async (e, rawTarget: NvidiaCapabilityTargetInput) => {
    requireMainRenderer(e.sender)
    const current = requireCurrentNvidiaTarget(rawTarget, rawTarget?.model)
    const binding = current.binding
    const model = current.model!
    const expectedRevision = nvidiaCapabilityRevision.snapshot()
    const capability = capabilityCache().get({ ...binding, model })
    if (capability?.capabilities.tools !== 'supported') {
      throw new Error('NVIDIA 조사 채팅에는 최신 도구 기능 확인이 필요합니다.')
    }
    await prepareNvidiaExecution(binding, nvidiaPreparationDeps())
    await requireUnchangedNvidiaTarget(binding, model, expectedRevision)
    const rechecked = capabilityCache().get({ ...binding, model })
    if (rechecked?.capabilities.tools !== 'supported') {
      throw new Error('NVIDIA 도구 기능 확인이 만료되거나 변경되었습니다.')
    }
    const ttlSeconds = capabilityBoundGrantTtlSeconds(rechecked.checkedAt)
    if (ttlSeconds < 1) throw new Error('NVIDIA 도구 기능 확인이 만료되었습니다.')
    const grant = await issueBackendNvidiaResearchGrant({
      ...binding,
      model,
      ttlSeconds
    })
    try {
      await requireUnchangedNvidiaTarget(binding, model, expectedRevision)
      if (capabilityCache().get({ ...binding, model })?.capabilities.tools !== 'supported') {
        throw new Error('NVIDIA 도구 기능 확인이 변경되었습니다.')
      }
      return grant
    } catch (error) {
      await clearBackendNvidiaResearchGrants().catch(() => {})
      throw error
    }
  })
  ipcMain.handle('nvidia:models:refresh', async (e, requestedInput: NvidiaCredentialBindingInput) => {
    requireMainRenderer(e.sender)
    nvidiaAgentDataApprovals.clearAll()
    await invalidateDiscordNvidiaRuntime()
    const { binding } = requireCurrentNvidiaTarget(requestedInput)
    const expectedRevision = nvidiaCapabilityRevision.snapshot()
    await prepareNvidiaExecution(binding, nvidiaPreparationDeps())
    const models = await fetchBackendNvidiaModels(binding.deploymentMode, binding.endpoint)
    await requireUnchangedNvidiaTarget(binding, undefined, expectedRevision)
    await commitNvidiaCapabilityMutation(
      nvidiaCapabilityRevision,
      revokeBackendNvidiaAllToolTrust,
      () => capabilityCache().removeModelsNotInList(binding, models),
      () => capabilityCache().clearBinding(binding)
    )
    return { models, refreshedAt: new Date().toISOString() }
  })
  ipcMain.handle('nvidia:capabilities:status', (e, target: NvidiaCapabilityTargetInput) => {
    requireMainRenderer(e.sender)
    return capabilityCache().get(target)
  })
  ipcMain.handle('nvidia:capabilities:clear', async (e, target: NvidiaCapabilityTargetInput) => {
    requireMainRenderer(e.sender)
    nvidiaAgentDataApprovals.clearAll()
    await invalidateDiscordNvidiaRuntime()
    await commitNvidiaCapabilityMutation(
      nvidiaCapabilityRevision,
      revokeBackendNvidiaAllToolTrust,
      () => capabilityCache().clearTarget(target),
      () => capabilityCache().clearTarget(target)
    )
  })
  ipcMain.handle('nvidia:capabilities:probe', async (e, target: NvidiaCapabilityTargetInput) => {
    requireMainRenderer(e.sender)
    nvidiaAgentDataApprovals.clearAll()
    await invalidateDiscordNvidiaRuntime()
    const current = requireCurrentNvidiaTarget(target, target.model)
    const binding = current.binding
    const model = current.model!
    const expectedRevision = nvidiaCapabilityRevision.snapshot()
    await prepareNvidiaExecution(binding, nvidiaPreparationDeps())
    const capabilities = await probeBackendNvidiaCapabilities(
      binding.deploymentMode,
      binding.endpoint,
      model
    )
    await requireUnchangedNvidiaTarget(binding, model, expectedRevision)
    return commitNvidiaCapabilityMutation(
      nvidiaCapabilityRevision,
      revokeBackendNvidiaAllToolTrust,
      () => capabilityCache().put({ ...binding, model }, capabilities),
      () => capabilityCache().clearTarget({ ...binding, model })
    )
  })
  ipcMain.handle('nvidia:agent:finish', async (e, rawInput: NvidiaAgentSessionFinishInput) => {
    requireMainRenderer(e.sender)
    const sessionId = rawInput?.sessionId
    if (
      typeof sessionId !== 'string' || sessionId.length < 16 || sessionId.length > 256 ||
      !/^[A-Za-z0-9._:-]+$/.test(sessionId)
    ) {
      throw new Error('NVIDIA Agent 세션 형식이 올바르지 않습니다.')
    }
    nvidiaAgentDataApprovals.clearSession(sessionId)
    await revokeBackendNvidiaAgentTrust()
  })
  ipcMain.handle('nvidia:agent:prepare', async (e, rawInput: unknown) => {
    requireMainRenderer(e.sender)
    const input = validateNvidiaAgentPrepareInput(rawInput)
    // 이전 실행의 bearer를 먼저 폐기한 뒤, Renderer가 고른 capability boolean이 아니라
    // 저장된 설정·private Comfy registry·현재 권한 모드로 exact scope를 새로 만든다.
    nvidiaAgentDataApprovals.clearSession(input.sessionId)
    await revokeBackendNvidiaAgentTrust()
    const settings = loadSettings()
    const registry = listComfyModelProfiles()
    const request = buildAutomaticNvidiaAgentDataScope(
      settings,
      registry.profiles,
      input.selectedComfyModelId
    )
    nvidiaAgentDataApprovals.authorizePolicy(buildNvidiaAgentManifestAuthority(
      settings,
      input.sessionId,
      request,
      registry.profiles,
      input.approvalMode
    ))
    try {
      return await prepareNvidiaAgentAuthorization(input, {
        loadSettings,
        getCapability: (target) => capabilityCache().get(target),
        revisionSnapshot: () => nvidiaCapabilityRevision.snapshot(),
        revisionIsCurrent: (revision) => nvidiaCapabilityRevision.isCurrent(revision),
        prepareExecution: async (binding) => {
          await prepareNvidiaExecution(binding, nvidiaPreparationDeps())
        },
        issueGrant: issueBackendNvidiaAgentGrant,
        revokeGrants: revokeBackendNvidiaAgentTrust,
        dataApprovalSnapshot: () => nvidiaAgentDataApprovals.snapshot(),
        dataApprovalIsCurrent: (revision) => nvidiaAgentDataApprovals.isCurrent(revision),
        getApprovedScope: (sessionId, target) => currentApprovedNvidiaAgentScope(sessionId, target),
        consumeApprovedScope: (sessionId, target) => currentApprovedNvidiaAgentScope(sessionId, target, true),
        now: Date.now
      })
    } catch (error) {
      nvidiaAgentDataApprovals.clearSession(input.sessionId)
      throw error
    }
  })
  onBackendChange((i) => {
    BrowserWindow.getAllWindows().forEach((w) => w.webContents.send('backend:status', i))
    // 백엔드가 준비되면 항상 디스코드 설정을 적용한다. enabled=false여도 apply는 예약 저장소를
    // configure(data_dir)로 초기화하므로, 봇을 껐어도 설정 탭 예약 목록이 디스크값을 반영한다
    // (봇 미활성 시 apply_config는 봇을 시작하지 않고 저장소만 준비하고 반환).
    if (i.state === 'ready') {
      void applyTrustedDiscordConfig()
    } else {
      nvidiaAgentDataApprovals.clearAll()
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
  ipcMain.handle('discord:set-llm-provider', async (e, provider: unknown) => {
    requireMainRenderer(e.sender)
    if (provider !== 'ollama' && provider !== 'nvidia') {
      throw new Error('지원하지 않는 Discord LLM 공급자입니다.')
    }
    if (provider === 'ollama') {
      trustedDiscordNvidiaRuntime = null
      await revokeBackendNvidiaDiscordTrust()
      const next = saveSettings({ discordLlmProvider: 'ollama' })
      await applyTrustedDiscordConfig()
      await clearNvidiaCredentialWithoutDemand(next)
      return next
    }

    const before = loadSettings()
    const binding = canonicalizeNvidiaBinding({
      deploymentMode: before.nvidiaDeploymentMode,
      endpoint: before.nvidiaDeploymentMode === 'nim' ? before.nvidiaNimEndpoint : undefined
    })
    const model = before.nvidiaModel.trim()
    if (!model) throw new Error('Discord에 사용할 NVIDIA 모델을 먼저 설정하세요.')
    const consentTarget = { ...binding, model }
    const consentRevision = nvidiaCapabilityRevision.snapshot()
    assertNvidiaDiscordConsentCurrent(
      consentTarget,
      before.discordLlmProvider,
      consentRevision,
      discordNvidiaFenceDeps()
    )
    const win = BrowserWindow.fromWebContents(e.sender)
    const decision = await dialog.showMessageBox(win!, {
      type: 'warning',
      title: 'Discord NVIDIA 실험 기능 확인',
      message: 'Discord 대화를 NVIDIA 모델로 처리할까요?',
      detail: [
        `대상: ${binding.deploymentMode === 'build' ? 'NVIDIA Build' : binding.endpoint} / ${model}`,
        '전송: Discord 사용자의 메시지, 최근 대화 문맥, 도구 호출 결과, 웹 조사 결과',
        '로컬 전용: NVIDIA API 키, Discord 봇 토큰, 승인 토큰',
        '이 기능은 실험적이며 앱 재시작·모델/대상/키/기능 변경 후 다시 확인해야 합니다.'
      ].join('\n'),
      buttons: ['확인하고 활성화', '취소'],
      defaultId: 1,
      cancelId: 1,
      noLink: true
    })
    if (decision.response !== 0) return loadSettings()

    // The native dialog can remain open while settings or capability evidence
    // changes. Never treat consent for an old target as consent for the new one.
    assertNvidiaDiscordConsentCurrent(
      consentTarget,
      before.discordLlmProvider,
      consentRevision,
      discordNvidiaFenceDeps()
    )

    saveSettings({ discordLlmProvider: 'nvidia' })
    try {
      await prepareNvidiaExecution(binding, nvidiaPreparationDeps(), 'discord')
      assertNvidiaDiscordConsentCurrent(
        consentTarget,
        'nvidia',
        consentRevision,
        discordNvidiaFenceDeps()
      )
      trustedDiscordNvidiaRuntime = {
        provider: 'nvidia',
        deploymentMode: binding.deploymentMode,
        endpoint: binding.endpoint,
        model
      }
      const applied = await applyTrustedDiscordConfig()
      if (!applied.ok) throw new Error(applied.detail ?? 'Discord NVIDIA 적용에 실패했습니다.')
      return loadSettings()
    } catch (error) {
      trustedDiscordNvidiaRuntime = null
      await revokeBackendNvidiaDiscordTrust().catch(() => {})
      if (before.discordLlmProvider !== 'nvidia') saveSettings({ discordLlmProvider: 'ollama' })
      await applyTrustedDiscordConfig().catch(() => stopBackend())
      await clearNvidiaCredentialWithoutDemand().catch(() => {})
      throw error
    }
  })
  ipcMain.handle('discord:apply', () => applyTrustedDiscordConfig())
  ipcMain.handle('discord:status', () => discordStatus())
  ipcMain.handle('discord:schedules', () => discordSchedules())
  ipcMain.handle('discord:schedule-remove', (_e, id: string) => discordScheduleRemove(id))

  // ---- IPC: 대화방 (userData/conversations.json) ----
  ipcMain.handle('conv:list', (_e, kind: ConversationKind) => listConversations(kind))
  ipcMain.handle('conv:get', (_e, id: string) => getConversation(id))
  ipcMain.handle('conv:save', (_e, c: ConversationSave) => saveConversation(c))
  ipcMain.handle('conv:pin', (_e, id: string, pinned: boolean) => setConversationPinned(id, pinned))
  ipcMain.handle('conv:rename', (_e, id: string, title: string) => renameConversation(id, title))
  ipcMain.handle('conv:delete', (_e, id: string) => {
    deleteConversation(id)
    // 이 대화만 참조하던 첨부를 회수한다. best-effort — 실패해도 삭제는 이미 끝났다.
    deferUnreferencedAttachmentSweep()
  })
  ipcMain.handle('project:list', () => listAgentProjects())
  ipcMain.handle('project:create', (_e, title: string) => createAgentProject(title))
  ipcMain.handle('project:create-conversation', (_e, projectId: string, title?: string) =>
    createAgentProjectConversation(projectId, title)
  )
  ipcMain.handle('project:start', (_e, id: string) => startAgentProject(id))

  // ---- IPC: 공장초기화 (개발자 모드) — userData의 앱 데이터 삭제(설정·대화·사용량) ----
  ipcMain.handle('app:factory-reset', async () => {
    // 삭제 전에 쓰기를 잠근다 — 진행 중이던 스트림의 지연 저장이 지운 파일을 되살리지 않게(리로드 시간 확보)
    freezeAppDataWrites()
    nvidiaAgentDataApprovals.clearAll()
    trustedDiscordNvidiaRuntime = null
    await revokeBackendNvidiaAllToolTrust().catch(() => {})
    await clearBackendNvidiaCredential().catch(() => {})
    deleteNvidiaCredential()
    invalidateAllNvidiaCapabilities()
    resetSettings()
    clearAllConversations()
    // "처음 설치 상태로 돌아갑니다"를 지키려면 첨부도 지워야 한다. 참조 스윕이 아니라
    // 통째 삭제이며 유예 기간을 적용하지 않는다 — 초기화의 약속이 그렇다.
    await clearAttachmentStore()
    clearBackendTodoWorkspaceRegistry()
    clearUsage()
    clearComfyModelRegistry() // Aiso 메타데이터만 삭제하며 ComfyUI의 실제 모델 파일은 보존한다.
    // 디스코드 비밀·상태·예약도 지우고(리셋 후 설정은 기본값=봇 꺼짐), 실행 중 봇을 중지한다.
    clearDiscordData()
    void applyTrustedDiscordConfig() // 설정 기본값(discordEnabled=false)+토큰 삭제 → 사이드카 봇 중지
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
  deferStaleComfyModelPartialCleanup()
  deferUnreferencedAttachmentSweep()
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
  if (myDbDailyReportTimer) clearInterval(myDbDailyReportTimer)
  myDbDailyReportTimer = null
  destroyComfySurface()
  stopManagedComfyUI()
  stopBackend()
  closeMyDbStorage()
})

app.on('window-all-closed', () => {
  // 상주 모드에선 창을 닫아도(=숨겨도) 앱을 살려 봇·예약을 유지한다.
  // (close 핸들러가 창을 hide로 막으므로 상주 시엔 이 이벤트가 거의 안 오지만, 안전 가드로 둔다.)
  if (residencyOn()) return
  if (process.platform !== 'darwin') {
    app.quit()
  }
})
