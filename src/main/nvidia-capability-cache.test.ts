import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { NVIDIA_BUILD_BASE_URL } from '../shared/nvidia.ts'
import {
  NVIDIA_CAPABILITY_MAX_AGE_MS,
  NvidiaCapabilityCache,
  NvidiaCapabilityRevision
} from './nvidia-capability-cache.ts'

function tempFile(t: test.TestContext): string {
  const directory = mkdtempSync(join(tmpdir(), 'aiso-nvidia-capability-'))
  t.after(() => rmSync(directory, { recursive: true, force: true }))
  return join(directory, 'nvidia-capabilities.json')
}

const supported = { chat: 'supported', stream: 'supported', tools: 'supported' } as const
const buildTarget = { deploymentMode: 'build', model: 'model/a' } as const

test('revision fence rejects a late result after credential or cache invalidation', () => {
  const revision = new NvidiaCapabilityRevision()
  const inFlight = revision.snapshot()
  assert.equal(revision.isCurrent(inFlight), true)
  revision.invalidate()
  assert.equal(revision.isCurrent(inFlight), false)
  assert.equal(revision.isCurrent(revision.snapshot()), true)
})

test('capability metadata survives restart only for the exact binding and model', (t) => {
  const file = tempFile(t)
  const now = Date.parse('2026-08-02T05:00:00.000Z')
  new NvidiaCapabilityCache(file, undefined, () => now).put(buildTarget, supported)

  const restarted = new NvidiaCapabilityCache(file, undefined, () => now + 1_000)
  assert.deepEqual(restarted.get(buildTarget), {
    schemaVersion: 1,
    binding: { deploymentMode: 'build', endpoint: NVIDIA_BUILD_BASE_URL },
    model: 'model/a',
    capabilities: supported,
    checkedAt: '2026-08-02T05:00:00.000Z'
  })
  assert.equal(restarted.get({ ...buildTarget, model: 'model/b' }), null)
  assert.equal(restarted.get({
    deploymentMode: 'nim', endpoint: 'https://nim.example/v1', model: 'model/a'
  }), null)
})

test('stale and future-dated entries are never trusted', (t) => {
  const file = tempFile(t)
  const now = Date.parse('2026-08-02T05:00:00.000Z')
  const cache = new NvidiaCapabilityCache(file, undefined, () => now)
  cache.put(buildTarget, supported)
  assert.equal(
    new NvidiaCapabilityCache(file, undefined, () => now + NVIDIA_CAPABILITY_MAX_AGE_MS + 1)
      .get(buildTarget),
    null
  )
  assert.equal(
    new NvidiaCapabilityCache(file, undefined, () => now - 1).get(buildTarget),
    null
  )
})

test('unknown or malformed schema is rejected instead of reused', (t) => {
  const file = tempFile(t)
  writeFileSync(file, JSON.stringify({
    schemaVersion: 99,
    entries: [{ model: 'model/a', capabilities: supported }],
    rawPrompt: 'CANARY-PROMPT-MUST-NOT-BE-READ'
  }))
  const cache = new NvidiaCapabilityCache(file)
  assert.equal(cache.get(buildTarget), null)
  cache.clearTarget(buildTarget)
  assert.deepEqual(JSON.parse(readFileSync(file, 'utf8')), { schemaVersion: 1, entries: [] })

  writeFileSync(file, JSON.stringify({ schemaVersion: 1, entries: [{ schemaVersion: 0 }] }))
  assert.equal(new NvidiaCapabilityCache(file).get(buildTarget), null)
})

test('cache writes only canonical target, states, and checked metadata', (t) => {
  const file = tempFile(t)
  new NvidiaCapabilityCache(file).put({
    deploymentMode: 'nim', endpoint: 'https://NIM.EXAMPLE:443/v1/', model: ' local/model '
  }, { chat: 'supported', stream: 'supported', tools: 'unknown' })
  const persisted = readFileSync(file, 'utf8')
  assert.equal(persisted.includes('https://nim.example/v1'), true)
  assert.equal(persisted.includes('local/model'), true)
  assert.equal(persisted.includes('prompt'), false)
  assert.equal(persisted.includes('secret'), false)
  assert.equal(persisted.includes('Authorization'), false)
})

test('model disappearance, target clear, binding clear, and full clear invalidate entries', (t) => {
  const file = tempFile(t)
  const cache = new NvidiaCapabilityCache(file)
  cache.put(buildTarget, supported)
  cache.put({ ...buildTarget, model: 'model/b' }, { ...supported, tools: 'unsupported' })
  cache.put({
    deploymentMode: 'nim', endpoint: 'https://nim.example/v1', model: 'model/a'
  }, { ...supported, tools: 'unknown' })

  cache.removeModelsNotInList({ deploymentMode: 'build' }, ['model/b'])
  assert.equal(cache.get(buildTarget), null)
  assert.equal(cache.get({ ...buildTarget, model: 'model/b' })?.capabilities.tools, 'unsupported')
  cache.clearTarget({ ...buildTarget, model: 'model/b' })
  assert.equal(cache.get({ ...buildTarget, model: 'model/b' }), null)
  assert.notEqual(cache.get({
    deploymentMode: 'nim', endpoint: 'https://nim.example/v1', model: 'model/a'
  }), null)
  cache.clearBinding({ deploymentMode: 'nim', endpoint: 'https://nim.example/v1' })
  assert.equal(cache.get({
    deploymentMode: 'nim', endpoint: 'https://nim.example/v1', model: 'model/a'
  }), null)
  cache.put(buildTarget, supported)
  cache.clearAll()
  assert.equal(cache.get(buildTarget), null)
})
