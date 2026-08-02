import assert from 'node:assert/strict'
import test from 'node:test'
import { NVIDIA_CAPABILITY_MAX_AGE_MS } from './nvidia-capability-cache.ts'
import { capabilityBoundGrantTtlSeconds } from './nvidia-grant-ttl.ts'

test('research and Discord grants cannot outlive capability evidence', () => {
  const now = Date.parse('2026-08-02T00:00:00.000Z')
  assert.equal(capabilityBoundGrantTtlSeconds(new Date(now).toISOString(), now), 60)
  assert.equal(capabilityBoundGrantTtlSeconds(
    new Date(now - NVIDIA_CAPABILITY_MAX_AGE_MS + 2_999).toISOString(), now
  ), 2)
  assert.throws(() => capabilityBoundGrantTtlSeconds(
    new Date(now - NVIDIA_CAPABILITY_MAX_AGE_MS + 999).toISOString(), now
  ), /expired/)
  assert.throws(() => capabilityBoundGrantTtlSeconds('not-a-date', now), /expired/)
})
