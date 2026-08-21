import assert from 'node:assert/strict'
import test from 'node:test'
import { DEFAULT_SETTINGS } from '../shared/settings.ts'
import type { ComfyModelProfile } from '../shared/comfy-model.ts'
import {
  NvidiaAgentDataApprovalStore,
  NVIDIA_AGENT_TODO_TOOLS,
  buildAutomaticNvidiaAgentDataScope,
  buildNvidiaAgentManifestAuthority,
  fenceNvidiaAgentSettingsMutation
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
    qualityMode: 'base',
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
  const request = { workspace: false, rag: false, image: true }
  const authority = buildNvidiaAgentManifestAuthority(settings(), session, request, [profile])
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
    image: true,
    todos: true,
    myDb: true
  })
  assert.deepEqual(buildAutomaticNvidiaAgentDataScope(
    { ...settings(), workspace: '' }, profiles
  ), { workspace: false, rag: false, image: true, todos: true, myDb: true })
  assert.deepEqual(buildAutomaticNvidiaAgentDataScope(
    { ...settings(), comfyModelSelectionMode: 'manual' }, profiles
  ), { workspace: true, rag: true, image: false, todos: true, myDb: true })
  assert.deepEqual(buildAutomaticNvidiaAgentDataScope(
    { ...settings(), comfyModelSelectionMode: 'manual' }, profiles, 'disabled-ready'
  ), { workspace: true, rag: true, image: true, todos: true, myDb: true, selectedComfyModelId: 'disabled-ready' })
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
  ), { workspace: false, rag: false, image: false, todos: false, myDb: false })

  const calendarOnlyPolicy: ReturnType<typeof settings> = {
    ...base,
    agentToolPolicy: {
      ollama: [],
      nvidia: ['create_calendar_event']
    }
  }
  assert.deepEqual(buildAutomaticNvidiaAgentDataScope(
    calendarOnlyPolicy,
    [readyProfile('enabled-ready', true)]
  ), { workspace: false, rag: false, image: false, todos: true, myDb: false })
})

test('every calendar tool on its own opens the todos scope', () => {
  // 예전에는 list_calendar_events / create_calendar_event 두 개만 손으로 검사해서,
  // manage_calendar_event만 켠 정책이 todos=false를 받고 캘린더 도구가 전부 빠졌다.
  const base = settings()
  for (const toolId of NVIDIA_AGENT_TODO_TOOLS) {
    const policy: ReturnType<typeof settings> = {
      ...base,
      agentToolPolicy: { ollama: [], nvidia: [toolId] }
    }
    assert.deepEqual(
      buildAutomaticNvidiaAgentDataScope(policy, [readyProfile('enabled-ready', true)]),
      { workspace: false, rag: false, image: false, todos: true, myDb: false },
      `${toolId} 하나만 켰을 때 todos 스코프가 열리지 않았다`
    )
  }
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

test('a grant is bound to one session and to the exact scope it was issued for', () => {
  const store = new NvidiaAgentDataApprovalStore()
  const minimal = buildNvidiaAgentManifestAuthority(
    settings(), session, { workspace: false, rag: false, image: false }, []
  )
  // 승인 전에는 아무 범위도 없다.
  assert.throws(() => store.approvedRequest(session), /승인되지/)

  store.authorizePolicy(minimal)
  assert.equal(store.requireExact(session, minimal).workspace, '')

  // 다른 세션은 이 권한을 쓸 수 없다.
  assert.throws(() => store.requireExact('session-other-1234567890', minimal), /다시 승인이 필요/)

  // 범위를 넓히면 같은 세션이라도 다시 승인해야 한다.
  const expanded = buildNvidiaAgentManifestAuthority(
    settings(), session, { workspace: true, rag: false, image: false }, []
  )
  assert.throws(() => store.requireExact(session, expanded), /다시 승인이 필요/)

  // 한 번 소비하면 사라진다 — 실행 경계가 쓰는 계약이다.
  store.consumeExact(session, minimal)
  assert.throws(() => store.requireExact(session, minimal), /다시 승인이 필요/)
})

test('rejection, provider/model change, and process-local reset fail closed', () => {
  const store = new NvidiaAgentDataApprovalStore()
  const authority = buildNvidiaAgentManifestAuthority(
    settings(), session, { workspace: false, rag: false, image: false }, []
  )
  // 승인하지 않으면 아무 범위도 열리지 않는다.
  assert.throws(() => store.approvedRequest(session), /승인되지/)

  store.authorizePolicy(authority)
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

test('RAG can never be sent without the workspace it came from', () => {
  // 예전에는 렌더러가 보낸 scope 를 검사해 이 조합을 거부했다. 이제 scope 는 Main 이
  // 설정에서 파생하므로 검사할 렌더러 입력이 없고, 대신 계산식이 구조적으로 보장한다:
  // rag 가 참이려면 workspace 문자열이 비어 있지 않아야 하고, 그러면 workspace 도 참이 된다.
  // 그 보장이 깨지면 작업 폴더 승인 없이 RAG 발췌가 나가므로 여기서 고정한다.
  const withRag = buildAutomaticNvidiaAgentDataScope(
    { ...settings(), agentToolPolicy: { ...DEFAULT_SETTINGS.agentToolPolicy, nvidia: ['search_docs'] } },
    []
  )
  assert.equal(withRag.rag, true)
  assert.equal(withRag.workspace, true, 'RAG 전송이 작업 폴더 전송 없이 열렸다')

  // 작업 폴더가 없으면 둘 다 닫힌다.
  const noWorkspace = buildAutomaticNvidiaAgentDataScope(
    { ...settings(), workspace: '', agentToolPolicy: { ...DEFAULT_SETTINGS.agentToolPolicy, nvidia: ['search_docs'] } },
    []
  )
  assert.equal(noWorkspace.rag, false)
  assert.equal(noWorkspace.workspace, false)
})

test('approved records remain bounded at 256 entries', () => {
  const request = { workspace: false, rag: false, image: false }
  const store = new NvidiaAgentDataApprovalStore()
  const sessions = Array.from({ length: 257 }, (_, index) => {
    const sessionId = `session-approved-${String(index).padStart(4, '0')}`
    store.authorizePolicy(buildNvidiaAgentManifestAuthority(settings(), sessionId, request, []))
    return sessionId
  })
  // 가장 오래된 것이 밀려나고 최신 것은 남는다 — 메모리 상한이 실제로 걸린다.
  assert.throws(() => store.approvedRequest(sessions[0]), /승인되지 않았습니다/)
  assert.deepEqual(store.approvedRequest(sessions.at(-1)!), request)
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
