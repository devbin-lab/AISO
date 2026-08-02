import assert from 'node:assert/strict'
import test from 'node:test'
import { DEFAULT_SETTINGS, type AppSettings } from '../shared/settings.ts'
import { NVIDIA_BUILD_BASE_URL } from '../shared/nvidia.ts'
import {
  prepareNvidiaExecution,
  type NvidiaExecutionPreparationDeps
} from './nvidia-execution.ts'

function settings(patch: Partial<AppSettings> = {}): AppSettings {
  return {
    ...DEFAULT_SETTINGS,
    activeLlmProvider: 'nvidia',
    nvidiaDeploymentMode: 'build',
    nvidiaModel: 'meta/llama-test',
    chatWebSearch: false,
    ...patch
  }
}

function harness(options: {
  settings?: AppSettings[]
  hasStoredCredential?: boolean
  matchesCurrentBinding?: boolean
  sidecarHasCredential?: boolean
  sidecarEndpoint?: string
  sidecarInitiallyReady?: boolean
  sidecarStatusImmutable?: boolean
} = {}) {
  const settingReads = [...(options.settings ?? [settings()])]
  const calls: Array<{ name: string; args: unknown[] }> = []
  const first = options.settings?.[0] ?? settings()
  let sidecarBinding: Record<string, unknown> | null = options.sidecarInitiallyReady
    ? {
        deploymentMode: first.nvidiaDeploymentMode,
        endpoint: options.sidecarEndpoint ?? (
          first.nvidiaDeploymentMode === 'nim' ? first.nvidiaNimEndpoint : NVIDIA_BUILD_BASE_URL
        )
      }
    : null
  let sidecarHasCredential = options.sidecarInitiallyReady
    ? options.sidecarHasCredential ?? first.nvidiaDeploymentMode === 'build'
    : false
  const deps: NvidiaExecutionPreparationDeps = {
    loadSettings: () => settingReads.length > 1 ? settingReads.shift()! : settingReads[0]!,
    credentialStatus: async (...args) => {
      calls.push({ name: 'credentialStatus', args })
      return {
        encryptionAvailable: true,
        hasStoredCredential: options.hasStoredCredential ?? true,
        matchesCurrentBinding: options.matchesCurrentBinding ?? true
      }
    },
    readCredential: async (...args) => {
      calls.push({ name: 'readCredential', args })
      return 'CANARY-MAIN-ONLY-NVIDIA-KEY'
    },
    setSidecarCredential: async (...args) => {
      calls.push({ name: 'setSidecarCredential', args })
      if (!options.sidecarStatusImmutable) {
        sidecarBinding = { deploymentMode: args[0], endpoint: args[1] }
        sidecarHasCredential = true
      }
    },
    bindSidecarNim: async (...args) => {
      calls.push({ name: 'bindSidecarNim', args })
      if (!options.sidecarStatusImmutable) {
        sidecarBinding = { deploymentMode: 'nim', endpoint: args[0] }
        sidecarHasCredential = false
      }
    },
    clearSidecarCredential: async (...args) => {
      calls.push({ name: 'clearSidecarCredential', args })
      sidecarBinding = null
      sidecarHasCredential = false
    },
    sidecarStatus: async () => {
      calls.push({ name: 'sidecarStatus', args: [] })
      return {
        binding: sidecarBinding,
        hasCredential: sidecarHasCredential
      }
    }
  }
  return { deps, calls }
}

test('Build preparation transfers the key only to the exact sidecar binding', async () => {
  const { deps, calls } = harness()
  const result = await prepareNvidiaExecution({ deploymentMode: 'build' }, deps)
  assert.deepEqual(result, { ready: true, credential: 'stored' })
  assert.deepEqual(calls.map((call) => call.name), [
    'credentialStatus', 'sidecarStatus', 'readCredential', 'setSidecarCredential', 'sidecarStatus'
  ])
  const transfer = calls.find((call) => call.name === 'setSidecarCredential')
  assert.deepEqual(transfer?.args, [
    'build', NVIDIA_BUILD_BASE_URL, 'CANARY-MAIN-ONLY-NVIDIA-KEY'
  ])
  assert.equal(JSON.stringify(result).includes('CANARY'), false)
})

test('an arbitrary Renderer endpoint is rejected before credential access or sidecar I/O', async () => {
  const { deps, calls } = harness()
  await assert.rejects(
    prepareNvidiaExecution({
      deploymentMode: 'nim',
      endpoint: 'https://attacker.example/v1'
    }, deps),
    /현재 NVIDIA 설정과 일치하지 않습니다/
  )
  assert.deepEqual(calls, [])
})

test('a settings binding change during preparation revokes the transferred credential', async () => {
  const before = settings()
  const after = settings({
    nvidiaDeploymentMode: 'nim',
    nvidiaNimEndpoint: 'https://nim.example/v1'
  })
  const { deps, calls } = harness({ settings: [before, after] })
  await assert.rejects(
    prepareNvidiaExecution({ deploymentMode: 'build' }, deps),
    /변경되어 자격 증명을 폐기했습니다/
  )
  assert.equal(calls.some((call) => call.name === 'setSidecarCredential'), true)
  assert.equal(calls.at(-1)?.name, 'clearSidecarCredential')
})

test('keyless NIM binds the exact endpoint without reading or forwarding a key', async () => {
  const nim = settings({
    nvidiaDeploymentMode: 'nim',
    nvidiaNimEndpoint: 'https://nim.example/v1'
  })
  const { deps, calls } = harness({
    settings: [nim],
    hasStoredCredential: false,
    matchesCurrentBinding: false,
    sidecarHasCredential: false
  })
  const result = await prepareNvidiaExecution({
    deploymentMode: 'nim',
    endpoint: 'https://nim.example/v1/'
  }, deps)
  assert.deepEqual(result, { ready: true, credential: 'not_required' })
  assert.equal(calls.some((call) => call.name === 'readCredential'), false)
  assert.equal(calls.some((call) => call.name === 'setSidecarCredential'), false)
  assert.deepEqual(calls.find((call) => call.name === 'bindSidecarNim')?.args, [
    'https://nim.example/v1'
  ])
})

test('Build without an exact stored credential is denied without sidecar I/O', async () => {
  const { deps, calls } = harness({
    hasStoredCredential: false,
    matchesCurrentBinding: false
  })
  await assert.rejects(
    prepareNvidiaExecution({ deploymentMode: 'build' }, deps),
    /API 키가 없습니다/
  )
  assert.deepEqual(calls.map((call) => call.name), ['credentialStatus'])
})

test('a mismatched or secret-bearing sidecar status is revoked', async () => {
  const nim = settings({
    nvidiaDeploymentMode: 'nim',
    nvidiaNimEndpoint: 'https://nim.example/v1'
  })
  const { deps, calls } = harness({
    settings: [nim],
    hasStoredCredential: false,
    matchesCurrentBinding: false,
    sidecarHasCredential: true,
    sidecarInitiallyReady: true,
    sidecarStatusImmutable: true
  })
  await assert.rejects(
    prepareNvidiaExecution({ deploymentMode: 'nim', endpoint: nim.nvidiaNimEndpoint }, deps),
    /바인딩을 확인하지 못했습니다/
  )
  assert.equal(calls.at(-1)?.name, 'clearSidecarCredential')
})

test('an already exact sidecar binding is reused without resetting concurrent grants', async () => {
  const { deps, calls } = harness({ sidecarInitiallyReady: true })
  const [first, second] = await Promise.all([
    prepareNvidiaExecution({ deploymentMode: 'build' }, deps),
    prepareNvidiaExecution({ deploymentMode: 'build' }, deps)
  ])
  assert.equal(first.ready, true)
  assert.equal(second.ready, true)
  assert.equal(calls.some((call) => call.name === 'readCredential'), false)
  assert.equal(calls.some((call) => call.name === 'setSidecarCredential'), false)
})

test('concurrent preparation from an empty sidecar transfers the key exactly once', async () => {
  const { deps, calls } = harness()
  const results = await Promise.all([
    prepareNvidiaExecution({ deploymentMode: 'build' }, deps),
    prepareNvidiaExecution({ deploymentMode: 'build' }, deps)
  ])
  assert.equal(results.every((result) => result.ready), true)
  assert.equal(calls.filter((call) => call.name === 'setSidecarCredential').length, 1)
  assert.equal(calls.filter((call) => call.name === 'readCredential').length, 1)
})
