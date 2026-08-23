import { describe, expect, it } from 'vitest'
import { resolveCollisions, type CollisionBody } from './collision'

/**
 * 겹침 분리는 alpha 와 무관해야 한다는 것이 이 모듈의 존재 이유다.
 * 기존 반발력은 감쇠하면 사라져서, 다 식은 화면에 겹친 노드가 그대로 남았다.
 */

function overlapCount(bodies: CollisionBody[], padding = 0): number {
  let count = 0
  for (let i = 0; i < bodies.length; i += 1) {
    for (let j = i + 1; j < bodies.length; j += 1) {
      const a = bodies[i]!
      const b = bodies[j]!
      const need = a.radius + b.radius + padding * 2
      // 부동소수 오차만큼은 봐준다.
      if (Math.hypot(b.x - a.x, b.y - a.y) < need - 0.01) count += 1
    }
  }
  return count
}

describe('노드 충돌 분리', () => {
  it('겹친 두 노드를 떼어 놓는다', () => {
    const bodies = [
      { x: 0, y: 0, radius: 10 },
      { x: 5, y: 0, radius: 10 }
    ]
    resolveCollisions(bodies, { iterations: 12, strength: 1 })
    expect(overlapCount(bodies)).toBe(0)
  })

  it('완전히 겹친 노드도 푼다 — 방향이 없어도 멈추지 않는다', () => {
    const bodies = [
      { x: 40, y: 40, radius: 8 },
      { x: 40, y: 40, radius: 8 },
      { x: 40, y: 40, radius: 8 }
    ]
    resolveCollisions(bodies, { iterations: 30, strength: 1 })
    expect(overlapCount(bodies)).toBe(0)
  })

  it('같은 입력은 같은 결과를 낸다 — 배치가 실행마다 달라지면 안 된다', () => {
    const make = (): CollisionBody[] => [
      { x: 0, y: 0, radius: 12 },
      { x: 0, y: 0, radius: 12 },
      { x: 3, y: 1, radius: 9 },
      { x: 1, y: 2, radius: 15 }
    ]
    const first = make()
    const second = make()
    resolveCollisions(first, { iterations: 8, strength: 1 })
    resolveCollisions(second, { iterations: 8, strength: 1 })
    expect(first).toEqual(second)
  })

  it('여유(padding)만큼 더 떨어뜨린다 — 보이지 않는 큰 원', () => {
    const bodies = [
      { x: 0, y: 0, radius: 10 },
      { x: 15, y: 0, radius: 10 }
    ]
    resolveCollisions(bodies, { iterations: 20, strength: 1, padding: 12 })
    const gap = Math.hypot(bodies[1]!.x - bodies[0]!.x, bodies[1]!.y - bodies[0]!.y)
    // 10 + 10 + 12*2 = 44
    expect(gap).toBeGreaterThanOrEqual(43.9)
  })

  it('고정된 노드는 밀리지 않는다 — 끌고 있는 노드가 손에서 벗어나면 안 된다', () => {
    const bodies = [
      { x: 0, y: 0, radius: 10 },
      { x: 6, y: 0, radius: 10 }
    ]
    resolveCollisions(bodies, {
      iterations: 20,
      strength: 1,
      isPinned: (_body, index) => index === 0
    })
    expect(bodies[0]!.x).toBe(0)
    expect(bodies[0]!.y).toBe(0)
    expect(overlapCount(bodies)).toBe(0)
  })

  it('닿지 않는 노드는 건드리지 않는다', () => {
    const bodies = [
      { x: 0, y: 0, radius: 10 },
      { x: 400, y: 0, radius: 10 }
    ]
    const moved = resolveCollisions(bodies, { iterations: 4, strength: 1 })
    expect(moved).toBe(0)
    expect(bodies[1]!.x).toBe(400)
  })

  it('많은 노드가 뭉쳐 있어도 전부 푼다', () => {
    const bodies: CollisionBody[] = []
    for (let i = 0; i < 60; i += 1) {
      // 좁은 자리에 의도적으로 몰아넣는다.
      bodies.push({ x: (i % 8) * 4, y: Math.floor(i / 8) * 4, radius: 9 })
    }
    resolveCollisions(bodies, { iterations: 60, strength: 1 })
    expect(overlapCount(bodies)).toBe(0)
  })

  it('노드가 하나거나 없으면 아무 일도 하지 않는다', () => {
    expect(resolveCollisions([], { iterations: 4 })).toBe(0)
    expect(resolveCollisions([{ x: 1, y: 2, radius: 5 }], { iterations: 4 })).toBe(0)
  })
})
