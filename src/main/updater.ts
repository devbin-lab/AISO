import { app, BrowserWindow } from 'electron'
import { autoUpdater } from 'electron-updater'
import type { UpdateStatus } from '../shared/update'

/**
 * 자동 업데이트 — GitHub 릴리스(publish 설정) 기반.
 * 패키징된 앱에서만 동작한다. app-update.yml(빌드 시 생성) + 릴리스의 latest.yml 이 필요하다.
 */

let wired = false

function broadcast(status: UpdateStatus): void {
  BrowserWindow.getAllWindows().forEach((w) => w.webContents.send('update:status', status))
}

export function initUpdater(): void {
  if (wired) return
  wired = true
  autoUpdater.autoDownload = false // '다운로드' 버튼을 눌러야 받는다
  autoUpdater.autoInstallOnAppQuit = true

  autoUpdater.on('checking-for-update', () => broadcast({ state: 'checking' }))
  autoUpdater.on('update-available', (info) =>
    broadcast({
      state: 'available',
      version: info.version,
      notes: typeof info.releaseNotes === 'string' ? info.releaseNotes : undefined
    })
  )
  autoUpdater.on('update-not-available', () => broadcast({ state: 'up-to-date' }))
  autoUpdater.on('download-progress', (p) =>
    broadcast({ state: 'downloading', percent: Math.round(p.percent) })
  )
  autoUpdater.on('update-downloaded', (info) =>
    broadcast({ state: 'downloaded', version: info.version })
  )
  autoUpdater.on('error', (err) =>
    broadcast({ state: 'error', message: String((err as Error)?.message || err) })
  )
}

export async function checkForUpdates(): Promise<UpdateStatus> {
  // 개발(비패키징)에선 app-update.yml이 없어 동작 불가 — 명확히 안내.
  if (!app.isPackaged) return { state: 'dev-disabled' }
  try {
    await autoUpdater.checkForUpdates()
    return { state: 'checking' } // 이후 결과는 update:status 이벤트로 전달
  } catch (err) {
    const message = String((err as Error)?.message || err)
    broadcast({ state: 'error', message })
    return { state: 'error', message }
  }
}

export async function downloadUpdate(): Promise<UpdateStatus> {
  if (!app.isPackaged) return { state: 'dev-disabled' }
  try {
    await autoUpdater.downloadUpdate()
    return { state: 'downloading' }
  } catch (err) {
    const message = String((err as Error)?.message || err)
    broadcast({ state: 'error', message })
    return { state: 'error', message }
  }
}

export function quitAndInstall(): void {
  if (!app.isPackaged) return
  // isSilent=false(설치 UI 표시), forceRunAfter=true(설치 후 재실행)
  autoUpdater.quitAndInstall(false, true)
}
