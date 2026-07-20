import { spawn, type ChildProcess } from 'child_process'
import { BrowserWindow, WebContentsView, shell } from 'electron'
import { existsSync } from 'fs'
import { connect } from 'net'
import { basename, dirname, join, resolve } from 'path'
import type { ComfyLaunchResult, ComfySurfaceRequest } from '../shared/comfy'

interface LocalComfyEndpoint {
  baseUrl: string
  hostname: string
  port: number
}

let managedProcess: ChildProcess | null = null
let managedProcessBaseUrl: string | null = null
let managedPortableRoot: string | null = null

interface ManagedSurface {
  owner: BrowserWindow
  view: WebContentsView
  allowedOrigin: string
  loadedUrl: string
}

let managedSurface: ManagedSurface | null = null

/** Electron 메인에서도 loopback 주소만 허용해 렌더러 입력이 프로세스 실행 인자로 번지지 않게 한다. */
function parseLocalEndpoint(raw: string): LocalComfyEndpoint {
  let url: URL
  try {
    url = new URL(raw.trim())
  } catch {
    throw new Error('ComfyUI 주소 형식이 올바르지 않습니다.')
  }
  const allowedHosts = new Set(['127.0.0.1', 'localhost', '[::1]'])
  if (url.protocol !== 'http:' || !allowedHosts.has(url.hostname)) {
    throw new Error('ComfyUI 주소는 이 PC의 loopback HTTP 주소만 사용할 수 있습니다.')
  }
  if (url.username || url.password || url.search || url.hash || (url.pathname && url.pathname !== '/')) {
    throw new Error('ComfyUI 주소에는 경로, 계정 정보, 쿼리 또는 fragment를 넣을 수 없습니다.')
  }
  const port = Number(url.port || '80')
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('ComfyUI 포트가 올바르지 않습니다.')
  }
  return { baseUrl: url.origin, hostname: url.hostname, port }
}

function sameOrigin(url: string, allowedOrigin: string): boolean {
  try {
    return new URL(url).origin === allowedOrigin
  } catch {
    return false
  }
}

function openExternal(url: string): void {
  try {
    const parsed = new URL(url)
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      void shell.openExternal(url).catch(() => {})
    }
  } catch {
    // 비정상 URL은 열지 않는다.
  }
}

function destroySurface(): void {
  const current = managedSurface
  managedSurface = null
  if (!current) return
  try {
    current.owner.contentView.removeChildView(current.view)
  } catch {
    // 창 종료 중 이미 분리된 뷰는 무시한다.
  }
  if (!current.view.webContents.isDestroyed()) {
    current.view.webContents.close({ waitForBeforeUnload: false })
  }
}

function createSurface(owner: BrowserWindow): ManagedSurface {
  destroySurface()
  const view = new WebContentsView({
    webPreferences: {
      partition: 'persist:aiso-comfy-ui',
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
      safeDialogs: true,
      spellcheck: false
    }
  })
  view.setVisible(false)
  owner.contentView.addChildView(view)
  const surface: ManagedSurface = { owner, view, allowedOrigin: '', loadedUrl: '' }
  managedSurface = surface

  const wc = view.webContents
  wc.setWindowOpenHandler(({ url }) => {
    openExternal(url)
    return { action: 'deny' }
  })
  const guardNavigation = (event: Electron.Event, url: string): void => {
    if (sameOrigin(url, surface.allowedOrigin)) return
    event.preventDefault()
    openExternal(url)
  }
  wc.on('will-navigate', guardNavigation)
  wc.on('will-redirect', guardNavigation)
  wc.session.setPermissionCheckHandler(() => false)
  wc.session.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false))
  owner.once('closed', () => {
    if (managedSurface === surface) destroySurface()
  })
  return surface
}

function loadSurfaceUrl(surface: ManagedSurface): void {
  if (!surface.allowedOrigin) return
  const loadingUrl = surface.allowedOrigin
  surface.loadedUrl = loadingUrl
  void surface.view.webContents.loadURL(`${loadingUrl}/`).catch(() => {
    if (managedSurface === surface && surface.loadedUrl === loadingUrl) {
      surface.loadedUrl = ''
      surface.view.setVisible(false)
    }
  })
}

/** ComfyUI를 top-level 격리 WebContents로 로드해 file:// iframe의 Origin 403 없이 앱 안에 배치한다. */
export function setComfySurface(owner: BrowserWindow, request: ComfySurfaceRequest): void {
  if (!request || typeof request.visible !== 'boolean' || typeof request.baseUrl !== 'string') {
    throw new Error('ComfyUI 화면 요청 형식이 올바르지 않습니다.')
  }
  if (!request.visible) {
    managedSurface?.view.setVisible(false)
    return
  }
  const endpoint = parseLocalEndpoint(request.baseUrl)
  if (!request.bounds) throw new Error('ComfyUI 화면 위치가 없습니다.')
  const rawBounds = [request.bounds.x, request.bounds.y, request.bounds.width, request.bounds.height]
  if (!rawBounds.every((value) => typeof value === 'number' && Number.isFinite(value))) {
    throw new Error('ComfyUI 화면 위치 형식이 올바르지 않습니다.')
  }
  const [contentWidth, contentHeight] = owner.getContentSize()
  const x = Math.max(0, Math.round(request.bounds.x))
  const y = Math.max(0, Math.round(request.bounds.y))
  const width = Math.max(0, Math.min(Math.round(request.bounds.width), contentWidth - x))
  const height = Math.max(0, Math.min(Math.round(request.bounds.height), contentHeight - y))
  if (width < 1 || height < 1) {
    managedSurface?.view.setVisible(false)
    return
  }

  const surface = !managedSurface || managedSurface.owner !== owner
    ? createSurface(owner)
    : managedSurface
  surface.allowedOrigin = endpoint.baseUrl
  surface.view.setBounds({ x, y, width, height })
  if (surface.loadedUrl !== endpoint.baseUrl) {
    loadSurfaceUrl(surface)
  }
  surface.view.setVisible(true)
}

export function reloadComfySurface(): void {
  const surface = managedSurface
  const wc = surface?.view.webContents
  if (!surface || !wc || wc.isDestroyed()) return
  surface.view.setVisible(true)
  if (surface.loadedUrl) wc.reload()
  else loadSurfaceUrl(surface)
}

/** 사용자가 portable 최상위나 그 안의 ComfyUI 폴더 중 어느 쪽을 골라도 최상위로 맞춘다. */
function resolvePortableRoot(selectedPath: string): string | null {
  if (!selectedPath.trim()) return null
  const selected = resolve(selectedPath.trim())
  const candidates = [selected]
  if (basename(selected).toLowerCase() === 'comfyui') candidates.push(dirname(selected))
  for (const root of candidates) {
    if (
      existsSync(join(root, 'python_embeded', 'python.exe')) &&
      existsSync(join(root, 'ComfyUI', 'main.py'))
    ) {
      return root
    }
  }
  return null
}

function portIsOpen(endpoint: LocalComfyEndpoint): Promise<boolean> {
  const host = endpoint.hostname === 'localhost'
    ? '127.0.0.1'
    : endpoint.hostname === '[::1]'
      ? '::1'
      : endpoint.hostname
  return new Promise((resolveOpen) => {
    const socket = connect({ host, port: endpoint.port })
    let settled = false
    const finish = (open: boolean): void => {
      if (settled) return
      settled = true
      socket.destroy()
      resolveOpen(open)
    }
    socket.setTimeout(600)
    socket.once('connect', () => finish(true))
    socket.once('timeout', () => finish(false))
    socket.once('error', () => finish(false))
  })
}

function samePortableRoot(left: string | null, right: string | null): boolean {
  if (!left || !right) return left === right
  return process.platform === 'win32'
    ? left.toLowerCase() === right.toLowerCase()
    : left === right
}

/** 주소나 설치본 변경 시 GPU를 이중 점유하지 않도록 기존 관리 프로세스 종료를 기다린다. */
function stopManagedComfyUIForRestart(): Promise<boolean> {
  const child = managedProcess
  if (!child || child.exitCode !== null || child.killed) return Promise.resolve(true)
  return new Promise((resolveStopped) => {
    let settled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    const finish = (stopped: boolean): void => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      child.off('exit', onExit)
      child.off('error', onError)
      resolveStopped(stopped)
    }
    const onExit = (): void => finish(true)
    const onError = (): void => finish(false)
    child.once('exit', onExit)
    child.once('error', onError)
    try {
      if (!child.kill()) {
        finish(false)
        return
      }
    } catch {
      finish(false)
      return
    }
    timer = setTimeout(() => finish(child.exitCode !== null), 3_000)
  })
}

/** 사용자가 고른 Windows Portable 설치본만 별도 프로세스로 시작한다. 모델/노드는 수정하지 않는다. */
export async function startComfyUI(
  installPath: string,
  baseUrl: string
): Promise<ComfyLaunchResult> {
  try {
    const endpoint = parseLocalEndpoint(baseUrl)
    const requestedRoot = resolvePortableRoot(installPath)
    let targetIsOpen = await portIsOpen(endpoint)
    if (managedProcess && managedProcess.exitCode === null && !managedProcess.killed) {
      const endpointChanged = managedProcessBaseUrl !== endpoint.baseUrl
      const portableChanged = requestedRoot != null && !samePortableRoot(managedPortableRoot, requestedRoot)
      if (!endpointChanged && !portableChanged) {
        if (targetIsOpen) {
          return {
            ok: true,
            state: 'already-running',
            detail: '설정한 로컬 포트가 이미 사용 중입니다. ComfyUI 응답을 확인합니다.'
          }
        }
        return { ok: true, state: 'already-started', pid: managedProcess.pid }
      }
      // 새 대상이 닫혀 있는데 설치 경로도 무효라면 작동 중인 기존 인스턴스를 보존한다.
      if (!targetIsOpen && !requestedRoot) {
        return {
          ok: false,
          state: 'error',
          detail: '새 ComfyUI를 시작할 설치 폴더가 올바르지 않아 기존 인스턴스를 유지합니다.'
        }
      }
      if (!await stopManagedComfyUIForRestart()) {
        return {
          ok: false,
          state: 'error',
          detail: '기존 ComfyUI 프로세스를 종료하지 못했습니다. Aiso를 다시 시작해 주세요.'
        }
      }
      targetIsOpen = await portIsOpen(endpoint)
    }
    if (targetIsOpen) {
      return {
        ok: true,
        state: 'already-running',
        detail: '설정한 로컬 포트가 이미 사용 중입니다. ComfyUI 응답을 확인합니다.'
      }
    }
    const root = requestedRoot
    if (!root) {
      return {
        ok: false,
        state: 'error',
        detail: 'ComfyUI Windows Portable 최상위 폴더를 다시 선택해 주세요.'
      }
    }

    const pythonExe = join(root, 'python_embeded', 'python.exe')
    const mainPy = join(root, 'ComfyUI', 'main.py')
    const listenHost = endpoint.hostname === '[::1]' ? '::1' : '127.0.0.1'
    const child = spawn(
      pythonExe,
      [
        '-s',
        mainPy,
        '--windows-standalone-build',
        '--listen',
        listenHost,
        '--port',
        String(endpoint.port),
        '--disable-auto-launch'
      ],
      { cwd: root, windowsHide: true, stdio: 'ignore' }
    )
    managedProcess = child
    managedProcessBaseUrl = endpoint.baseUrl
    managedPortableRoot = root
    child.once('exit', () => {
      if (managedProcess === child) {
        managedProcess = null
        managedProcessBaseUrl = null
        managedPortableRoot = null
      }
    })
    child.once('error', () => {
      if (managedProcess === child) {
        managedProcess = null
        managedProcessBaseUrl = null
        managedPortableRoot = null
      }
    })
    return await new Promise<ComfyLaunchResult>((resolveLaunch) => {
      child.once('spawn', () => resolveLaunch({ ok: true, state: 'started', pid: child.pid }))
      child.once('error', (error) => {
        resolveLaunch({ ok: false, state: 'error', detail: `ComfyUI 실행 실패: ${error.message}` })
      })
    })
  } catch (err) {
    return { ok: false, state: 'error', detail: err instanceof Error ? err.message : String(err) }
  }
}

/** Aiso가 시작한 프로세스만 종료한다. 사용자가 직접 실행한 ComfyUI는 건드리지 않는다. */
export function stopManagedComfyUI(): void {
  if (!managedProcess || managedProcess.exitCode !== null || managedProcess.killed) return
  try {
    managedProcess.kill()
  } catch {
    // 종료 중 이미 사라진 프로세스는 무시한다.
  }
  managedProcess = null
  managedProcessBaseUrl = null
  managedPortableRoot = null
}

export function destroyComfySurface(): void {
  destroySurface()
}
