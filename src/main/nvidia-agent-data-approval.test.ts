import assert from 'node:assert/strict'
import test from 'node:test'
import { DEFAULT_SETTINGS } from '../shared/settings.ts'
import type { ComfyModelProfile } from '../shared/comfy-model.ts'
import {
  NvidiaAgentDataApprovalStore,
  buildNvidiaAgentManifestAuthority,
  validateManifestDecisionInput,
  validateManifestDescribeInput
} from './nvidia-agent-data-approval.ts'

const session = 'session-gate6-1234567890'
function readyProfile(id: string, agentEnabled: boolean): ComfyModelProfile {
  return {
    id,
    name: id === 'private-profile' ? 'PRIVATE_NAME_CANARY' : id,
    family: 'sdxl',
    capabilities: ['txt2img'],
    tags: [],
    assets: [{
      id: `${id}-checkpoint`,
      kind: 'checkpoint',
      slot: 'checkpoint',
      fileName: 'CHECKPOINT_CANARY.safetensors',
      comfyName: 'CHECKPOINT_CANARY.safetensors',
      relativePath: 'checkpoints/CHECKPOINT_CANARY.safetensors',
      size: 1,
      sha256: 'a'.repeat(64),
      importedAt: 1
    }],
    workflowTemplateId: 'PRIVATE_WORKFLOW_CANARY',
    defaults: { width: 1024, height: 1024, steps: 20, cfg: 7 },
    agentEnabled,
    priority: 0,
    createdAt: 1,
    updatedAt: 1
  }
}

function unreadyProfile(id: string, agentEnabled = true): ComfyModelProfile {
  return { ...readyProfile(id, agentEnabled), assets: [] }
}

const profile = readyProfile('private-profile', true)

function settings() {
  return {
    ...DEFAULT_SETTINGS,
    activeLlmProvider: 'nvidia' as const,
    nvidiaModel: 'model/a',
    workspace: 'C:/PRIVATE/WORKSPACE_CANARY',
    ragEnabled: true
  }
}

test('manifest is Main-derived and hides Comfy registry, paths, and workflow', () => {
  const request = validateManifestDescribeInput({
    sessionId: session,
    scope: { workspace: false, rag: false, image: true }
  })
  const authority = buildNvidiaAgentManifestAuthority(settings(), session, request.scope, [profile])
  const publicText = JSON.stringify(authority.manifest)
  assert.equal(authority.manifest.sends.conversation, true)
  assert.equal(authority.manifest.sends.workspace, false)
  assert.equal(authority.manifest.sends.rag, false)
  assert.ok(authority.manifest.allowedTools.includes('generate_image'))
  assert.doesNotMatch(publicText, /PRIVATE_NAME_CANARY|CHECKPOINT_CANARY|WORKFLOW_CANARY/)
  assert.equal(authority.executionScope.comfy.profiles[0]?.id, 'private-profile')
})

test('auto image scope includes only ready profiles enabled for Agent selection', () => {
  const authority = buildNvidiaAgentManifestAuthority(
    settings(),
    session,
    { workspace: false, rag: false, image: true },
    [readyProfile('enabled-ready', true), readyProfile('disabled-ready', false), unreadyProfile('enabled-unready')]
  )
  assert.deepEqual(authority.executionScope.comfy.profiles.map(({ id }) => id), ['enabled-ready'])
})

test('manual image scope includes the exact ready profile even when auto selection is disabled', () => {
  const authority = buildNvidiaAgentManifestAuthority(
    { ...settings(), comfyModelSelectionMode: 'manual' },
    session,
    { workspace: false, rag: false, image: true, selectedComfyModelId: 'manual-ready' },
    [readyProfile('other-ready', true), readyProfile('manual-ready', false)]
  )
  assert.deepEqual(authority.executionScope.comfy.profiles.map(({ id }) => id), ['manual-ready'])
  assert.equal(authority.executionScope.comfy.selectedProfileId, 'manual-ready')
})

test('image scope rejects unready manual selection and auto mode without an eligible profile', () => {
  assert.throws(() => buildNvidiaAgentManifestAuthority(
    settings(),
    session,
    { workspace: false, rag: false, image: true },
    [readyProfile('disabled-ready', false), unreadyProfile('enabled-unready')]
  ), /준비 완료/)
  assert.throws(() => buildNvidiaAgentManifestAuthority(
    { ...settings(), comfyModelSelectionMode: 'manual' },
    session,
    { workspace: false, rag: false, image: true, selectedComfyModelId: 'manual-unready' },
    [unreadyProfile('manual-unready', false)]
  ), /준비 상태가 아닙니다/)
})

test('approval challenge is one-use and bound to one session and exact scope', () => {
  const store = new NvidiaAgentDataApprovalStore()
  const minimal = buildNvidiaAgentManifestAuthority(
    settings(), session, { workspace: false, rag: false, image: false }, []
  )
  const manifest = store.describe(minimal)
  assert.throws(() => store.approvedRequest(session), /승인되지/)
  assert.deepEqual(store.decide({ sessionId: session, manifestId: manifest.manifestId, approved: true }), {
    approved: true
  })
  assert.equal(store.requireExact(session, minimal).workspace, '')
  assert.throws(
    () => store.decide({ sessionId: session, manifestId: manifest.manifestId, approved: true }),
    /만료되었거나/
  )
  assert.throws(
    () => store.requireExact('session-other-1234567890', minimal),
    /다시 승인이 필요/
  )
  const expanded = buildNvidiaAgentManifestAuthority(
    settings(), session, { workspace: true, rag: false, image: false }, []
  )
  assert.throws(() => store.requireExact(session, expanded), /다시 승인이 필요/)
  assert.throws(() => store.requireExact(session, minimal))
})

test('rejection, provider/model change, and process-local reset fail closed', () => {
  const store = new NvidiaAgentDataApprovalStore()
  const authority = buildNvidiaAgentManifestAuthority(
    settings(), session, { workspace: false, rag: false, image: false }, []
  )
  const rejected = store.describe(authority)
  assert.deepEqual(store.decide({
    sessionId: session, manifestId: rejected.manifestId, approved: false
  }), { approved: false })
  assert.throws(() => store.approvedRequest(session), /승인되지/)

  const approved = store.describe(authority)
  store.decide({ sessionId: session, manifestId: approved.manifestId, approved: true })
  const changed = buildNvidiaAgentManifestAuthority(
    { ...settings(), nvidiaModel: 'model/b' },
    session,
    { workspace: false, rag: false, image: false },
    []
  )
  assert.throws(() => store.requireExact(session, changed), /다시 승인이 필요/)
  store.clearAll()
  assert.throws(() => store.approvedRequest(session), /승인되지/)
})

test('invalid scope and forged decision fields are rejected', () => {
  assert.throws(
    () => validateManifestDescribeInput({ sessionId: session, scope: { workspace: false, rag: true, image: false } }),
    /함께 선택/
  )
  assert.throws(
    () => validateManifestDecisionInput({ sessionId: session, manifestId: 'short', approved: true }),
    /형식/
  )
})

test('pending and approved records remain bounded at 256 entries', () => {
  const request = { workspace: false, rag: false, image: false }

  const pendingStore = new NvidiaAgentDataApprovalStore()
  const pendingManifests = Array.from({ length: 257 }, (_, index) => {
    const sessionId = `session-pending-${String(index).padStart(4, '0')}`
    return {
      sessionId,
      manifest: pendingStore.describe(buildNvidiaAgentManifestAuthority(settings(), sessionId, request, []))
    }
  })
  assert.throws(
    () => pendingStore.decide({
      sessionId: pendingManifests[0].sessionId,
      manifestId: pendingManifests[0].manifest.manifestId,
      approved: true
    }),
    /만료되었거나/
  )
  const newestPending = pendingManifests.at(-1)!
  assert.deepEqual(pendingStore.decide({
    sessionId: newestPending.sessionId,
    manifestId: newestPending.manifest.manifestId,
    approved: true
  }), { approved: true })

  const approvedStore = new NvidiaAgentDataApprovalStore()
  const approvedSessions = Array.from({ length: 257 }, (_, index) => {
    const sessionId = `session-approved-${String(index).padStart(4, '0')}`
    const manifest = approvedStore.describe(
      buildNvidiaAgentManifestAuthority(settings(), sessionId, request, [])
    )
    approvedStore.decide({ sessionId, manifestId: manifest.manifestId, approved: true })
    return sessionId
  })
  assert.throws(() => approvedStore.approvedRequest(approvedSessions[0]), /승인되지 않았습니다/)
  assert.deepEqual(approvedStore.approvedRequest(approvedSessions.at(-1)!), request)
})
