import type { AppSettings } from '../shared/settings.ts'
import { NVIDIA_CAPABILITY_MAX_AGE_MS } from './nvidia-capability-cache.ts'
import {
  canonicalizeNvidiaBinding,
  sameNvidiaBinding,
  type NvidiaAgentPrepareInput,
  type NvidiaAgentPrepareResult,
  type NvidiaCapabilitySnapshot,
  type NvidiaCapabilityTargetInput,
  type NvidiaCredentialBinding
} from '../shared/nvidia.ts'

export interface NvidiaAgentAuthorizationDeps {
  loadSettings(): AppSettings
  getCapability(target: NvidiaCapabilityTargetInput): NvidiaCapabilitySnapshot | null
  revisionSnapshot(): number
  revisionIsCurrent(snapshot: number): boolean
  prepareExecution(binding: NvidiaCredentialBinding): Promise<void>
  issueGrant(
    input: NvidiaAgentPrepareInput & {
      deploymentMode: 'build' | 'nim'
      endpoint: string
      model: string
      ttlSeconds: number
    }
  ): Promise<NvidiaAgentPrepareResult>
  revokeGrants(): Promise<void>
  now(): number
}

type ExactNvidiaAgentTarget = NvidiaCredentialBinding & { model: string }

/** Fence new grants, revoke all outstanding bearers, then publish changed trust state. */
export async function commitNvidiaCapabilityMutation<T>(
  revision: { beginMutation(): void; endMutation(): void },
  revokeGrants: () => Promise<void>,
  mutateCache: () => T | Promise<T>,
  failClosed: () => void | Promise<void>
): Promise<T> {
  revision.beginMutation()
  let safeToEnd = false
  try {
    await revokeGrants()
    const result = await mutateCache()
    safeToEnd = true
    return result
  } catch (error) {
    try {
      await failClosed()
      safeToEnd = true
    } catch {
      // Deliberately leave the mutation fence active for this process if trust
      // cannot be cleared. A fresh grant must not be minted from old metadata.
      throw new Error('NVIDIA capability trust could not be cleared safely')
    }
    throw error
  } finally {
    if (safeToEnd) revision.endMutation()
  }
}

export function validateNvidiaAgentPrepareInput(input: unknown): NvidiaAgentPrepareInput {
  if (!input || typeof input !== 'object') throw new Error('NVIDIA Agent 실행 식별자가 필요합니다.')
  const value = input as Record<string, unknown>
  const valid = (candidate: unknown): candidate is string =>
    typeof candidate === 'string' && candidate.length >= 16 && candidate.length <= 256 &&
    /^[A-Za-z0-9._:-]+$/.test(candidate)
  if (!valid(value.sessionId) || !valid(value.assistantTurnId)) {
    throw new Error('NVIDIA Agent 실행 식별자 형식이 올바르지 않습니다.')
  }
  return { sessionId: value.sessionId, assistantTurnId: value.assistantTurnId }
}

function savedTarget(settings: AppSettings): ExactNvidiaAgentTarget | null {
  if (settings.activeLlmProvider !== 'nvidia' || !settings.nvidiaModel.trim()) return null
  const binding = canonicalizeNvidiaBinding({
    deploymentMode: settings.nvidiaDeploymentMode,
    endpoint: settings.nvidiaDeploymentMode === 'nim' ? settings.nvidiaNimEndpoint : undefined
  })
  return { ...binding, model: settings.nvidiaModel.trim() }
}

function assertUnchanged(
  expected: ExactNvidiaAgentTarget,
  revision: number,
  deps: NvidiaAgentAuthorizationDeps
): void {
  if (!deps.revisionIsCurrent(revision)) throw new Error('NVIDIA Agent 기능 확인 상태가 변경되었습니다.')
  const current = savedTarget(deps.loadSettings())
  if (
    !current || current.model !== expected.model ||
    !sameNvidiaBinding(current, expected)
  ) {
    throw new Error('NVIDIA Agent 대상 또는 모델이 변경되었습니다.')
  }
}

function requireSupported(
  target: ExactNvidiaAgentTarget,
  deps: NvidiaAgentAuthorizationDeps
): NvidiaCapabilitySnapshot {
  const cached = deps.getCapability(target)
  if (!cached || cached.capabilities.tools !== 'supported') {
    throw new Error('현재 NVIDIA 모델의 도구 기능이 유효하게 확인되지 않아 Agent를 시작할 수 없습니다.')
  }
  return cached
}

export async function prepareNvidiaAgentAuthorization(
  rawInput: unknown,
  deps: NvidiaAgentAuthorizationDeps
): Promise<NvidiaAgentPrepareResult> {
  const input = validateNvidiaAgentPrepareInput(rawInput)
  const target = savedTarget(deps.loadSettings())
  if (!target) throw new Error('현재 저장된 NVIDIA Agent 대상 또는 모델이 없습니다.')
  const revision = deps.revisionSnapshot()
  if (!deps.revisionIsCurrent(revision)) {
    throw new Error('NVIDIA Agent 기능 확인 상태가 변경 중입니다.')
  }
  requireSupported(target, deps)
  await deps.prepareExecution(target)
  assertUnchanged(target, revision, deps)
  const capability = requireSupported(target, deps)
  const remainingMs = Date.parse(capability.checkedAt) + NVIDIA_CAPABILITY_MAX_AGE_MS - deps.now()
  if (!Number.isFinite(remainingMs) || remainingMs <= 0) {
    throw new Error('NVIDIA Agent 기능 확인 유효 시간이 만료되었습니다.')
  }
  const grant = await deps.issueGrant({
    ...input,
    deploymentMode: target.deploymentMode,
    endpoint: target.endpoint,
    model: target.model,
    ttlSeconds: Math.min(60, remainingMs / 1000)
  })
  try {
    assertUnchanged(target, revision, deps)
    requireSupported(target, deps)
    return grant
  } catch (error) {
    await deps.revokeGrants().catch(() => {})
    throw error
  }
}
