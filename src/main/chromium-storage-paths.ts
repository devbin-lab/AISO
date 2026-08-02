import { join } from 'path'

export interface ChromiumStoragePathsInput {
  userData: string
  temp: string
  isDev: boolean
  pid: number
}

export interface ChromiumStoragePaths {
  /**
   * Must remain stable across launches. Electron async safeStorage keeps its
   * protected key metadata in Chromium's Local State under sessionData.
   */
  sessionData: string
  /** HTTP/GPU cache may be isolated per development process to avoid lock races. */
  diskCache: string
}

export function chromiumStoragePaths(input: ChromiumStoragePathsInput): ChromiumStoragePaths {
  return {
    sessionData: join(input.userData, input.isDev ? 'chromium-session-dev' : 'chromium-session'),
    diskCache: input.isDev
      ? join(input.temp, `aiso-chromium-cache-${input.pid}`)
      : join(input.userData, 'chromium-cache')
  }
}
