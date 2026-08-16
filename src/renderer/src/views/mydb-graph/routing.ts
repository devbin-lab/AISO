export interface GraphPoint {
  x: number
  y: number
}

export interface RouteNode extends GraphPoint {
  id: string
  radius: number
}

export interface RouteEdge {
  id: string
  sourceId: string
  targetId: string
}

export interface GraphRoute {
  edgeId: string
  sourceId: string
  targetId: string
  start: GraphPoint
  end: GraphPoint
  control?: GraphPoint
  waypoints?: GraphPoint[]
  points: GraphPoint[]
  secondary: boolean
}

interface CandidateRoute {
  control?: GraphPoint
  waypoints?: GraphPoint[]
  points: GraphPoint[]
}

interface GraphBounds {
  minX: number
  maxX: number
  minY: number
  maxY: number
}

const EPSILON = 0.0001
const CURVE_SAMPLES = 12
const MAX_SCORED_SECONDARY_ROUTES = 120

function hash(value: string): number {
  let result = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    result ^= value.charCodeAt(index)
    result = Math.imul(result, 16777619)
  }
  return result >>> 0
}

function distance(left: GraphPoint, right: GraphPoint): number {
  return Math.hypot(right.x - left.x, right.y - left.y)
}

function port(node: RouteNode, toward: GraphPoint): GraphPoint {
  const dx = toward.x - node.x
  const dy = toward.y - node.y
  const length = Math.hypot(dx, dy) || 1
  return {
    x: node.x + (dx / length) * (node.radius + 1.5),
    y: node.y + (dy / length) * (node.radius + 1.5)
  }
}

function sampleQuadratic(start: GraphPoint, control: GraphPoint, end: GraphPoint): GraphPoint[] {
  const points: GraphPoint[] = []
  for (let index = 0; index <= CURVE_SAMPLES; index += 1) {
    const t = index / CURVE_SAMPLES
    const oneMinusT = 1 - t
    points.push({
      x: oneMinusT * oneMinusT * start.x + 2 * oneMinusT * t * control.x + t * t * end.x,
      y: oneMinusT * oneMinusT * start.y + 2 * oneMinusT * t * control.y + t * t * end.y
    })
  }
  return points
}

function orientation(a: GraphPoint, b: GraphPoint, c: GraphPoint): number {
  return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)
}

/** Proper segment intersection. Collinear/shared endpoint segments are not crossings. */
export function segmentsProperlyIntersect(a: GraphPoint, b: GraphPoint, c: GraphPoint, d: GraphPoint): boolean {
  const abC = orientation(a, b, c)
  const abD = orientation(a, b, d)
  const cdA = orientation(c, d, a)
  const cdB = orientation(c, d, b)
  return (abC > EPSILON && abD < -EPSILON || abC < -EPSILON && abD > EPSILON)
    && (cdA > EPSILON && cdB < -EPSILON || cdA < -EPSILON && cdB > EPSILON)
}

function pointToSegmentDistance(point: GraphPoint, start: GraphPoint, end: GraphPoint): number {
  const dx = end.x - start.x
  const dy = end.y - start.y
  const lengthSquared = dx * dx + dy * dy
  if (lengthSquared < EPSILON) return distance(point, start)
  const t = Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / lengthSquared))
  return Math.hypot(point.x - (start.x + dx * t), point.y - (start.y + dy * t))
}

function routeLength(points: readonly GraphPoint[]): number {
  let length = 0
  for (let index = 1; index < points.length; index += 1) length += distance(points[index - 1]!, points[index]!)
  return length
}

function graphBounds(nodes: readonly RouteNode[]): GraphBounds | null {
  if (nodes.length === 0) return null
  return {
    minX: Math.min(...nodes.map((node) => node.x - node.radius)),
    maxX: Math.max(...nodes.map((node) => node.x + node.radius)),
    minY: Math.min(...nodes.map((node) => node.y - node.radius)),
    maxY: Math.max(...nodes.map((node) => node.y + node.radius))
  }
}

function outerDetourCandidates(start: GraphPoint, end: GraphPoint, bounds: GraphBounds | null): CandidateRoute[] {
  if (!bounds) return []
  const padding = Math.max(42, Math.min(128, Math.max(bounds.maxX - bounds.minX, bounds.maxY - bounds.minY) * 0.12))
  const routes = [
    [{ x: start.x, y: bounds.minY - padding }, { x: end.x, y: bounds.minY - padding }],
    [{ x: start.x, y: bounds.maxY + padding }, { x: end.x, y: bounds.maxY + padding }],
    [{ x: bounds.minX - padding, y: start.y }, { x: bounds.minX - padding, y: end.y }],
    [{ x: bounds.maxX + padding, y: start.y }, { x: bounds.maxX + padding, y: end.y }]
  ]
  return routes.map((waypoints) => ({
    waypoints,
    points: [start, ...waypoints, end]
  }))
}

function routeCrossings(candidate: readonly GraphPoint[], edge: RouteEdge, routes: readonly GraphRoute[]): number {
  let crossings = 0
  for (const route of routes) {
    if (
      route.sourceId === edge.sourceId
      || route.sourceId === edge.targetId
      || route.targetId === edge.sourceId
      || route.targetId === edge.targetId
    ) continue
    for (let sourceIndex = 1; sourceIndex < candidate.length; sourceIndex += 1) {
      for (let targetIndex = 1; targetIndex < route.points.length; targetIndex += 1) {
        if (segmentsProperlyIntersect(
          candidate[sourceIndex - 1]!,
          candidate[sourceIndex]!,
          route.points[targetIndex - 1]!,
          route.points[targetIndex]!
        )) crossings += 1
      }
    }
  }
  return crossings
}

function obstacleHits(candidate: readonly GraphPoint[], edge: RouteEdge, nodes: readonly RouteNode[]): number {
  let hits = 0
  for (const node of nodes) {
    if (node.id === edge.sourceId || node.id === edge.targetId) continue
    for (let index = 1; index < candidate.length; index += 1) {
      if (pointToSegmentDistance(node, candidate[index - 1]!, candidate[index]!) < node.radius + 6) {
        hits += 1
        break
      }
    }
  }
  return hits
}

function scoreCandidate(candidate: CandidateRoute, edge: RouteEdge, nodes: readonly RouteNode[], routes: readonly GraphRoute[]): number {
  return routeCrossings(candidate.points, edge, routes) * 10_000
    + obstacleHits(candidate.points, edge, nodes) * 2_000
    + routeLength(candidate.points) * 0.02
}

function anchorCandidate(source: RouteNode, target: RouteNode, candidate?: CandidateRoute): CandidateRoute {
  const firstDirection = candidate?.control ?? candidate?.waypoints?.[0] ?? target
  const lastDirection = candidate?.control ?? candidate?.waypoints?.at(-1) ?? source
  const start = port(source, firstDirection)
  const end = port(target, lastDirection)
  if (candidate?.control) {
    return {
      control: candidate.control,
      points: sampleQuadratic(start, candidate.control, end)
    }
  }
  if (candidate?.waypoints) {
    return {
      waypoints: candidate.waypoints,
      points: [start, ...candidate.waypoints, end]
    }
  }
  return { points: [start, end] }
}

function pairKey(edge: RouteEdge): string {
  return edge.sourceId < edge.targetId
    ? `${edge.sourceId}\u0000${edge.targetId}`
    : `${edge.targetId}\u0000${edge.sourceId}`
}

function leastCostCandidate(
  candidates: readonly CandidateRoute[],
  edge: RouteEdge,
  nodes: readonly RouteNode[],
  routes: readonly GraphRoute[]
): CandidateRoute {
  let best = candidates[0]!
  let bestScore = scoreCandidate(best, edge, nodes, routes)
  for (let index = 1; index < candidates.length; index += 1) {
    const current = candidates[index]!
    const score = scoreCandidate(current, edge, nodes, routes)
    if (score < bestScore) {
      best = current
      bestScore = score
    }
  }
  return best
}

/**
 * Builds stable node-border routes. Structural edges stay straight. Secondary
 * relationship edges choose the least-crossing route among a direct line and
 * two outside curves, so they do not cut through the middle of the hierarchy.
 */
export function buildGraphRoutes(
  edges: readonly RouteEdge[],
  nodes: readonly RouteNode[],
  secondaryEdgeIds: ReadonlySet<string>,
  center: GraphPoint
): Map<string, GraphRoute> {
  const nodesById = new Map(nodes.map((node) => [node.id, node]))
  const bounds = graphBounds(nodes)
  const routes: GraphRoute[] = []
  const append = (edge: RouteEdge, secondary: boolean, candidate?: CandidateRoute): void => {
    const source = nodesById.get(edge.sourceId)
    const target = nodesById.get(edge.targetId)
    if (!source || !target) return
    const route = anchorCandidate(source, target, candidate)
    const start = route.points[0]!
    const end = route.points.at(-1)!
    routes.push({
      edgeId: edge.id,
      sourceId: edge.sourceId,
      targetId: edge.targetId,
      start,
      end,
      ...(route.control ? { control: route.control } : {}),
      ...(route.waypoints ? { waypoints: route.waypoints } : {}),
      points: route.points,
      secondary
    })
  }

  const primary = edges.filter((edge) => !secondaryEdgeIds.has(edge.id)).sort((left, right) => left.id.localeCompare(right.id))
  for (const edge of primary) append(edge, false)

  const secondary = edges.filter((edge) => secondaryEdgeIds.has(edge.id)).sort((left, right) => left.id.localeCompare(right.id))
  const edgesByPair = new Map<string, RouteEdge[]>()
  for (const edge of edges) {
    const key = pairKey(edge)
    const pair = edgesByPair.get(key) ?? []
    pair.push(edge)
    edgesByPair.set(key, pair)
  }
  for (const [index, edge] of secondary.entries()) {
    const source = nodesById.get(edge.sourceId)
    const target = nodesById.get(edge.targetId)
    if (!source || !target) continue
    const direct = anchorCandidate(source, target)
    const start = direct.points[0]!
    const end = direct.points.at(-1)!
    const midpoint = { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 }
    const dx = end.x - start.x
    const dy = end.y - start.y
    const length = Math.hypot(dx, dy) || 1
    const normal = { x: -dy / length, y: dx / length }
    const outsideDot = normal.x * (midpoint.x - center.x) + normal.y * (midpoint.y - center.y)
    const preferredSign = outsideDot === 0 ? (hash(edge.id) % 2 === 0 ? 1 : -1) : Math.sign(outsideDot)
    const lane = Math.min(132, Math.max(34, length * 0.28) + (hash(edge.id) % 3) * 12)
    const pair = edgesByPair.get(pairKey(edge)) ?? []
    const primarySharesPair = pair.some((entry) => !secondaryEdgeIds.has(entry.id))
    const secondarySiblings = pair
      .filter((entry) => secondaryEdgeIds.has(entry.id))
      .sort((left, right) => left.id.localeCompare(right.id))
    const siblingIndex = secondarySiblings.findIndex((entry) => entry.id === edge.id)
    const parallelLane = primarySharesPair
      ? (siblingIndex % 2 === 0 ? 1 + Math.floor(siblingIndex / 2) : -1 - Math.floor(siblingIndex / 2))
      : siblingIndex - (secondarySiblings.length - 1) / 2
    if (parallelLane !== 0) {
      const control = {
        x: midpoint.x + normal.x * Math.max(-72, Math.min(72, parallelLane * 24)),
        y: midpoint.y + normal.y * Math.max(-72, Math.min(72, parallelLane * 24))
      }
      append(edge, true, { control, points: sampleQuadratic(start, control, end) })
      continue
    }
    const candidates = [
      direct,
      {
        control: { x: midpoint.x + normal.x * lane * preferredSign, y: midpoint.y + normal.y * lane * preferredSign },
        points: sampleQuadratic(start, { x: midpoint.x + normal.x * lane * preferredSign, y: midpoint.y + normal.y * lane * preferredSign }, end)
      },
      {
        control: { x: midpoint.x - normal.x * lane, y: midpoint.y - normal.y * lane },
        points: sampleQuadratic(start, { x: midpoint.x - normal.x * lane, y: midpoint.y - normal.y * lane }, end)
      },
      ...outerDetourCandidates(start, end, bounds)
    ].map((candidate) => anchorCandidate(source, target, candidate))
    const candidate = index < MAX_SCORED_SECONDARY_ROUTES
      ? leastCostCandidate(candidates, edge, nodes, routes)
      : candidates[1]!
    append(edge, true, candidate)
  }
  return new Map(routes.map((route) => [route.edgeId, route]))
}
