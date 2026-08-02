import assert from 'node:assert/strict'
import test from 'node:test'
import { DEFAULT_SETTINGS, type AppSettings } from '../shared/settings.ts'
import { NVIDIA_BUILD_BASE_URL, type NvidiaCapabilitySnapshot } from '../shared/nvidia.ts'
import { NvidiaCapabilityRevision } from './nvidia-capability-cache.ts'
import {
  commitNvidiaCapabilityMutation,
  prepareNvidiaAgentAuthorization,
  type NvidiaAgentAuthorizationDeps
} from './nvidia-agent-authorization.ts'
import type { NvidiaAgentExecutionScope } from './nvidia-agent-data-approval.ts'

const input = {
  sessionId: 'session-1234567890',
  assistantTurnId: 'assistant-turn-1234567890',
  approvalMode: 'read' as const
}
const supported: NvidiaCapabilitySnapshot = {
  schemaVersion: 1,
  binding: { deploymentMode: 'build', endpoint: NVIDIA_BUILD_BASE_URL },
  model: 'model/a',
  capabilities: { chat: 'supported', stream: 'supported', tools: 'supported' },
  checkedAt: new Date().toISOString()
}

function settings(patch: Partial<AppSettings> = {}): AppSettings {
  return {
    ...DEFAULT_SETTINGS,
    activeLlmProvider: 'nvidia',
    nvidiaDeploymentMode: 'build',
    nvidiaModel: 'model/a',
    ...patch
  }
}

function harness(options: {
  states?: AppSettings[]
  capability?: NvidiaCapabilitySnapshot | null
  invalidateDuring?: 'prepare' | 'grant'
  now?: number
} = {}) {
  let revision = 7
  let reads = 0
  const states = options.states ?? [settings()]
  const calls: string[] = []
  const executionScope: NvidiaAgentExecutionScope = {
    fingerprint: 'f'.repeat(64),
    approvalMode: 'read',
    workspace: '',
    ragEnabled: false,
    ollamaHost: '',
    ragTopK: 0,
    allowedTools: ['update_plan', 'get_system_time'],
    comfy: {
      enabled: false,
      baseUrl: '',
      profiles: [],
      selectionMode: 'auto',
      selectedProfileId: null
    }
  }
  const deps: NvidiaAgentAuthorizationDeps = {
    loadSettings: () => states[Math.min(reads++, states.length - 1)],
    getCapability: () => options.capability === undefined ? supported : options.capability,
    revisionSnapshot: () => revision,
    revisionIsCurrent: (value) => value === revision,
    prepareExecution: async () => {
      calls.push('prepare')
      if (options.invalidateDuring === 'prepare') revision++
    },
    issueGrant: async (scope) => {
      calls.push(`grant:${scope.endpoint}:${scope.model}:${scope.ttlSeconds}`)
      if (options.invalidateDuring === 'grant') revision++
      return {
        grantId: 'g'.repeat(43),
        assistantTurnId: scope.assistantTurnId,
        expiresInSeconds: scope.ttlSeconds
      }
    },
    revokeGrants: async () => { calls.push('revoke') },
    dataApprovalSnapshot: () => revision,
    dataApprovalIsCurrent: (value) => value === revision,
    getApprovedScope: () => structuredClone(executionScope),
    consumeApprovedScope: () => structuredClone(executionScope),
    now: () => options.now ?? Date.parse(supported.checkedAt)
  }
  return { deps, calls }
}

test('exact current target with fresh tools=supported receives a scoped grant', async () => {
  const { deps, calls } = harness()
  const result = await prepareNvidiaAgentAuthorization(input, deps)
  assert.equal(result.assistantTurnId, input.assistantTurnId)
  assert.deepEqual(calls, ['prepare', `grant:${NVIDIA_BUILD_BASE_URL}:model/a:60`])
})

test('Renderer cannot change the permission mode after Main binds the exact scope', async () => {
  const { deps, calls } = harness()
  await assert.rejects(
    prepareNvidiaAgentAuthorization({ ...input, approvalMode: 'auto' }, deps),
    /권한 모드/
  )
  assert.deepEqual(calls, [])
})

test('stale unknown unsupported or mismatched cache blocks before preparation and grant', async () => {
  for (const capability of [
    null,
    { ...supported, capabilities: { ...supported.capabilities, tools: 'unknown' as const } },
    { ...supported, capabilities: { ...supported.capabilities, tools: 'unsupported' as const } }
  ]) {
    const { deps, calls } = harness({ capability })
    await assert.rejects(prepareNvidiaAgentAuthorization(input, deps), /유효하게 확인/)
    assert.deepEqual(calls, [])
  }
})

test('settings or key revision race during credential preparation blocks before grant', async () => {
  const revisionRace = harness({ invalidateDuring: 'prepare' })
  await assert.rejects(prepareNvidiaAgentAuthorization(input, revisionRace.deps), /변경/)
  assert.deepEqual(revisionRace.calls, ['prepare'])

  const settingsRace = harness({ states: [settings(), settings({ nvidiaModel: 'model/b' })] })
  await assert.rejects(prepareNvidiaAgentAuthorization(input, settingsRace.deps), /변경/)
  assert.deepEqual(settingsRace.calls, ['prepare'])
})

test('race after grant issuance revokes the grant and returns no authorization', async () => {
  const { deps, calls } = harness({ invalidateDuring: 'grant' })
  await assert.rejects(prepareNvidiaAgentAuthorization(input, deps), /변경/)
  assert.deepEqual(calls, ['prepare', `grant:${NVIDIA_BUILD_BASE_URL}:model/a:60`, 'revoke'])
})

test('grant TTL cannot outlive the capability cache entry', async () => {
  const checkedAt = Date.parse(supported.checkedAt)
  const nearExpiry = {
    ...supported,
    checkedAt: new Date(checkedAt - (24 * 60 * 60 * 1000) + 250).toISOString()
  }
  const { deps, calls } = harness({ capability: nearExpiry, now: checkedAt })
  const result = await prepareNvidiaAgentAuthorization(input, deps)
  assert.ok(result.expiresInSeconds <= 0.25)
  assert.deepEqual(calls, ['prepare', `grant:${NVIDIA_BUILD_BASE_URL}:model/a:0.25`])
})

test('reprobe or model refresh fences grants and waits for revocation before cache mutation', async () => {
  const calls: string[] = []
  const revision = new NvidiaCapabilityRevision()
  let releaseRevoke!: () => void
  let releasePublish!: () => void
  const revoking = new Promise<void>((resolve) => { releaseRevoke = resolve })
  const publishing = new Promise<void>((resolve) => { releasePublish = resolve })
  const committing = commitNvidiaCapabilityMutation(
    revision,
    async () => { calls.push('revoke:start'); await revoking; calls.push('revoke:done') },
    async () => { calls.push('cache:mutate'); await publishing; return 'committed' },
    () => { calls.push('cache:fail-closed') }
  )
  await Promise.resolve()
  const duringRevoke = revision.snapshot()
  assert.equal(revision.isCurrent(duringRevoke), false)
  const blockedDuringRevoke = harness()
  blockedDuringRevoke.deps.revisionSnapshot = () => duringRevoke
  blockedDuringRevoke.deps.revisionIsCurrent = (value) => revision.isCurrent(value)
  await assert.rejects(prepareNvidiaAgentAuthorization(input, blockedDuringRevoke.deps), /변경 중/)
  assert.deepEqual(blockedDuringRevoke.calls, [])
  assert.deepEqual(calls, ['revoke:start'])
  releaseRevoke()
  await new Promise((resolve) => setImmediate(resolve))
  assert.deepEqual(calls, ['revoke:start', 'revoke:done', 'cache:mutate'])
  const duringPublish = revision.snapshot()
  const blockedDuringPublish = harness()
  blockedDuringPublish.deps.revisionSnapshot = () => duringPublish
  blockedDuringPublish.deps.revisionIsCurrent = (value) => revision.isCurrent(value)
  await assert.rejects(prepareNvidiaAgentAuthorization(input, blockedDuringPublish.deps), /변경 중/)
  assert.deepEqual(blockedDuringPublish.calls, [])
  releasePublish()
  assert.equal(await committing, 'committed')
  const current = revision.snapshot()
  assert.equal(revision.isCurrent(current), true)
})

test('failed capability publication clears old trust before reopening authorization', async () => {
  const revision = new NvidiaCapabilityRevision()
  const calls: string[] = []
  await assert.rejects(commitNvidiaCapabilityMutation(
    revision,
    async () => { calls.push('revoke') },
    () => { calls.push('publish'); throw new Error('disk failure') },
    () => { calls.push('clear-old-trust') }
  ), /disk failure/)
  assert.deepEqual(calls, ['revoke', 'publish', 'clear-old-trust'])
  const snapshot = revision.snapshot()
  assert.equal(revision.isCurrent(snapshot), true)
})
