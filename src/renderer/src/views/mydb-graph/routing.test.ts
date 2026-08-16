import { describe, expect, it } from 'vitest'
import { buildGraphRoutes, segmentsProperlyIntersect } from './routing'

function crossings(
  left: ReadonlyArray<{ x: number; y: number }>,
  right: ReadonlyArray<{ x: number; y: number }>
): number {
  let count = 0
  for (let leftIndex = 1; leftIndex < left.length; leftIndex += 1) {
    for (let rightIndex = 1; rightIndex < right.length; rightIndex += 1) {
      if (segmentsProperlyIntersect(
        left[leftIndex - 1]!,
        left[leftIndex]!,
        right[rightIndex - 1]!,
        right[rightIndex]!
      )) count += 1
    }
  }
  return count
}

describe('My DB relationship routing', () => {
  it('attaches a structural line to node borders instead of their centres', () => {
    const routes = buildGraphRoutes(
      [{ id: 'tree', sourceId: 'parent', targetId: 'child' }],
      [
        { id: 'parent', x: 0, y: 0, radius: 12 },
        { id: 'child', x: 100, y: 0, radius: 12 }
      ],
      new Set(),
      { x: 50, y: 0 }
    )

    const route = routes.get('tree')!
    expect(route.start.x).toBeCloseTo(13.5)
    expect(route.end.x).toBeCloseTo(86.5)
    expect(route.start.y).toBe(0)
    expect(route.end.y).toBe(0)
  })

  it('routes a crossing secondary relation around the structural tree', () => {
    const nodes = [
      { id: 'top-left', x: 0, y: 0, radius: 10 },
      { id: 'bottom-right', x: 160, y: 160, radius: 10 },
      { id: 'bottom-left', x: 0, y: 160, radius: 10 },
      { id: 'top-right', x: 160, y: 0, radius: 10 }
    ]
    const routes = buildGraphRoutes(
      [
        { id: 'structural', sourceId: 'top-left', targetId: 'bottom-right' },
        { id: 'secondary', sourceId: 'bottom-left', targetId: 'top-right' }
      ],
      nodes,
      new Set(['secondary']),
      { x: 80, y: 80 }
    )

    const structural = routes.get('structural')!
    const secondary = routes.get('secondary')!
    expect(secondary.secondary).toBe(true)
    expect(secondary.control ?? secondary.waypoints).toBeTruthy()
    expect(crossings(structural.points, secondary.points)).toBe(0)
  })

  it('keeps a non-crossing secondary relationship direct and stable', () => {
    const nodes = [
      { id: 'left-top', x: 0, y: 0, radius: 8 },
      { id: 'left-bottom', x: 0, y: 120, radius: 8 },
      { id: 'right-top', x: 180, y: 0, radius: 8 },
      { id: 'right-bottom', x: 180, y: 120, radius: 8 }
    ]
    const edges = [
      { id: 'tree', sourceId: 'left-top', targetId: 'left-bottom' },
      { id: 'related', sourceId: 'right-top', targetId: 'right-bottom' }
    ]
    const first = buildGraphRoutes(edges, nodes, new Set(['related']), { x: 90, y: 60 })
    const second = buildGraphRoutes([...edges].reverse(), [...nodes].reverse(), new Set(['related']), { x: 90, y: 60 })

    const route = first.get('related')!
    expect(route.control).toBeUndefined()
    expect(route.waypoints).toBeUndefined()
    expect(second.get('related')).toEqual(route)
  })

  it('uses separate lanes for relationships that share the same two nodes', () => {
    const routes = buildGraphRoutes(
      [
        { id: 'alpha', sourceId: 'left', targetId: 'right' },
        { id: 'beta', sourceId: 'left', targetId: 'right' }
      ],
      [
        { id: 'left', x: 0, y: 0, radius: 10 },
        { id: 'right', x: 160, y: 0, radius: 10 }
      ],
      new Set(['alpha', 'beta']),
      { x: 80, y: 0 }
    )

    const alpha = routes.get('alpha')!
    const beta = routes.get('beta')!
    expect(alpha.control).toBeDefined()
    expect(beta.control).toBeDefined()
    expect(alpha.control!.y).toBeLessThan(0)
    expect(beta.control!.y).toBeGreaterThan(0)
  })

  it('uses the curve direction for the node-border port of a parallel relation', () => {
    const routes = buildGraphRoutes(
      [
        { id: 'structural', sourceId: 'left', targetId: 'right' },
        { id: 'related', sourceId: 'left', targetId: 'right' }
      ],
      [
        { id: 'left', x: 0, y: 0, radius: 10 },
        { id: 'right', x: 160, y: 0, radius: 10 }
      ],
      new Set(['related']),
      { x: 80, y: 0 }
    )

    const route = routes.get('related')!
    expect(route.control).toBeDefined()
    expect(route.start.y).toBeGreaterThan(0)
    expect(route.end.y).toBeGreaterThan(0)
  })
})
