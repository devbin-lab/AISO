import type { AppSettings } from '../shared/settings.ts'
import {
  canonicalizeNvidiaBinding,
  sameNvidiaBinding,
  type NvidiaCapabilitySnapshot,
  type NvidiaCredentialBinding
} from '../shared/nvidia.ts'
import { capabilityBoundGrantTtlSeconds } from './nvidia-grant-ttl.ts'

export interface ExactNvidiaDiscordTarget extends NvidiaCredentialBinding {
  model: string
}

export interface TrustedNvidiaDiscordRuntime extends ExactNvidiaDiscordTarget {
  provider: 'nvidia'
  grantId?: string
}

export interface DiscordApplyResult {
  ok: boolean
  detail?: string
}

export interface NvidiaDiscordFenceDeps {
  loadSettings(): AppSettings
  revisionIsCurrent(revision: number): boolean
  getCapability(target: ExactNvidiaDiscordTarget): NvidiaCapabilitySnapshot | null
}

export interface NvidiaDiscordApplyDeps extends NvidiaDiscordFenceDeps {
  revisionSnapshot(): number
  getTrustedRuntime(): TrustedNvidiaDiscordRuntime | null
  clearTrustedRuntimeIf(expected: TrustedNvidiaDiscordRuntime): void
  prepareExecution(binding: NvidiaCredentialBinding): Promise<void>
  issueGrant(input: ExactNvidiaDiscordTarget & { ttlSeconds: number }): Promise<{ grantId: string }>
  revokeGrants(): Promise<void>
  applyConfig(runtime?: TrustedNvidiaDiscordRuntime): Promise<DiscordApplyResult>
  disableConfig(): Promise<DiscordApplyResult>
  failClosed(): void
  clearCredentialWhenUnused(): Promise<void>
  now(): number
}

export function nvidiaDiscordTargetFromSettings(settings: AppSettings): ExactNvidiaDiscordTarget | null {
  const model = settings.nvidiaModel.trim()
  if (!model) return null
  const binding = canonicalizeNvidiaBinding({
    deploymentMode: settings.nvidiaDeploymentMode,
    endpoint: settings.nvidiaDeploymentMode === 'nim' ? settings.nvidiaNimEndpoint : undefined
  })
  return { ...binding, model }
}

function sameTarget(left: ExactNvidiaDiscordTarget, right: ExactNvidiaDiscordTarget): boolean {
  return left.model === right.model && sameNvidiaBinding(left, right)
}

export function assertNvidiaDiscordConsentCurrent(
  expected: ExactNvidiaDiscordTarget,
  expectedProvider: AppSettings['discordLlmProvider'],
  revision: number,
  deps: NvidiaDiscordFenceDeps
): NvidiaCapabilitySnapshot {
  if (!deps.revisionIsCurrent(revision)) {
    throw new Error('Discord NVIDIA 도구 기능 확인 상태가 변경되었습니다.')
  }
  const settings = deps.loadSettings()
  const current = nvidiaDiscordTargetFromSettings(settings)
  if (!current || !sameTarget(current, expected) || settings.discordLlmProvider !== expectedProvider) {
    throw new Error('Discord NVIDIA 대상 또는 설정이 변경되었습니다.')
  }
  const capability = deps.getCapability(expected)
  if (capability?.capabilities.tools !== 'supported') {
    throw new Error('현재 NVIDIA 모델의 도구 기능 검사가 필요합니다.')
  }
  return capability
}

function assertApplyCurrent(
  expected: TrustedNvidiaDiscordRuntime,
  revision: number,
  deps: NvidiaDiscordApplyDeps
): NvidiaCapabilitySnapshot {
  if (!deps.revisionIsCurrent(revision) || deps.getTrustedRuntime() !== expected) {
    throw new Error('Discord NVIDIA 실행 신뢰가 변경되었습니다.')
  }
  const settings = deps.loadSettings()
  const current = nvidiaDiscordTargetFromSettings(settings)
  if (
    settings.discordLlmProvider !== 'nvidia' || !current ||
    !sameTarget(current, expected)
  ) {
    throw new Error('Discord NVIDIA 대상이 준비 중 변경되었습니다.')
  }
  const capability = deps.getCapability(expected)
  if (capability?.capabilities.tools !== 'supported') {
    throw new Error('Discord NVIDIA 도구 기능 확인이 만료되거나 변경되었습니다.')
  }
  return capability
}

const DISCORD_FAIL_CLOSED_DETAIL = 'Discord 봇을 안전하게 중지하지 못해 백엔드를 종료했습니다.'

async function enforceDisabled(deps: NvidiaDiscordApplyDeps): Promise<DiscordApplyResult> {
  try {
    const result = await deps.disableConfig()
    if (result.ok) return result
  } catch {
    // A timeout or transport failure is indistinguishable from a live old bot.
  }
  deps.failClosed()
  return { ok: false, detail: DISCORD_FAIL_CLOSED_DETAIL }
}

async function applyUntrustedConfig(deps: NvidiaDiscordApplyDeps): Promise<DiscordApplyResult> {
  const disabled = await enforceDisabled(deps)
  await deps.clearCredentialWhenUnused().catch(() => {})
  if (!disabled.ok) return disabled
  if (deps.loadSettings().discordLlmProvider !== 'nvidia') return deps.applyConfig()
  return { ok: false, detail: 'Discord NVIDIA 실행 신뢰를 확인하지 못해 봇을 중지했습니다.' }
}

async function runFencedApply(deps: NvidiaDiscordApplyDeps): Promise<DiscordApplyResult> {
  const settings = deps.loadSettings()
  if (settings.discordLlmProvider !== 'nvidia') return applyUntrustedConfig(deps)
  const target = nvidiaDiscordTargetFromSettings(settings)
  const expected = deps.getTrustedRuntime()
  if (!target || !expected || !sameTarget(target, expected)) return applyUntrustedConfig(deps)

  const revision = deps.revisionSnapshot()
  try {
    assertApplyCurrent(expected, revision, deps)
    await deps.prepareExecution(expected)
    let capability = assertApplyCurrent(expected, revision, deps)
    let runtime: TrustedNvidiaDiscordRuntime = expected
    if (deps.loadSettings().discordEnabled) {
      const ttlSeconds = capabilityBoundGrantTtlSeconds(capability.checkedAt, deps.now())
      const grant = await deps.issueGrant({ ...expected, ttlSeconds })
      capability = assertApplyCurrent(expected, revision, deps)
      capabilityBoundGrantTtlSeconds(capability.checkedAt, deps.now())
      runtime = { ...expected, grantId: grant.grantId }
    }
    const result = await deps.applyConfig(runtime)
    if (!result.ok) throw new Error('Discord NVIDIA 설정을 적용하지 못했습니다.')
    // The POST may have raced an invalidation. A stale success must be followed
    // by the compensating untrusted/disabled configuration in the catch path.
    assertApplyCurrent(expected, revision, deps)
    return result
  } catch (error) {
    deps.clearTrustedRuntimeIf(expected)
    await deps.revokeGrants().catch(() => {})
    const disabled = await enforceDisabled(deps)
    await deps.clearCredentialWhenUnused().catch(() => {})
    if (!disabled.ok) return disabled
    return { ok: false, detail: error instanceof Error ? error.message : String(error) }
  }
}

/** Serializes every Discord apply so an older completion cannot win final sidecar state. */
export class NvidiaDiscordApplyCoordinator {
  private tail: Promise<void> = Promise.resolve()

  apply(deps: NvidiaDiscordApplyDeps): Promise<DiscordApplyResult> {
    const operation = this.tail.then(
      () => runFencedApply(deps),
      () => runFencedApply(deps)
    )
    this.tail = operation.then(() => undefined, () => undefined)
    return operation
  }
}
