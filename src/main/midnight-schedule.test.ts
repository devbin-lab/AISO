import assert from 'node:assert/strict'
import test from 'node:test'

/**
 * 자정 재시도 시각 계산.
 *
 * 주기 검사(기본 6시간)만으로는 날이 바뀐 뒤 최대 6시간 동안 어제 보고서가
 * 없다. 자정 직후 한 번 더 돌려 그 창을 없앤다. 이 계산이 틀리면 증상이
 * 조용히 돌아오므로(하루 늦은 보고서) 여기서 고정한다.
 *
 * index.ts 의 scheduleMyDbMidnightReport 와 같은 식이다. 그쪽은 Electron
 * 모듈을 끌어와 단위 테스트로 부르기 어려워 계산만 옮겨 검사한다.
 */
function nextMidnightWaitMs(now: Date): number {
  const nextMidnight = new Date(now)
  nextMidnight.setHours(24, 0, 30, 0)
  return Math.max(1000, nextMidnight.getTime() - now.getTime())
}

/** 대기 후 도달하는 시각. */
function firesAt(now: Date): Date {
  return new Date(now.getTime() + nextMidnightWaitMs(now))
}

test('자정을 넘긴 직후에 깨어난다 — 하루가 바뀐 뒤여야 한다', () => {
  const at = firesAt(new Date(2026, 7, 22, 14, 30))
  assert.equal(at.getDate(), 23, '다음 날이어야 한다')
  assert.equal(at.getHours(), 0)
  assert.equal(at.getMinutes(), 0)
  // 자정 정각이 아니라 30초 뒤 — 경계에서 '어제'가 흔들리지 않게 한다.
  assert.equal(at.getSeconds(), 30)
})

test('자정 직전에도 다음 자정으로 간다 — 같은 날 두 번 돌지 않는다', () => {
  const now = new Date(2026, 7, 22, 23, 59, 50)
  const wait = nextMidnightWaitMs(now)
  assert.ok(wait <= 60_000, `대기 ${wait}ms 가 너무 길다`)
  assert.equal(firesAt(now).getDate(), 23)
})

test('자정 직후에 깨어나면 다음 자정까지 기다린다', () => {
  // 재예약 시점이 이미 0시 0분 31초라도, 그 날 자정으로 되돌아가면 안 된다.
  const now = new Date(2026, 7, 23, 0, 0, 31)
  const at = firesAt(now)
  assert.equal(at.getDate(), 24, '다음 날 자정이어야 한다')
  assert.ok(nextMidnightWaitMs(now) > 23 * 60 * 60 * 1000)
})

test('달 경계를 넘는다', () => {
  assert.equal(firesAt(new Date(2026, 7, 31, 20, 0)).getMonth(), 8) // 9월
  assert.equal(firesAt(new Date(2026, 7, 31, 20, 0)).getDate(), 1)
})

test('해 경계를 넘는다', () => {
  const at = firesAt(new Date(2026, 11, 31, 22, 0))
  assert.equal(at.getFullYear(), 2027)
  assert.equal(at.getMonth(), 0)
  assert.equal(at.getDate(), 1)
})

test('대기 시간은 항상 양수다 — 0 이면 타이머가 폭주한다', () => {
  for (const hour of [0, 1, 6, 12, 18, 23]) {
    const wait = nextMidnightWaitMs(new Date(2026, 7, 22, hour, 0))
    assert.ok(wait >= 1000, `${hour}시의 대기 ${wait}ms`)
    assert.ok(wait <= 24 * 60 * 60 * 1000 + 60_000, `${hour}시의 대기가 하루를 넘는다`)
  }
})
