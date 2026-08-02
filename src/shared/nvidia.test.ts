import assert from 'node:assert/strict'
import test from 'node:test'
import {
  NVIDIA_BUILD_BASE_URL,
  canonicalizeNvidiaBinding,
  canonicalizeNvidiaNimEndpoint
} from './nvidia.ts'

test('NVIDIA Build URL is fixed', () => {
  assert.deepEqual(canonicalizeNvidiaBinding({ deploymentMode: 'build' }), {
    deploymentMode: 'build', endpoint: NVIDIA_BUILD_BASE_URL
  })
  assert.throws(() => canonicalizeNvidiaBinding({ deploymentMode: 'build', endpoint: 'https://evil.example/v1' }))
})

test('NIM canonicalizes safe loopback and remote HTTPS endpoints', () => {
  assert.equal(canonicalizeNvidiaNimEndpoint(' HTTP://LOCALHOST:80/v1/// '), 'http://localhost/v1')
  assert.equal(canonicalizeNvidiaNimEndpoint('http://127.0.0.1:8000/v1/'), 'http://127.0.0.1:8000/v1')
  assert.equal(canonicalizeNvidiaNimEndpoint('http://[::1]:8000/v1/'), 'http://[::1]:8000/v1')
  assert.equal(canonicalizeNvidiaNimEndpoint('https://NIM.Example.COM:443/v1/'), 'https://nim.example.com/v1')
})

for (const unsafe of [
  'http://nim.example.com/v1',
  'https://user:pass@nim.example.com/v1',
  'https://nim.example.com/v1?q=1',
  'https://nim.example.com/v1#fragment',
  'https://nim.example.com:99999/v1',
  'file:///tmp/nim'
]) {
  test(`NIM rejects unsafe endpoint: ${unsafe}`, () => {
    assert.throws(() => canonicalizeNvidiaNimEndpoint(unsafe))
  })
}
