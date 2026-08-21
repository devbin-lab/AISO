import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtemp, rm } from 'fs/promises'
import { tmpdir } from 'os'
import { join } from 'path'
import { closeMyDbStorage, configureMyDbStorageRoot, getMyDbStore } from './mydb.ts'
import { startMyDbAgentBridge, stopMyDbAgentBridge } from './mydb-agent-bridge.ts'

test('My DB Agent bridge exposes metadata and one-item restore only', async () => {
  const root = await mkdtemp(join(tmpdir(), 'aiso-mydb-agent-bridge-'))
  try {
    configureMyDbStorageRoot(root)
    const core = getMyDbStore().createCore('개인 자료')
    getMyDbStore().deleteNode(core.id)
    const bridge = await startMyDbAgentBridge()

    const denied = await fetch(`${bridge.url}/v1/library`)
    assert.equal(denied.status, 401)

    const headers = { 'X-Aiso-Mydb-Agent-Token': bridge.token }
    const trash = await fetch(`${bridge.url}/v1/trash`, { headers })
    const trashBody = await trash.json() as { nodes: Array<{ id: string; title: string }> }
    assert.deepEqual(trashBody.nodes.map((node) => node.id), [core.id])

    const restored = await fetch(`${bridge.url}/v1/restore-node`, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ nodeId: core.id })
    })
    assert.equal(restored.status, 200)
    const body = await restored.json() as { node: { id: string; kind: string; title: string } }
    assert.equal(body.node.id, core.id)
    assert.equal(body.node.kind, 'core')
    assert.equal(body.node.title, '개인 자료')
    assert.equal(getMyDbStore().snapshot().nodes.some((node) => node.id === core.id), true)
  } finally {
    stopMyDbAgentBridge()
    closeMyDbStorage()
    await rm(root, { recursive: true, force: true })
  }
})

test('the agent bridge offers no way to destroy anything, however the request is shaped', async () => {
  // 사용자가 못 박은 경계다 (2026-07-19):
  //   "DB에 뭐가 있는지 조회하는것도 삭제는 못했으면 좋겠고 복구하는 거는 가능 했으면 좋겠고."
  // 사용자 UI에는 휴지통 완전 삭제가 생겼지만, 그 능력이 에이전트 쪽으로
  // 새지 않아야 한다. 라우트가 늘어날 때 이 테스트가 먼저 깨진다.
  const root = await mkdtemp(join(tmpdir(), 'aiso-mydb-agent-nodestroy-'))
  try {
    configureMyDbStorageRoot(root)
    const core = getMyDbStore().createCore('지켜져야 할 코어')
    getMyDbStore().deleteNode(core.id)
    const bridge = await startMyDbAgentBridge()
    const headers = { 'X-Aiso-Mydb-Agent-Token': bridge.token, 'Content-Type': 'application/json' }

    const attempts = [
      ['POST', '/v1/purge-node'],
      ['POST', '/v1/delete-node'],
      ['DELETE', '/v1/trash'],
      ['POST', '/v1/clear-all'],
      ['DELETE', '/v1/library'],
      ['POST', '/v1/restore-graph-checkpoint']
    ] as const

    for (const [method, url] of attempts) {
      const response = await fetch(`${bridge.url}${url}`, {
        method,
        headers,
        body: method === 'POST' ? JSON.stringify({ nodeId: core.id }) : undefined
      })
      assert.equal(
        response.ok, false,
        `에이전트 브리지가 ${method} ${url} 을 받아들였다 — 파괴 능력이 새어 나갔다`
      )
    }

    // 무엇도 사라지지 않았다.
    assert.equal(getMyDbStore().trash().nodes.some((node) => node.id === core.id), true)
  } finally {
    stopMyDbAgentBridge()
    closeMyDbStorage()
    await rm(root, { recursive: true, force: true })
  }
})
