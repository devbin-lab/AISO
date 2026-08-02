import type { AppSettings } from '../shared/settings.ts'
import { canonicalizeNvidiaBinding } from '../shared/nvidia.ts'

export interface TrustedDiscordNvidiaTarget {
  deploymentMode: 'build' | 'nim'
  endpoint: string
  model: string
}

/** True only while an exact, currently authorized NVIDIA consumer still exists. */
export function hasLiveNvidiaDemand(
  settings: AppSettings,
  trustedDiscordRuntime: TrustedDiscordNvidiaTarget | null
): boolean {
  if (settings.activeLlmProvider === 'nvidia') return true
  if (settings.discordLlmProvider !== 'nvidia' || !settings.discordEnabled) return false
  if (!trustedDiscordRuntime) return false
  const binding = canonicalizeNvidiaBinding({
    deploymentMode: settings.nvidiaDeploymentMode,
    endpoint: settings.nvidiaDeploymentMode === 'nim' ? settings.nvidiaNimEndpoint : undefined
  })
  return trustedDiscordRuntime.deploymentMode === binding.deploymentMode &&
    trustedDiscordRuntime.endpoint === binding.endpoint &&
    trustedDiscordRuntime.model === settings.nvidiaModel.trim()
}

/** Clears a sidecar credential only after every authorized NVIDIA consumer is gone. */
export async function clearNvidiaCredentialWhenUnused(
  settings: AppSettings,
  trustedDiscordRuntime: TrustedDiscordNvidiaTarget | null,
  clearCredential: () => Promise<void>
): Promise<boolean> {
  if (hasLiveNvidiaDemand(settings, trustedDiscordRuntime)) return false
  await clearCredential()
  return true
}
