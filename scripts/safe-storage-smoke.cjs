const { app, safeStorage } = require('electron')
const { randomBytes } = require('crypto')

const fail = (error) => {
  process.stdout.write(JSON.stringify({
    ok: false,
    error: 'safe-storage-smoke-failed',
    detail: error instanceof Error ? error.message : String(error ?? 'timeout'),
    asyncAvailableType: typeof safeStorage.isAsyncEncryptionAvailable,
    asyncEncryptType: typeof safeStorage.encryptStringAsync,
    asyncDecryptType: typeof safeStorage.decryptStringAsync
  }))
  app.exit(1)
}

app.whenReady().then(async () => {
  const available = await safeStorage.isAsyncEncryptionAvailable()
  const backend = process.platform === 'linux'
    ? safeStorage.getSelectedStorageBackend()
    : 'platform_native'
  if (!available || backend === 'basic_text') {
    process.stdout.write(JSON.stringify({ ok: false, available, backend }))
    app.exit(2)
    return
  }
  const secret = randomBytes(32).toString('hex')
  const encrypted = await safeStorage.encryptStringAsync(secret)
  const decrypted = await safeStorage.decryptStringAsync(encrypted)
  process.stdout.write(JSON.stringify({
    ok: decrypted.result === secret,
    available,
    backend,
    shouldReEncrypt: decrypted.shouldReEncrypt,
    ciphertextBytes: encrypted.length
  }))
  app.exit(decrypted.result === secret ? 0 : 3)
}).catch(fail)

setTimeout(() => fail('timeout'), 15_000).unref()
