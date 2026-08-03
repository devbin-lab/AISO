import assert from 'node:assert/strict'
import test from 'node:test'
import { DEFAULT_SETTINGS } from '../shared/settings.ts'
import type { ComfyModelProfile } from '../shared/comfy-model.ts'
import {
  NvidiaAgentDataApprovalStore,
  buildAutomaticNvidiaAgentDataScope,
  buildNvidiaAgentManifestAuthority,
  fenceNvidiaAgentSettingsMutation,
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

test('automatic scope comes only from saved workspace RAG and private Comfy readiness', () => {
  const profiles = [
    readyProfile('enabled-ready', true),
    readyProfile('disabled-ready', false),
    unreadyProfile('enabled-unready')
  ]
  assert.deepEqual(buildAutomaticNvidiaAgentDataScope(settings(), profiles), {
    workspace: true,
    rag: true,
    image: true
  })
  assert.deepEqual(buildAutomaticNvidiaAgentDataScope(
    { ...settings(), workspace: '' }, profiles
  ), { workspace: false, rag: false, image: true })
  assert.deepEqual(buildAutomaticNvidiaAgentDataScope(
    { ...settings(), comfyModelSelectionMode: 'manual' }, profiles
  ), { workspace: true, rag: true, image: false })
  assert.deepEqual(buildAutomaticNvidiaAgentDataScope(
    { ...settings(), comfyModelSelectionMode: 'manual' }, profiles, 'disabled-ready'
  ), { workspace: true, rag: true, image: true, selectedComfyModelId: 'disabled-ready' })
  assert.throws(
    () => buildAutomaticNvidiaAgentDataScope(settings(), profiles, 'enabled-ready'),
    /자동 모델 선택/
  )
})

test('automatic scope is derived only from the saved NVIDIA tool policy', () => {
  const base = settings()
  const localOnlyPolicy: ReturnType<typeof settings> = {
    ...base,
    agentToolPolicy: {
      ollama: ['list_dir', 'search_docs', 'generate_image'],
      nvidia: []
    }
  }

  assert.deepEqual(buildAutomaticNvidiaAgentDataScope(
    localOnlyPolicy,
    [readyProfile('enabled-ready', true)]
  ), { workspace: false, rag: false, image: false })
})

test('allowed tools and fingerprint bind the exact saved NVIDIA policy', () => {
  const base = settings()
  const scopedSettings: ReturnType<typeof settings> = {
    ...base,
    agentToolPolicy: {
      ollama: ['delete_dir'],
      nvidia: [
        'update_plan',
        'list_dir',
        'write_code_file',
        'run_command',
        'search_docs',
        'generate_image'
      ]
    }
  }
  const request = { workspace: true, rag: true, image: true }
  const authority = buildNvidiaAgentManifestAuthority(
    scopedSettings,
    session,
    request,
    [readyProfile('enabled-ready', true)]
  )

  assert.deepEqual(authority.executionScope.allowedTools, [
    'update_plan',
    'list_dir',
    'write_code_file',
    'run_command',
    'search_docs',
    'generate_image'
  ])
  assert.deepEqual(authority.manifest.allowedTools, authority.executionScope.allowedTools)
  assert.equal(authority.executionScope.allowedTools.includes('delete_dir'), false)

  const changedPolicy = buildNvidiaAgentManifestAuthority(
    {
      ...scopedSettings,
      agentToolPolicy: {
        ...scopedSettings.agentToolPolicy,
        nvidia: scopedSettings.agentToolPolicy.nvidia.filter((tool) => tool !== 'run_command')
      }
    },
    session,
    request,
    [readyProfile('enabled-ready', true)]
  )
  assert.notEqual(authority.executionScope.fingerprint, changedPolicy.executionScope.fingerprint)

  const store = new NvidiaAgentDataApprovalStore()
  store.authorizePolicy(authority)
  assert.throws(() => store.requireExact(session, changedPolicy), /다시 승인이 필요/)
})

test('policy authorization binds the permission mode into the exact fingerprint', () => {
  const store = new NvidiaAgentDataApprovalStore()
  const read = buildNvidiaAgentManifestAuthority(
    settings(), session, { workspace: true, rag: true, image: false }, [], 'read'
  )
  const automatic = buildNvidiaAgentManifestAuthority(
    settings(), session, { workspace: true, rag: true, image: false }, [], 'auto'
  )
  assert.notEqual(read.executionScope.fingerprint, automatic.executionScope.fingerprint)
  store.authorizePolicy(read)
  assert.equal(store.requireExact(session, read).approvalMode, 'read')
  assert.throws(() => store.requireExact(session, automatic), /다시 승인이 필요/)
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

test('settings mutation revokes Agent grants before unrelated follow-up work', async () => {
  const events: string[] = []
  let releaseRevoke!: () => void
  const revokeFinished = new Promise<void>((resolve) => {
    releaseRevoke = resolve
  })
  const operation = fenceNvidiaAgentSettingsMutation(
    true,
    async () => { events.push('follow-up') },
    {
      clearApprovals: () => { events.push('clear-main') },
      revokeAgentGrants: async () => {
        events.push('revoke-sidecar')
        await revokeFinished
      }
    }
  )

  assert.deepEqual(events, ['clear-main', 'revoke-sidecar'])
  releaseRevoke()
  await operation
  assert.deepEqual(events, ['clear-main', 'revoke-sidecar', 'follow-up'])
})
