import { describe, expect, it } from 'vitest'
import type { MyDbEdge, MyDbNode } from '../../../../shared/mydb'
import { createInitialLayout } from '../MyDbView'
import { measureLayoutQuality } from './layout-quality'
import { measureCurrentLayout, radiusForNodes, REAL_LIBRARY } from './layout-quality.bench'

/**
 * 방사형 배치의 계약.
 *
 * 이 그래프는 간선이 전부 `contains` 인 순수 트리다. 트리를 부채꼴이 겹치지 않게
 * 놓으면 구조선 교차는 **구성상** 0 이다 — 줄이는 값이 아니라 0 이어야 하는 값이라
 * 여기서 0 으로 못박는다. 예전 배치는 교차 9 · 관통 9 였다.
 */

function core(id: string, title = id): MyDbNode {
  return { id, kind: 'core', title, createdAt: '2026-01-01T00:00:00.000Z', updatedAt: '2026-01-01T00:00:00.000Z' }
}
function file(id: string, title = id): MyDbNode {
  return { id, kind: 'file', title, fileType: 'other', createdAt: '2026-01-01T00:00:00.000Z', updatedAt: '2026-01-01T00:00:00.000Z' }
}
function contains(sourceId: string, targetId: string): MyDbEdge {
  return { id: `${sourceId}->${targetId}`, sourceId, targetId, relation: 'contains', createdAt: '2026-01-01T00:00:00.000Z', updatedAt: '2026-01-01T00:00:00.000Z' }
}
function allFinite(plan: { positions: Map<string, { x: number; y: number }> }): boolean {
  for (const p of plan.positions.values()) if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) return false
  return true
}

describe('실제 라이브러리 방사형 배치', () => {
  const q = measureCurrentLayout()

  it('선이 서로 가로지르지 않는다', () => {
    expect(q.edgeCrossings).toBe(0)
  })

  it('선이 남의 노드를 뚫고 지나가지 않는다', () => {
    expect(q.edgeNodeHits).toBe(0)
  })

  it('노드 원이 서로 파고들지 않는다', () => {
    expect(q.nodeOverlaps).toBe(0)
    expect(q.worstOverlapDepth).toBe(0)
  })

  it('자식이 부모 둘레에 고르게 흩어진다', () => {
    // 예전 배치는 0.874 였다. 1 은 완전 균등 방사형.
    // 예전 배치 0.874 → 원뿔 배치 0.982. 1 이 완전 균등 방사형.
    expect(q.radialSpread).toBeGreaterThan(0.97)
  })

  /**
   * ── 여기서부터는 '얼마나 방사형인가'의 래칫이다 ──
   *
   * 예전 배치는 노드가 **부모에게 물려받은 부채꼴 안에서만** 자식을 펼쳤다. 그래야
   * 형제 영역이 안 겹쳐 선이 교차하지 않지만, 갈래질 때마다 각이 반씩 잘려 내부 노드가
   * 구조적으로 부채꼴이 됐다(코어 자식을 둔 코어 157~193°). 하위 트리를 부채꼴이 아니라
   * **원뿔**로 잡아 그 제약을 풀었다 — 부모에서 본 각 구간만 서로 배타적이면 되므로
   * 자식이 부모 둘레 어디에나 앉을 수 있고, 교차·관통·겹침은 여전히 0 이다.
   *
   * 남은 숫자는 이제 **기하학의 천장**이지 배치의 흠이 아니다. 자식이 n 명이면 중심
   * 사이 틈의 합이 360° 이므로 제일 큰 틈은 아무리 고르게 놔도 360/n 보다 작아질 수
   * 없고, 따라서 폭은 360 − 360/n 이 최대다.
   *   · n=3 → 240° · n=4 → 270° · n=8 → 315° · n=17 → 339°
   * 실측 19개 부모 중 17개가 이 천장에 정확히 붙어 있고, 나머지 둘(Unity2D-이동사전 ·
   * github-학습노트)만 288/300 이다. 즉 FAN_ARC_LIMIT(270°) 아래로 남은 넷은 전부
   * 자식이 3~4명이라 **원리적으로** 270°를 넘을 수 없는 부모다.
   *
   * 이 값들은 래칫이다 — 나빠지면 실패하고, 좋아지면 숫자를 조여 준다.
   */
  it('천장에 못 미친 부모가 하나도 없다', () => {
    // 판정이 자식 수에 따른 천장(360 − 360/n)을 감안하므로 0 이 실제로 도달 가능한
    // 값이다. 예전 판정은 270° 고정이라 자식 3명짜리 부모를 영원히 부채꼴로 셌다.
    expect(q.fanShapedParents).toBe(0)
    expect(q.parentsMeasured).toBe(19)
  })

  it('가장 좁게 몰린 부모도 자기 천장에 붙어 있다', () => {
    // 자식 3명짜리 부모의 이론상 최대치가 240° 다.
    expect(q.narrowestArcDegrees).toBeGreaterThanOrEqual(240)
    // 19개 부모의 천장을 평균 내면 297.5° 다. 296 은 거기서 1.5° 안쪽이다.
    expect(q.meanChildArcDegrees).toBeGreaterThanOrEqual(296)
  })

  it('노드를 하나도 잃지 않는다', () => {
    expect(q.placed).toBe(REAL_LIBRARY.nodes.length)
  })
})

describe('방사형 배치 견고성', () => {
  it('같은 입력이면 좌표가 완전히 같다 — 창 크기가 바뀔 때마다 다시 도는 함수다', () => {
    const a = createInitialLayout(REAL_LIBRARY.nodes, REAL_LIBRARY.edges, 1600, 900)
    const b = createInitialLayout(REAL_LIBRARY.nodes, REAL_LIBRARY.edges, 1600, 900)
    expect([...a.positions.entries()]).toEqual([...b.positions.entries()])
  })

  it('GraphLayoutPlan 모양을 지킨다 — 드래그가 fileSlots 로 파일 위치를 다시 계산한다', () => {
    const plan = createInitialLayout(REAL_LIBRARY.nodes, REAL_LIBRARY.edges, 1600, 900)
    expect(plan.fileSlots.size).toBeGreaterThan(0)
    for (const slot of plan.fileSlots.values()) {
      expect(typeof slot.coreId).toBe('string')
      expect(Number.isFinite(slot.angle)).toBe(true)
      expect(Number.isFinite(slot.radius)).toBe(true)
    }
    expect(plan.center).toEqual({ x: 800, y: 450 })
  })

  const shapes: Array<[string, MyDbNode[], MyDbEdge[]]> = [
    ['빈 그래프', [], []],
    ['코어 하나', [core('a')], []],
    ['고아만', [file('f1'), file('f2'), core('c1')], []],
    ['파일 40개가 달린 코어', [core('c'), ...Array.from({ length: 40 }, (_, i) => file(`f${i}`))],
      Array.from({ length: 40 }, (_, i) => contains('c', `f${i}`))],
    ['깊이 20 외줄기', Array.from({ length: 20 }, (_, i) => core(`c${i}`)),
      Array.from({ length: 19 }, (_, i) => contains(`c${i}`, `c${i + 1}`))],
    ['자식 30개 별', [core('root'), ...Array.from({ length: 30 }, (_, i) => core(`k${i}`))],
      Array.from({ length: 30 }, (_, i) => contains('root', `k${i}`))],
    ['뿌리 20개', Array.from({ length: 20 }, (_, i) => core(`r${i}`)),
      Array.from({ length: 20 }, (_, i) => contains(`r${i}`, `r${i}f`))
        .map((e, i) => ({ ...e, targetId: `r${i}f` }))],
    ['계층과 고아 혼합', [core('r'), core('a'), file('x'), file('lonely')],
      [contains('r', 'a'), contains('a', 'x')]]
  ]

  for (const [name, nodes, edges] of shapes) {
    it(`${name}: NaN 없이 모든 노드를 배치한다`, () => {
      const withTargets = name === '뿌리 20개'
        ? [...nodes, ...Array.from({ length: 20 }, (_, i) => file(`r${i}f`))]
        : nodes
      const plan = createInitialLayout(withTargets, edges, 1600, 900)
      expect(allFinite(plan)).toBe(true)
      expect(plan.positions.size).toBe(withTargets.length)
    })
  }

  it('넓은 부채(자식 24 · 각자 파일 6)에서도 겹침과 교차가 없다', () => {
    const nodes: MyDbNode[] = [core('root')]
    const edges: MyDbEdge[] = []
    for (let i = 0; i < 24; i += 1) {
      nodes.push(core(`k${i}`))
      edges.push(contains('root', `k${i}`))
      for (let f = 0; f < 6; f += 1) {
        nodes.push(file(`k${i}f${f}`))
        edges.push(contains(`k${i}`, `k${i}f${f}`))
      }
    }
    const plan = createInitialLayout(nodes, edges, 1600, 900)
    const quality = measureLayoutQuality({
      nodes, edges, positions: plan.positions, radiusOf: radiusForNodes(nodes, edges)
    })
    expect(quality.nodeOverlaps).toBe(0)
    expect(quality.edgeCrossings).toBe(0)
    expect(quality.edgeNodeHits).toBe(0)
  })
})

describe('물리가 배치를 무너뜨리지 않기 위한 전제', () => {
  /**
   * 시뮬레이션의 구조 용수철은 자연 길이를 **배치가 정한 거리**로 써야 한다.
   *
   * 예전에는 240px 고정이었다. 방사형 배치는 깊이마다 고리 반지름이 다르므로
   * 한 값으로 끌어당기면 고리가 무너진다. 실제로 그랬다 — 초기 배치는 관통 0
   * 인데 물리를 거치면 1학기에서 나가는 두 선이 1.8° 로 붙어 2학년 원을
   * 29.9px 파고들었다. 계획 거리로 바꾼 뒤 관통 0 · 사이각 20.8° 가 됐다.
   *
   * 여기서는 그 전제를 고정한다: 계획 거리는 하나의 상수로 대체할 수 없을 만큼
   * 넓게 흩어져 있다. 이 값이 좁아지면 고정 길이로 되돌려도 티가 안 나므로,
   * 그때는 이 테스트가 먼저 알려 준다.
   */
  it('구조 간선의 계획 거리는 한 상수로 대체할 수 없다', () => {
    const plan = createInitialLayout(REAL_LIBRARY.nodes, REAL_LIBRARY.edges, 1600, 900)
    const distances: number[] = []
    for (const edge of REAL_LIBRARY.edges) {
      if (!plan.structuralCoreEdgeIds.has(edge.id)) continue
      const a = plan.coreTargets.get(edge.sourceId)
      const b = plan.coreTargets.get(edge.targetId)
      if (!a || !b) continue
      distances.push(Math.hypot(b.x - a.x, b.y - a.y))
    }
    expect(distances.length).toBeGreaterThan(5)
    const min = Math.min(...distances)
    const max = Math.max(...distances)
    // 예전 고정값 240px 은 이 범위 안의 한 점일 뿐이다.
    expect(max / min).toBeGreaterThan(1.8)
  })
})
