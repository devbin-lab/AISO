import type { MyDbEdge, MyDbNode } from '../../../../shared/mydb'
import { segmentsProperlyIntersect, type GraphPoint } from './routing'

/**
 * 그래프 배치 품질을 **수치로** 재는 자.
 *
 * "선이 꼬여서 보기 흉하다"는 눈으로만 판단하면 고쳐도 나아졌는지 알 수 없고,
 * 한 곳을 펴면 다른 곳이 엉키는 것도 놓친다. 여기서 재는 네 가지는 모두
 * 화면에서 실제로 보이는 흠에 하나씩 대응한다.
 *
 * 이 파일은 판정만 한다 — 배치를 바꾸지 않는다.
 */

export interface QualityInput {
  nodes: MyDbNode[]
  edges: MyDbEdge[]
  positions: Map<string, GraphPoint>
  /** 노드가 화면에서 차지하는 반지름. 겹침 판정의 기준. */
  radiusOf: (node: MyDbNode) => number
}

export interface LayoutQuality {
  /** 원이 서로 파고든 노드 쌍 수. 0 이어야 한다. */
  nodeOverlaps: number
  /** 가장 심하게 파고든 깊이(px). */
  worstOverlapDepth: number
  /** 서로 가로지르는 간선 쌍 수. 사용자가 말한 '선이 꼬인다'가 이것이다. */
  edgeCrossings: number
  /** 자기와 무관한 노드의 원을 뚫고 지나가는 간선 수. */
  edgeNodeHits: number
  /**
   * 자식이 둘 이상인 부모에 대해, 자식들이 부모 둘레에 얼마나 고르게 퍼졌는가.
   * 1 이면 완전 방사형(360° 균등), 0 에 가까울수록 한쪽으로 몰린 부채꼴이다.
   */
  radialSpread: number
  /**
   * 자식들이 실제로 **차지하는 각도 폭**의 평균(도).
   *
   * 원형 분산(radialSpread)만 보면 안 된다 — 반원(180°)에 고르게 퍼진 자식도
   * 분산은 0.54 로 높게 나와서, 눈에는 명백한 부채꼴인데 수치는 통과한다.
   * 실제로 이것 때문에 부채꼴을 '없다'고 잘못 보고한 적이 있다. 폭을 직접 잰다.
   */
  meanChildArcDegrees: number
  /** 부채꼴로 몰린 부모 수. 자식이 차지하는 폭이 FAN_ARC_LIMIT 보다 좁은 경우. */
  fanShapedParents: number
  /** 가장 좁게 몰린 부모의 폭(도). */
  narrowestArcDegrees: number
  parentsMeasured: number
}

/**
 * 자식이 부모 둘레에서 실제로 차지하는 각도 폭(도).
 *
 * 각도를 정렬해 가장 큰 빈틈을 찾고 360°에서 뺀다. 그 빈틈이 부모 쪽으로
 * 열린 자리다. 폭이 넓을수록 자식이 부모를 빙 두른다.
 */
export function childArcDegrees(angles: readonly number[]): number {
  if (angles.length < 2) return 0
  const sorted = [...angles].sort((left, right) => left - right)
  let widestGap = 0
  for (let i = 0; i < sorted.length; i += 1) {
    const next = sorted[(i + 1) % sorted.length]!
    const gap = (next - sorted[i]! + Math.PI * 2) % (Math.PI * 2)
    widestGap = Math.max(widestGap, gap)
  }
  return ((Math.PI * 2 - widestGap) * 180) / Math.PI
}

/**
 * 이보다 좁으면 부채꼴로 센다.
 *
 * 부모로 돌아가는 선이 지날 자리는 비워야 하므로 360°는 애초에 불가능하다.
 * 파일만 달린 코어가 실측으로 324~339°를 쓰므로 그 근처를 목표로 둔다.
 */
export const FAN_ARC_LIMIT = 270

/**
 * 자식이 n 명일 때 **도달 가능한 최대 폭**(도).
 *
 * 자식 사이 간격 n 개의 합이 360°이므로 가장 큰 간격은 아무리 고르게 놓아도
 * 360/n 이상이고, 따라서 폭은 360 − 360/n 을 넘을 수 없다. n=3 이면 240°,
 * n=4 면 270° 다.
 *
 * 이걸 따지지 않으면 자식이 셋인 부모는 **무슨 수를 써도** FAN_ARC_LIMIT(270°)
 * 을 넘길 수 없어 영원히 부채꼴로 집계된다. 실제로 그 때문에 도달 불가능한
 * 목표를 쫓을 뻔했다. 한계에 닿았으면 부채꼴로 세지 않는다.
 */
export function maxAttainableArcDegrees(childCount: number): number {
  if (childCount < 2) return 0
  return 360 - 360 / childCount
}

function dist(a: GraphPoint, b: GraphPoint): number {
  return Math.hypot(a.x - b.x, a.y - b.y)
}

/** 선분 ab 와 점 p 사이의 최단 거리. 간선이 노드를 뚫는지 볼 때 쓴다. */
export function pointSegmentDistance(p: GraphPoint, a: GraphPoint, b: GraphPoint): number {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const lengthSquared = dx * dx + dy * dy
  if (lengthSquared === 0) return dist(p, a)
  let t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / lengthSquared
  t = Math.max(0, Math.min(1, t))
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))
}

/**
 * 각도들이 원 둘레에 고르게 퍼진 정도(0~1).
 *
 * 원형 분산을 쓴다 — 단순 min/max 폭은 0°와 350°가 섞이면 '거의 한 바퀴'로
 * 잘못 읽는다. 결과 1 은 완전 균등, 0 은 한 방향으로 완전히 몰린 상태다.
 */
export function angularSpread(angles: readonly number[]): number {
  if (angles.length < 2) return 1
  let sx = 0
  let sy = 0
  for (const angle of angles) {
    sx += Math.cos(angle)
    sy += Math.sin(angle)
  }
  // 합 벡터가 짧을수록 고르게 퍼진 것이다.
  const resultant = Math.hypot(sx, sy) / angles.length
  return 1 - resultant
}

export function measureLayoutQuality(input: QualityInput): LayoutQuality {
  const { nodes, edges, positions, radiusOf } = input
  const placed = nodes.filter((node) => positions.has(node.id))
  const radius = new Map(placed.map((node) => [node.id, radiusOf(node)] as const))

  // ── 노드 겹침 ──
  let nodeOverlaps = 0
  let worstOverlapDepth = 0
  for (let i = 0; i < placed.length; i += 1) {
    for (let j = i + 1; j < placed.length; j += 1) {
      const a = placed[i]!
      const b = placed[j]!
      const pa = positions.get(a.id)!
      const pb = positions.get(b.id)!
      const need = (radius.get(a.id) ?? 0) + (radius.get(b.id) ?? 0)
      const gap = dist(pa, pb) - need
      if (gap < 0) {
        nodeOverlaps += 1
        worstOverlapDepth = Math.max(worstOverlapDepth, -gap)
      }
    }
  }

  // ── 간선 교차 ──
  const drawn = edges
    .map((edge) => ({
      edge,
      a: positions.get(edge.sourceId),
      b: positions.get(edge.targetId)
    }))
    .filter((entry): entry is { edge: MyDbEdge; a: GraphPoint; b: GraphPoint } => Boolean(entry.a && entry.b))

  let edgeCrossings = 0
  for (let i = 0; i < drawn.length; i += 1) {
    for (let j = i + 1; j < drawn.length; j += 1) {
      const left = drawn[i]!
      const right = drawn[j]!
      // 끝점을 공유하는 간선은 한 점에서 만나는 게 정상이라 교차로 세지 않는다.
      const shares =
        left.edge.sourceId === right.edge.sourceId ||
        left.edge.sourceId === right.edge.targetId ||
        left.edge.targetId === right.edge.sourceId ||
        left.edge.targetId === right.edge.targetId
      if (shares) continue
      if (segmentsProperlyIntersect(left.a, left.b, right.a, right.b)) edgeCrossings += 1
    }
  }

  // ── 간선이 남의 노드를 뚫는가 ──
  let edgeNodeHits = 0
  for (const { edge, a, b } of drawn) {
    for (const node of placed) {
      if (node.id === edge.sourceId || node.id === edge.targetId) continue
      const p = positions.get(node.id)!
      if (pointSegmentDistance(p, a, b) < (radius.get(node.id) ?? 0)) {
        edgeNodeHits += 1
        break
      }
    }
  }

  // ── 방사형인가 부채꼴인가 ──
  const childrenOf = new Map<string, string[]>()
  for (const edge of edges) {
    if (edge.relation !== 'contains') continue
    const list = childrenOf.get(edge.sourceId) ?? []
    list.push(edge.targetId)
    childrenOf.set(edge.sourceId, list)
  }
  let spreadSum = 0
  let arcSum = 0
  let parentsMeasured = 0
  let fanShapedParents = 0
  let narrowestArc = 360
  for (const [parentId, childIds] of childrenOf) {
    const parent = positions.get(parentId)
    if (!parent || childIds.length < 3) continue
    const angles: number[] = []
    for (const childId of childIds) {
      const child = positions.get(childId)
      if (!child) continue
      angles.push(Math.atan2(child.y - parent.y, child.x - parent.x))
    }
    if (angles.length < 3) continue
    spreadSum += angularSpread(angles)
    const arc = childArcDegrees(angles)
    arcSum += arc
    narrowestArc = Math.min(narrowestArc, arc)
    parentsMeasured += 1
    // 한계(360 − 360/n)와 목표(270°) 중 낮은 쪽이 이 부모가 실제로 노릴 수 있는
    // 폭이다. 부동소수 오차만큼은 봐준다.
    const attainable = Math.min(FAN_ARC_LIMIT, maxAttainableArcDegrees(angles.length))
    if (arc < attainable - 0.5) fanShapedParents += 1
  }

  return {
    nodeOverlaps,
    worstOverlapDepth: Math.round(worstOverlapDepth * 10) / 10,
    edgeCrossings,
    edgeNodeHits,
    radialSpread: parentsMeasured === 0 ? 1 : Math.round((spreadSum / parentsMeasured) * 1000) / 1000,
    meanChildArcDegrees: parentsMeasured === 0 ? 360 : Math.round(arcSum / parentsMeasured),
    fanShapedParents,
    narrowestArcDegrees: parentsMeasured === 0 ? 360 : Math.round(narrowestArc),
    parentsMeasured
  }
}
