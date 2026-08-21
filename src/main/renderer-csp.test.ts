import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { buildRendererCsp } from './renderer-csp.ts'

function directives(csp: string): Map<string, string[]> {
  return new Map(
    csp.split(';').map((part) => {
      const [name, ...values] = part.trim().split(/\s+/)
      return [name, values]
    })
  )
}

test('production policy denies everything by default and allows no inline script', () => {
  const map = directives(buildRendererCsp({ sidecarPort: 51234, isDev: false }))
  assert.deepEqual(map.get('default-src'), ["'none'"])
  assert.deepEqual(map.get('script-src'), ["'self'"])
  assert.equal(map.get('script-src')?.includes("'unsafe-inline'"), false)
  assert.deepEqual(map.get('object-src'), ["'none'"])
  assert.deepEqual(map.get('base-uri'), ["'none'"])
  assert.deepEqual(map.get('form-action'), ["'none'"])
})

test('production connect-src names the exact sidecar port, not a wildcard', () => {
  // 헤더 방식을 택한 핵심 이유. 정적 meta였다면 127.0.0.1:* 를 써야 해서
  // 로컬의 아무 포트나 여는 셈이 된다.
  const map = directives(buildRendererCsp({ sidecarPort: 51234, isDev: false }))
  assert.deepEqual(map.get('connect-src'), ['http://127.0.0.1:51234'])
  assert.equal(map.get('connect-src')?.some((v) => v.includes('*')), false)
})

test('the preview iframe origin is allowed or the preview pane goes blank', () => {
  const map = directives(buildRendererCsp({ sidecarPort: 51234, isDev: false }))
  assert.deepEqual(map.get('frame-src'), ['http://127.0.0.1:51234'])
})

test('screenshots and generated images keep working', () => {
  // 스크린샷은 data:, ComfyUI 결과는 createObjectURL(blob:).
  const map = directives(buildRendererCsp({ sidecarPort: 51234, isDev: false }))
  assert.equal(map.get('img-src')?.includes('data:'), true)
  assert.equal(map.get('img-src')?.includes('blob:'), true)
})

test('before the sidecar is ready no local origin is opened', () => {
  const map = directives(buildRendererCsp({ sidecarPort: null, isDev: false }))
  assert.equal(map.has('connect-src'), false, '포트를 모르는데 연결을 열었다')
  assert.equal(map.has('frame-src'), false)
  assert.deepEqual(map.get('default-src'), ["'none'"])
})

test('development adds only what Vite needs, and only in development', () => {
  const dev = directives(
    buildRendererCsp({ sidecarPort: 51234, isDev: true, devRendererUrl: 'http://localhost:5173' })
  )
  // Vite가 index.html에 인라인 react-refresh preamble을 주입한다.
  assert.equal(dev.get('script-src')?.includes("'unsafe-inline'"), true)
  assert.equal(dev.get('script-src')?.includes('http://localhost:5173'), true)
  // HMR WebSocket.
  assert.equal(dev.get('connect-src')?.includes('ws://localhost:5173'), true)

  const prod = directives(buildRendererCsp({ sidecarPort: 51234, isDev: false }))
  assert.equal(prod.get('script-src')?.includes("'unsafe-inline'"), false)
  assert.equal(
    prod.get('connect-src')?.some((v) => v.startsWith('ws://')),
    false,
    '배포본에 dev 전용 오리진이 새어 들어갔다'
  )
})

test('a malformed dev url does not poison the policy', () => {
  const map = directives(
    buildRendererCsp({ sidecarPort: 51234, isDev: true, devRendererUrl: 'not-a-url' })
  )
  assert.deepEqual(map.get('connect-src'), ['http://127.0.0.1:51234'])
})

test('no external origin is ever allowed', () => {
  for (const csp of [
    buildRendererCsp({ sidecarPort: 51234, isDev: false }),
    buildRendererCsp({ sidecarPort: 51234, isDev: true, devRendererUrl: 'http://localhost:5173' })
  ]) {
    // 로컬(127.0.0.1 / localhost) 외의 http(s) 오리진이 정책에 들어가면 안 된다.
    const external = csp.match(/https?:\/\/(?!127\.0\.0\.1|localhost)[^\s;]+/g)
    assert.equal(external, null, `외부 오리진이 허용됐다: ${external}`)
  }
})
