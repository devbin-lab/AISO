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
