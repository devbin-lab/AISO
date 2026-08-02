import assert from 'node:assert/strict'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { NVIDIA_BUILD_BASE_URL } from '../shared/nvidia.ts'
import {
  NvidiaCredentialStore,
  type AsyncSafeCrypto,
  type CredentialFileOps
} from './nvidia-credential-store.ts'

function tempFile(t: test.TestContext): string {
  const dir = mkdtempSync(join(tmpdir(), 'aiso-credential-v4-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  return join(dir, 'nvidia-credential.json')
}

function fakeCrypto(options?: { available?: boolean; backend?: string; failEncrypt?: boolean }) {
  let generation = 0
  let rotate = false
  const adapter: AsyncSafeCrypto = {
    isAvailable: async () => options?.available !== false,
    backend: () => options?.backend ?? 'dpapi',
    encrypt: async (plaintext) => {
      if (options?.failEncrypt) throw new Error('temporary failure')
      generation += 1
      const mask = generation % 251 || 1
      return Buffer.concat([Buffer.from([mask]), Buffer.from(plaintext).map((byte) => byte ^ mask)])
    },
    decrypt: async (ciphertext) => {
      const mask = ciphertext[0]
      const result = Buffer.from(ciphertext.subarray(1).map((byte) => byte ^ mask)).toString('utf8')
      return { result, shouldReEncrypt: rotate }
    }
  }
  return {
    adapter,
    setRotate: (value: boolean) => { rotate = value },
    encryptions: () => generation
  }
}

const realOps: CredentialFileOps = {
  exists: existsSync,
  mkdir: (path) => mkdirSync(path, { recursive: true }),
  read: (path) => readFileSync(path, 'utf8'),
  rename: renameSync,
  remove: (path) => rmSync(path, { force: true }),
  writeExclusive: (path, contents) => writeFileSync(path, contents, { encoding: 'utf8', flag: 'wx' })
}

test('save/restart/status never persist or return the plaintext canary', async (t) => {
  const file = tempFile(t)
  const crypto = fakeCrypto()
  const canary = 'CANARY-NVIDIA-KEY-48721'
  await new NvidiaCredentialStore(file, crypto.adapter).save({ deploymentMode: 'build' }, canary)
  const persisted = readFileSync(file, 'utf8')
  assert.equal(persisted.includes(canary), false)
  const restarted = new NvidiaCredentialStore(file, crypto.adapter)
  const status = await restarted.status({ deploymentMode: 'build' })
  assert.equal(status.hasStoredCredential, true)
  assert.equal(status.matchesCurrentBinding, true)
  assert.equal(JSON.stringify(status).includes(canary), false)
  assert.equal(await restarted.readForTransfer({ deploymentMode: 'build' }), canary)
})

test('Build credential is never reused for user NIM', async (t) => {
  const file = tempFile(t)
  const crypto = fakeCrypto()
  const store = new NvidiaCredentialStore(file, crypto.adapter)
  await store.save({ deploymentMode: 'build' }, 'key')
  const status = await store.status({ deploymentMode: 'nim', endpoint: 'https://nim.example.com/v1' })
  assert.equal(status.matchesCurrentBinding, false)
  await assert.rejects(
    store.readForTransfer({ deploymentMode: 'nim', endpoint: 'https://nim.example.com/v1' }),
    /일치하지 않습니다/
  )
})

test('user NIM credential is not reused after an endpoint change', async (t) => {
  const file = tempFile(t)
  const crypto = fakeCrypto()
  const store = new NvidiaCredentialStore(file, crypto.adapter)
  await store.save({ deploymentMode: 'nim', endpoint: 'https://nim-a.example/v1' }, 'nim-key')
  const status = await store.status({ deploymentMode: 'nim', endpoint: 'https://nim-b.example/v1' })
  assert.equal(status.matchesCurrentBinding, false)
  await assert.rejects(
    store.readForTransfer({ deploymentMode: 'nim', endpoint: 'https://nim-b.example/v1' }),
    /일치하지 않습니다/
  )
})

test('tampering with plaintext binding metadata is detected after decrypt', async (t) => {
  const file = tempFile(t)
  const crypto = fakeCrypto()
  const store = new NvidiaCredentialStore(file, crypto.adapter)
  await store.save({ deploymentMode: 'build' }, 'bound-key')
  const envelope = JSON.parse(readFileSync(file, 'utf8'))
  envelope.binding = { deploymentMode: 'nim', endpoint: 'https://nim.example.com/v1' }
  writeFileSync(file, JSON.stringify(envelope))
  await assert.rejects(
    store.readForTransfer({ deploymentMode: 'nim', endpoint: 'https://nim.example.com/v1' }),
    /바인딩 검증/
  )
})

test('shouldReEncrypt immediately rotates the stored ciphertext', async (t) => {
  const file = tempFile(t)
  const crypto = fakeCrypto()
  const store = new NvidiaCredentialStore(file, crypto.adapter)
  await store.save({ deploymentMode: 'build' }, 'rotate-me')
  const before = readFileSync(file, 'utf8')
  crypto.setRotate(true)
  assert.equal(await store.readForTransfer({ deploymentMode: 'build' }), 'rotate-me')
  assert.equal(crypto.encryptions(), 2)
  assert.notEqual(readFileSync(file, 'utf8'), before)
})

for (const scenario of [
  { name: 'basic_text', options: { backend: 'basic_text' } },
  { name: 'unavailable', options: { available: false } },
  { name: 'temporary encryption failure', options: { failEncrypt: true } }
]) {
  test(`credential save refuses ${scenario.name} without plaintext fallback`, async (t) => {
    const file = tempFile(t)
    const crypto = fakeCrypto(scenario.options)
    await assert.rejects(new NvidiaCredentialStore(file, crypto.adapter).save({ deploymentMode: 'build' }, 'CANARY'))
    assert.equal(existsSync(file), false)
  })
}

test('encryption exceptions never echo the plaintext key', async (t) => {
  const file = tempFile(t)
  const canary = 'CANARY-ENCRYPTION-ERROR-44182'
  const crypto: AsyncSafeCrypto = {
    isAvailable: async () => true,
    backend: () => 'dpapi',
    encrypt: async () => { throw new Error(canary) },
    decrypt: async () => { throw new Error(canary) }
  }
  await assert.rejects(
    new NvidiaCredentialStore(file, crypto).save({ deploymentMode: 'build' }, canary),
    (error: Error) => !error.message.includes(canary)
  )
})

test('failed credential replacement preserves the previous encrypted file', async (t) => {
  const file = tempFile(t)
  const crypto = fakeCrypto()
  await new NvidiaCredentialStore(file, crypto.adapter).save({ deploymentMode: 'build' }, 'old')
  const previous = readFileSync(file, 'utf8')
  const failingOps: CredentialFileOps = { ...realOps, rename: () => { throw new Error('rename failure') } }
  await assert.rejects(
    new NvidiaCredentialStore(file, crypto.adapter, failingOps).save({ deploymentMode: 'build' }, 'new')
  )
  assert.equal(readFileSync(file, 'utf8'), previous)
})

test('delete removes the encrypted credential', async (t) => {
  const file = tempFile(t)
  const crypto = fakeCrypto()
  const store = new NvidiaCredentialStore(file, crypto.adapter)
  await store.save({ deploymentMode: 'build', endpoint: NVIDIA_BUILD_BASE_URL }, 'key')
  store.delete()
  assert.equal(existsSync(file), false)
})
