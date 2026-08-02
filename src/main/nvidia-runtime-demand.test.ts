import assert from 'node:assert/strict'
import test from 'node:test'
import { DEFAULT_SETTINGS } from '../shared/settings.ts'
import { NVIDIA_BUILD_BASE_URL } from '../shared/nvidia.ts'
import {
  clearNvidiaCredentialWhenUnused,
  hasLiveNvidiaDemand
} from './nvidia-runtime-demand.ts'

const exactDiscord = {
  deploymentMode: 'build' as const,
  endpoint: NVIDIA_BUILD_BASE_URL,
  model: 'model/a'
}

test('desktop and Discord NVIDIA credential demand are independent', () => {
  assert.equal(hasLiveNvidiaDemand({
    ...DEFAULT_SETTINGS,
    activeLlmProvider: 'nvidia',
    discordEnabled: false,
    discordLlmProvider: 'ollama'
  }, null), true)

  const discordSettings = {
    ...DEFAULT_SETTINGS,
    activeLlmProvider: 'ollama' as const,
    discordEnabled: true,
    discordLlmProvider: 'nvidia' as const,
    nvidiaDeploymentMode: 'build' as const,
    nvidiaModel: 'model/a'
  }
  assert.equal(hasLiveNvidiaDemand(discordSettings, exactDiscord), true)
  assert.equal(hasLiveNvidiaDemand({ ...discordSettings, discordEnabled: false }, exactDiscord), false)
  assert.equal(hasLiveNvidiaDemand({ ...discordSettings, discordLlmProvider: 'ollama' }, exactDiscord), false)
  assert.equal(hasLiveNvidiaDemand(discordSettings, null), false)
  assert.equal(hasLiveNvidiaDemand(discordSettings, { ...exactDiscord, model: 'model/b' }), false)
  assert.equal(hasLiveNvidiaDemand({
    ...discordSettings,
    nvidiaDeploymentMode: 'nim',
    nvidiaNimEndpoint: 'https://nim.example.com/v1'
  }, exactDiscord), false)
})

test('a model-only target change clears the credential after Discord trust is revoked', async () => {
  let clears = 0
  const afterModelChange = {
    ...DEFAULT_SETTINGS,
    activeLlmProvider: 'ollama' as const,
    discordEnabled: true,
    discordLlmProvider: 'nvidia' as const,
    nvidiaDeploymentMode: 'build' as const,
    nvidiaModel: 'model/b'
  }
  assert.equal(await clearNvidiaCredentialWhenUnused(afterModelChange, null, async () => {
    clears++
  }), true)
  assert.equal(clears, 1)

  assert.equal(await clearNvidiaCredentialWhenUnused({
    ...afterModelChange,
    activeLlmProvider: 'nvidia'
  }, null, async () => {
    clears++
  }), false)
  assert.equal(clears, 1)
})

test('failed Discord NVIDIA activation clears credentials after rollback or disabled config', async () => {
  let clears = 0
  const rolledBack = {
    ...DEFAULT_SETTINGS,
    activeLlmProvider: 'ollama' as const,
    discordEnabled: true,
    discordLlmProvider: 'ollama' as const,
    nvidiaDeploymentMode: 'build' as const,
    nvidiaModel: 'model/a'
  }
  assert.equal(await clearNvidiaCredentialWhenUnused(rolledBack, null, async () => {
    clears++
  }), true)

  const disabledNvidia = {
    ...rolledBack,
    discordLlmProvider: 'nvidia' as const
  }
  assert.equal(await clearNvidiaCredentialWhenUnused(disabledNvidia, null, async () => {
    clears++
  }), true)
  assert.equal(clears, 2)
})
