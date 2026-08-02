import assert from 'node:assert/strict'
import test from 'node:test'
import { DEFAULT_SETTINGS } from '../shared/settings.ts'
import { NVIDIA_BUILD_BASE_URL, type NvidiaCapabilitySnapshot } from '../shared/nvidia.ts'
import {
  NvidiaDiscordApplyCoordinator,
  assertNvidiaDiscordConsentCurrent,
  type NvidiaDiscordApplyDeps,
  type TrustedNvidiaDiscordRuntime
} from './nvidia-discord-apply.ts'

const target = {
  deploymentMode: 'build' as const,
  endpoint: NVIDIA_BUILD_BASE_URL,
  model: 'model/a'
}
const capability: NvidiaCapabilitySnapshot = {
  schemaVersion: 1,
  binding: target,
  model: target.model,
  capabilities: { chat: 'supported', stream: 'supported', tools: 'supported' },
  checkedAt: '2026-08-02T00:00:00.000Z'
}

function settings() {
  return {
    ...DEFAULT_SETTINGS,
    activeLlmProvider: 'ollama' as const,
    discordEnabled: true,
    discordLlmProvider: 'nvidia' as const,
    nvidiaDeploymentMode: 'build' as const,
    nvidiaModel: target.model
  }
}

function applyHarness(overrides: Partial<NvidiaDiscordApplyDeps> = {}) {
  let revision = 1
  let trusted: TrustedNvidiaDiscordRuntime | null = { provider: 'nvidia', ...target }
  const applied: string[] = []
  let revocations = 0
  let failClosedCalls = 0
  const deps: NvidiaDiscordApplyDeps = {
    loadSettings: settings,
    revisionSnapshot: () => revision,
    revisionIsCurrent: (snapshot) => snapshot === revision,
    getCapability: () => capability,
    getTrustedRuntime: () => trusted,
    clearTrustedRuntimeIf: (expected) => {
      if (trusted === expected) trusted = null
    },
    prepareExecution: async () => {},
    issueGrant: async () => ({ grantId: 'grant-1' }),
    revokeGrants: async () => { revocations++ },
    applyConfig: async (runtime) => {
      applied.push(runtime ? 'nvidia' : 'ollama')
      return { ok: true }
    },
    disableConfig: async () => {
      applied.push('disabled')
      return { ok: true }
    },
    failClosed: () => { failClosedCalls++ },
    clearCredentialWhenUnused: async () => {},
    now: () => Date.parse('2026-08-02T00:00:01.000Z'),
    ...overrides
  }
  return {
    deps,
    applied,
    revocations: () => revocations,
    failClosedCalls: () => failClosedCalls,
    trusted: () => trusted,
    invalidate: () => { revision++ },
    clearTrusted: () => { trusted = null }
  }
}

test('native consent rejects a changed capability revision before activation', () => {
  assert.throws(() => assertNvidiaDiscordConsentCurrent(
    target,
    'nvidia',
    1,
    {
      loadSettings: settings,
      revisionIsCurrent: () => false,
      getCapability: () => capability
    }
  ), /변경되었습니다/)
})

test('apply rechecks revision after preparation and compensates without enabling NVIDIA', async () => {
  const harness = applyHarness()
  harness.deps.prepareExecution = async () => { harness.invalidate() }
  const result = await new NvidiaDiscordApplyCoordinator().apply(harness.deps)
  assert.equal(result.ok, false)
  assert.deepEqual(harness.applied, ['disabled'])
  assert.equal(harness.revocations(), 1)
  assert.equal(harness.failClosedCalls(), 0)
  assert.equal(harness.trusted(), null)
})

test('stale success after the sidecar POST is immediately compensated with disabled config', async () => {
  const harness = applyHarness()
  harness.deps.applyConfig = async (runtime) => {
    harness.applied.push(runtime ? 'nvidia' : 'ollama')
    if (runtime) {
      harness.clearTrusted()
      harness.invalidate()
    }
    return { ok: true }
  }
  const result = await new NvidiaDiscordApplyCoordinator().apply(harness.deps)
  assert.equal(result.ok, false)
  assert.deepEqual(harness.applied, ['nvidia', 'disabled'])
  assert.equal(harness.revocations(), 1)
  assert.equal(harness.failClosedCalls(), 0)
  assert.equal(harness.trusted(), null)
})

test('stale POST with a non-ok disabled compensation fails closed exactly once', async () => {
  const harness = applyHarness()
  harness.deps.applyConfig = async (runtime) => {
    harness.applied.push(runtime ? 'nvidia' : 'ollama')
    if (runtime) {
      harness.clearTrusted()
      harness.invalidate()
    }
    return { ok: true }
  }
  harness.deps.disableConfig = async () => {
    harness.applied.push('disabled')
    return { ok: false, detail: 'sanitized failure' }
  }
  const result = await new NvidiaDiscordApplyCoordinator().apply(harness.deps)
  assert.equal(result.ok, false)
  assert.match(result.detail ?? '', /백엔드를 종료/)
  assert.deepEqual(harness.applied, ['nvidia', 'disabled'])
  assert.equal(harness.failClosedCalls(), 1)
})

test('an initial untrusted apply fails closed when disabled compensation throws', async () => {
  const harness = applyHarness()
  harness.clearTrusted()
  harness.deps.disableConfig = async () => {
    harness.applied.push('disabled')
    throw new Error('timeout canary')
  }
  const result = await new NvidiaDiscordApplyCoordinator().apply(harness.deps)
  assert.equal(result.ok, false)
  assert.match(result.detail ?? '', /백엔드를 종료/)
  assert.deepEqual(harness.applied, ['disabled'])
  assert.equal(harness.failClosedCalls(), 1)
})

test('a non-ok exact NVIDIA apply revokes trust and confirms disabled state', async () => {
  const harness = applyHarness()
  harness.deps.applyConfig = async (runtime) => {
    harness.applied.push(runtime ? 'nvidia' : 'ollama')
    return { ok: false, detail: 'provider detail must not escape' }
  }
  const result = await new NvidiaDiscordApplyCoordinator().apply(harness.deps)
  assert.equal(result.ok, false)
  assert.equal(result.detail, 'Discord NVIDIA 설정을 적용하지 못했습니다.')
  assert.deepEqual(harness.applied, ['nvidia', 'disabled'])
  assert.equal(harness.revocations(), 1)
  assert.equal(harness.failClosedCalls(), 0)
  assert.equal(harness.trusted(), null)
})
