import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  ATTACHMENT_GRACE_MS,
  looksLikeAttachmentId,
  referencedAttachmentIds,
  unreferencedAttachmentIds
} from './attachment-gc.ts'

const HOUR = 60 * 60 * 1000
const NOW = 1_700_000_000_000
const OLD = NOW - ATTACHMENT_GRACE_MS - HOUR
const FRESH = NOW - HOUR

const A = '3cb0cafc-7ff2-48f4-a658-6a7df43e3474'
const B = '5b0c5a92-74f0-4d14-a3f7-4983eaf428d4'

test('a referenced attachment is never swept, however old', () => {
  const gone = unreferencedAttachmentIds({
    entries: [{ id: A, modifiedAtMs: 0 }],
    live: new Set([A]),
    nowMs: NOW
  })
  assert.deepEqual(gone, [], '대화가 참조 중인 첨부를 지웠다')
})

test('an unreferenced attachment past the grace period is swept', () => {
  const gone = unreferencedAttachmentIds({
    entries: [{ id: A, modifiedAtMs: OLD }],
    live: new Set(),
    nowMs: NOW
  })
  assert.deepEqual(gone, [A])
})

test('a freshly staged attachment is protected even with no reference yet', () => {
  // 스테이징된 첨부는 전송 전까지 어떤 DB 행에도 없고 렌더러 state에만 있다.
  // 유예가 없으면 "첨부해 두고 잠시 뒤 전송" 시나리오에서 사용자 파일이 사라진다.
  const gone = unreferencedAttachmentIds({
    entries: [{ id: A, modifiedAtMs: FRESH }],
    live: new Set(),
    nowMs: NOW
  })
  assert.deepEqual(gone, [], '유예 기간 안의 첨부를 지웠다 — 전송 대기 중일 수 있다')
})

test('the grace boundary is inclusive so an exactly-expired entry is swept', () => {
  const gone = unreferencedAttachmentIds({
    entries: [{ id: A, modifiedAtMs: NOW - ATTACHMENT_GRACE_MS }],
    live: new Set(),
    nowMs: NOW
  })
  assert.deepEqual(gone, [A])
})

test('mixed store sweeps only the unreferenced expired entries', () => {
  const gone = unreferencedAttachmentIds({
    entries: [
      { id: A, modifiedAtMs: OLD }, // 미참조 + 만료 → 삭제
      { id: B, modifiedAtMs: OLD } // 참조됨 → 보존
    ],
    live: new Set([B]),
    nowMs: NOW
  })
  assert.deepEqual(gone, [A])
})

test('reference scan finds ids in both chat and agent conversation shapes', () => {
  // 채팅은 messages[].attachments, 에이전트는 items[]와 history[] 양쪽에 담는다.
  const chat = JSON.stringify({ messages: [{ role: 'user', attachments: [A] }] })
  const agent = JSON.stringify({
    items: [{ kind: 'user', attachments: [{ id: B, name: 'x.pdf' }] }],
    history: [{ role: 'user', attachments: [{ id: B }] }]
  })
  const live = referencedAttachmentIds([chat, agent])
  assert.equal(live.has(A), true)
  assert.equal(live.has(B), true)
})

test('reference scan is case-insensitive and tolerates empty rows', () => {
  const live = referencedAttachmentIds(['', JSON.stringify({ x: A.toUpperCase() })])
  assert.equal(live.has(A), true)
})

test('only uuid-shaped directory names are treated as attachment folders', () => {
  // 저장소에 섞인 다른 파일·폴더를 지우지 않기 위한 방어.
  assert.equal(looksLikeAttachmentId(A), true)
  assert.equal(looksLikeAttachmentId('not-a-uuid'), false)
  assert.equal(looksLikeAttachmentId('..'), false)
  assert.equal(looksLikeAttachmentId(''), false)
})
