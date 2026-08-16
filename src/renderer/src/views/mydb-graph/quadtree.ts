/**
 * Barnes-Hut quadtree used by the My DB canvas.
 *
 * This is deliberately the same force approximation used by the standalone
 * My DB System.  Keeping it here rather than doing an O(n^2) pair loop is
 * what lets a larger personal library keep its soft, responsive motion.
 */
export interface GraphBody {
  x: number
  y: number
  vx: number
  vy: number
}

interface QuadCell<T extends GraphBody> {
  x0: number
  y0: number
  x1: number
  y1: number
  cx: number
  cy: number
  mass: number
  body: T | null
  children: QuadCell<T>[] | null
}

export const BARNES_HUT_THETA = 0.8
const MIN_CELL_SIZE = 1

function makeCell<T extends GraphBody>(x0: number, y0: number, x1: number, y1: number): QuadCell<T> {
  return { x0, y0, x1, y1, cx: 0, cy: 0, mass: 0, body: null, children: null }
}

function insertIntoChild<T extends GraphBody>(cell: QuadCell<T>, body: T): void {
  const midX = (cell.x0 + cell.x1) / 2
  const midY = (cell.y0 + cell.y1) / 2
  const index = (body.x < midX ? 0 : 1) + (body.y < midY ? 0 : 2)
  insertBody(cell.children![index]!, body)
}

function insertBody<T extends GraphBody>(cell: QuadCell<T>, body: T): void {
  cell.cx = (cell.cx * cell.mass + body.x) / (cell.mass + 1)
  cell.cy = (cell.cy * cell.mass + body.y) / (cell.mass + 1)
  cell.mass += 1
  if (cell.mass === 1) {
    cell.body = body
    return
  }
  if (cell.x1 - cell.x0 < MIN_CELL_SIZE) return
  if (!cell.children) {
    const midX = (cell.x0 + cell.x1) / 2
    const midY = (cell.y0 + cell.y1) / 2
    cell.children = [
      makeCell(cell.x0, cell.y0, midX, midY),
      makeCell(midX, cell.y0, cell.x1, midY),
      makeCell(cell.x0, midY, midX, cell.y1),
      makeCell(midX, midY, cell.x1, cell.y1)
    ]
    if (cell.body) {
      insertIntoChild(cell, cell.body)
      cell.body = null
    }
  }
  insertIntoChild(cell, body)
}

export function buildQuadTree<T extends GraphBody>(bodies: readonly T[]): QuadCell<T> | null {
  if (bodies.length === 0) return null
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const body of bodies) {
    minX = Math.min(minX, body.x)
    minY = Math.min(minY, body.y)
    maxX = Math.max(maxX, body.x)
    maxY = Math.max(maxY, body.y)
  }
  const size = Math.max(maxX - minX, maxY - minY) || 1
  const root = makeCell<T>(minX, minY, minX + size, minY + size)
  for (const body of bodies) insertBody(root, body)
  return root
}

/** Adds the Barnes-Hut approximated repulsion force to one body. */
export function applyRepulsion<T extends GraphBody>(
  cell: QuadCell<T>,
  body: T,
  repulsion: number,
  alpha: number,
  thetaSquared: number
): void {
  if (cell.mass === 0) return
  let dx = body.x - cell.cx
  let dy = body.y - cell.cy
  let distanceSquared = dx * dx + dy * dy
  const size = cell.x1 - cell.x0
  if (cell.children === null || size * size < thetaSquared * distanceSquared) {
    if (cell.children === null && cell.body === body && cell.mass === 1) return
    if (distanceSquared < 1) {
      // A tiny deterministic-looking jitter prevents coincident nodes from
      // making the force undefined. The original engine does the same.
      dx = Math.random() - 0.5
      dy = Math.random() - 0.5
      distanceSquared = 1
    }
    const distance = Math.sqrt(distanceSquared)
    const force = ((repulsion * cell.mass) / distanceSquared) * alpha
    body.vx += (dx / distance) * force
    body.vy += (dy / distance) * force
    return
  }
  for (const child of cell.children) applyRepulsion(child, body, repulsion, alpha, thetaSquared)
}
