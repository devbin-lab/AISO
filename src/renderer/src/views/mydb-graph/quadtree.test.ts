import { describe, expect, it } from 'vitest'
import { applyRepulsion, BARNES_HUT_THETA, buildQuadTree } from './quadtree'

describe('My DB Barnes-Hut graph force', () => {
  it('keeps a single body stationary and pushes two separate bodies apart', () => {
    const single = { x: 0, y: 0, vx: 0, vy: 0 }
    const singleTree = buildQuadTree([single])
    expect(singleTree).not.toBeNull()
    applyRepulsion(singleTree!, single, 2500, 0.5, BARNES_HUT_THETA ** 2)
    expect(single.vx).toBe(0)
    expect(single.vy).toBe(0)

    const left = { x: -40, y: 0, vx: 0, vy: 0 }
    const right = { x: 40, y: 0, vx: 0, vy: 0 }
    const tree = buildQuadTree([left, right])
    expect(tree).not.toBeNull()
    applyRepulsion(tree!, left, 2500, 0.5, BARNES_HUT_THETA ** 2)
    applyRepulsion(tree!, right, 2500, 0.5, BARNES_HUT_THETA ** 2)
    expect(left.vx).toBeLessThan(0)
    expect(right.vx).toBeGreaterThan(0)
  })
})
