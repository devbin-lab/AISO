import assert from 'node:assert/strict'
import test from 'node:test'
import type { NvidiaCapabilitySnapshot } from '../shared/nvidia.ts'
import {
  NvidiaDiscordConsentStore,
  consentMatchesTarget,
  type ConsentFileOps,
  type NvidiaDiscordConsentRecord,
  type NvidiaDiscordConsentTarget
} from './nvidia-discord-consent.ts'

/**
 * 앱을 껐다 켤 때마다 Discord 승인을 다시 눌러야 하던 문제의 계약.
 *
 * 재시작은 무엇이 어디로 가는지를 바꾸지 않는다. 지켜야 할 성질은 "이 프로세스에서
 * 승인했다"가 아니라 "지금 이 대상에 대해 승인했다"이다. 그래서 승인을 대상에 묶어
 * 저장하되, 대상이나 기능 검사 근거가 조금이라도 어긋나면 스스로 무효가 되어야 한다.
 */

const BUILD_ENDPOINT = 'https://integrate.api.nvidia.com/v1'

function target(model = 'moonshotai/kimi-k3'): NvidiaDiscordConsentTarget {
  return { deploymentMode: 'build', endpoint: BUILD_ENDPOINT, model }
}

function capability(checkedAt = '2026-09-08T00:00:00.000Z', tools = 'supported' as const): NvidiaCapabilitySnapshot {
  return {
    schemaVersion: 1,
    binding: { deploymentMode: 'build', endpoint: BUILD_ENDPOINT },
    model: 'moonshotai/kimi-k3',
    capabilities: { chat: 'supported', stream: 'supported', tools },
    checkedAt
  } as NvidiaCapabilitySnapshot
}

function record(overrides: Partial<NvidiaDiscordConsentRecord> = {}): NvidiaDiscordConsentRecord {
  return {
    schemaVersion: 1,
    deploymentMode: 'build',
    endpoint: BUILD_ENDPOINT,
    model: 'moonshotai/kimi-k3',
    capabilityCheckedAt: '2026-09-08T00:00:00.000Z',
    consentedAt: '2026-09-08T00:01:00.000Z',
    ...overrides
  }
}

test('같은 대상이면 저장된 승인이 그대로 통한다 — 재시작마다 다시 묻지 않는다', () => {
  assert.equal(consentMatchesTarget(record(), target(), capability()), true)
})

test('모델을 바꾸면 승인이 무효다', () => {
  assert.equal(consentMatchesTarget(record(), target('meta/llama-3.3-70b-instruct'), capability()), false)
})

test('배포 대상을 바꾸면 승인이 무효다', () => {
  const nim: NvidiaDiscordConsentTarget = {
    deploymentMode: 'nim',
    endpoint: 'http://127.0.0.1:8000/v1',
    model: 'moonshotai/kimi-k3'
  }
  assert.equal(consentMatchesTarget(record(), nim, capability()), false)
})

test('기능 검사를 다시 하면 승인이 무효다 — 근거가 달라졌다', () => {
  assert.equal(consentMatchesTarget(record(), target(), capability('2026-09-08T05:00:00.000Z')), false)
})

test('기능 검사 캐시가 비면 승인이 무효다 — 키 교체·삭제가 여기로 이어진다', () => {
  assert.equal(consentMatchesTarget(record(), target(), null), false)
})

test('도구 호출이 확인되지 않은 검사로는 승인이 살아나지 않는다', () => {
  assert.equal(consentMatchesTarget(record(), target(), capability(undefined, 'unknown')), false)
})

test('저장된 승인이 없으면 당연히 무효다', () => {
  assert.equal(consentMatchesTarget(null, target(), capability()), false)
})

test('대상이 없으면 무효다 — 모델을 지운 상태', () => {
  assert.equal(consentMatchesTarget(record(), null, capability()), false)
})

// ── 저장소 ────────────────────────────────────────────────────────────────

function memoryOps(): { ops: ConsentFileOps; files: Map<string, string> } {
  const files = new Map<string, string>()
  return {
    files,
    ops: {
      exists: (path) => files.has(path),
      read: (path) => files.get(path) ?? '',
      writeExclusive: (path, contents) => {
        if (files.has(path)) throw new Error('exists')
        files.set(path, contents)
      },
      rename: (from, to) => {
        files.set(to, files.get(from) ?? '')
        files.delete(from)
      },
      remove: (path) => void files.delete(path),
      mkdir: () => undefined
    }
  }
}

test('기록한 승인을 다시 읽어 온다 — 다음 실행이 이 값을 본다', () => {
  const { ops } = memoryOps()
  const store = new NvidiaDiscordConsentStore('C:/data/consent.json', ops)
  store.record(target(), capability())
  const read = store.read()
  assert.ok(read)
  assert.equal(read!.model, 'moonshotai/kimi-k3')
  assert.equal(read!.capabilityCheckedAt, '2026-09-08T00:00:00.000Z')
  assert.equal(consentMatchesTarget(read, target(), capability()), true)
})

test('임시 파일을 남기지 않는다', () => {
  const { ops, files } = memoryOps()
  new NvidiaDiscordConsentStore('C:/data/consent.json', ops).record(target(), capability())
  assert.deepEqual([...files.keys()], ['C:/data/consent.json'])
})

test('지우면 다시 물어본다', () => {
  const { ops } = memoryOps()
  const store = new NvidiaDiscordConsentStore('C:/data/consent.json', ops)
  store.record(target(), capability())
  store.clear()
  assert.equal(store.read(), null)
})

test('파일이 없으면 null 이다 — 첫 실행', () => {
  const { ops } = memoryOps()
  assert.equal(new NvidiaDiscordConsentStore('C:/data/consent.json', ops).read(), null)
})

test('망가진 파일은 승인으로 읽지 않는다 — 손상이 자동 활성화가 되면 안 된다', () => {
  const { ops, files } = memoryOps()
  files.set('C:/data/consent.json', '{ not json')
  assert.equal(new NvidiaDiscordConsentStore('C:/data/consent.json', ops).read(), null)
})

test('모르는 스키마 버전은 승인으로 읽지 않는다', () => {
  const { ops, files } = memoryOps()
  files.set('C:/data/consent.json', JSON.stringify({ ...record(), schemaVersion: 2 }))
  assert.equal(new NvidiaDiscordConsentStore('C:/data/consent.json', ops).read(), null)
})

test('필드가 빠진 파일은 승인으로 읽지 않는다', () => {
  const { ops, files } = memoryOps()
  files.set('C:/data/consent.json', JSON.stringify({ schemaVersion: 1, deploymentMode: 'build' }))
  assert.equal(new NvidiaDiscordConsentStore('C:/data/consent.json', ops).read(), null)
})
