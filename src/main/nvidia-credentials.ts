import { app, safeStorage } from 'electron'
import type { NvidiaCredentialBindingInput, NvidiaCredentialStatus } from '../shared/nvidia'
import { NvidiaCredentialStore, type AsyncSafeCrypto } from './nvidia-credential-store'
import { nvidiaCredentialPath } from './nvidia-credential-path'

const electronSafeCrypto: AsyncSafeCrypto = {
  isAvailable: () => safeStorage.isAsyncEncryptionAvailable(),
  // Electron only exposes the selected backend on Linux at runtime. Other
  // platforms use their native protected store and are governed by availability.
  backend: () => process.platform === 'linux' ? safeStorage.getSelectedStorageBackend() : 'platform_native',
  encrypt: (plaintext) => safeStorage.encryptStringAsync(plaintext),
  decrypt: (ciphertext) => safeStorage.decryptStringAsync(ciphertext)
}

function store(): NvidiaCredentialStore {
  return new NvidiaCredentialStore(
    nvidiaCredentialPath(app.getPath('userData'), app.isPackaged),
    electronSafeCrypto
  )
}

export async function saveNvidiaCredential(binding: NvidiaCredentialBindingInput, apiKey: unknown): Promise<void> {
  await store().save(binding, apiKey)
}

export async function nvidiaCredentialStatus(binding?: NvidiaCredentialBindingInput): Promise<NvidiaCredentialStatus> {
  return store().status(binding)
}

/** Main-only Gate 3 primitive. Never expose this function through preload/IPC. */
export async function readNvidiaCredentialForTransfer(binding: NvidiaCredentialBindingInput): Promise<string> {
  return store().readForTransfer(binding)
}

export function deleteNvidiaCredential(): void {
  store().delete()
}
