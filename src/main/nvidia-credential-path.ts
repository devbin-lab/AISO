import { join } from 'path'

/** Keep development and installed-app secrets bound to their own stable Chromium key stores. */
export function nvidiaCredentialPath(userData: string, isPackaged: boolean): string {
  return join(userData, isPackaged ? 'nvidia-credential.json' : 'nvidia-credential.dev.json')
}
