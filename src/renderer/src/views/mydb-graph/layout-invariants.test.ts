import { describe, expect, it } from 'vitest'
import type { MyDbEdge, MyDbNode } from '../../../../shared/mydb'
import { createInitialLayout } from '../MyDbView'
import { measureLayoutQuality } from './layout-quality'
import { radiusForNodes } from './layout-quality.bench'

const STAMP = '2026-01-01T00:00:00.000Z'
function core(id: string): MyDbNode {
  return { id, kind: 'core', title: id, createdAt: STAMP, updatedAt: STAMP }
}
function file(id: string): MyDbNode {
  return { id, kind: 'file', title: id, fileType: 'other', createdAt: STAMP, updatedAt: STAMP }
}
function contains(sourceId: string, targetId: string): MyDbEdge {
  return { id: `${sourceId}->${targetId}`, sourceId, targetId, relation: 'contains', createdAt: STAMP, updatedAt: STAMP }
}

function rng(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0
    return state / 4294967296
  }
}

function randomForest(seed: number, coreCount: number, maxFiles: number): { nodes: MyDbNode[]; edges: MyDbEdge[] } {
  const next = rng(seed)
  const nodes: MyDbNode[] = []
  const edges: MyDbEdge[] = []
  const ids: string[] = []
  for (let i = 0; i < coreCount; i += 1) {
    const id = `c${i}`
    ids.push(id)
    nodes.push(core(id))
    if (i > 0 && next() < 0.86) {
      const parent = ids[Math.floor(next() * i)] as string
      edges.push(contains(parent, id))
    }
    const files = Math.floor(next() * (maxFiles + 1))
    for (let f = 0; f < files; f += 1) {
      const fid = `${id}f${f}`
      nodes.push(file(fid))
      edges.push(contains(id, fid))
    }
  }
  return { nodes, edges }
}

describe('배치 불변식 스트레스', () => {
  const shapes: Array<[string, { nodes: MyDbNode[]; edges: MyDbEdge[] }]> = []
  for (let seed = 1; seed <= 24; seed += 1) {
    shapes.push([`무작위 숲 seed=${seed}`, randomForest(seed, 8 + (seed % 5) * 12, seed % 4 === 0 ? 18 : 6)])
  }
  // 손으로 고른 극단 모양
  {
    const nodes: MyDbNode[] = []
    const edges: MyDbEdge[] = []
    for (let i = 0; i < 40; i += 1) {
      nodes.push(core(`d${i}`))
      if (i > 0) edges.push(contains(`d${i - 1}`, `d${i}`))
      for (let f = 0; f < 5; f += 1) {
        nodes.push(file(`d${i}f${f}`))
        edges.push(contains(`d${i}`, `d${i}f${f}`))
      }
    }
    shapes.push(['깊이 40 사슬 + 파일 5', { nodes, edges }])
  }
  {
    const nodes: MyDbNode[] = [core('r')]
    const edges: MyDbEdge[] = []
    for (let i = 0; i < 70; i += 1) {
      nodes.push(core(`k${i}`))
      edges.push(contains('r', `k${i}`))
      for (let f = 0; f < 4; f += 1) {
        nodes.push(file(`k${i}f${f}`))
        edges.push(contains(`k${i}`, `k${i}f${f}`))
      }
    }
    shapes.push(['자식 70 + 각자 파일 4', { nodes, edges }])
  }
  {
    // 한 자식만 거대하고 나머지는 잎 — 원뿔이 크게 비대칭인 경우
    const nodes: MyDbNode[] = [core('r'), core('big')]
    const edges: MyDbEdge[] = [contains('r', 'big')]
    for (let i = 0; i < 5; i += 1) {
      nodes.push(core(`s${i}`))
      edges.push(contains('r', `s${i}`))
    }
    for (let i = 0; i < 12; i += 1) {
      nodes.push(core(`b${i}`))
      edges.push(contains('big', `b${i}`))
      for (let f = 0; f < 9; f += 1) {
        nodes.push(file(`b${i}f${f}`))
        edges.push(contains(`b${i}`, `b${i}f${f}`))
      }
    }
    shapes.push(['거대한 외아들 + 잎 형제 5', { nodes, edges }])
  }

  for (const [name, graph] of shapes) {
    it(`${name}: 교차 0 · 관통 0 · 겹침 0`, () => {
      const plan = createInitialLayout(graph.nodes, graph.edges, 1600, 900)
      for (const point of plan.positions.values()) {
        expect(Number.isFinite(point.x) && Number.isFinite(point.y)).toBe(true)
      }
      expect(plan.positions.size).toBe(graph.nodes.length)
      const quality = measureLayoutQuality({
        nodes: graph.nodes,
        edges: graph.edges,
        positions: plan.positions,
        radiusOf: radiusForNodes(graph.nodes, graph.edges)
      })
      let far = 0
      for (const point of plan.positions.values()) {
        far = Math.max(far, Math.hypot(point.x - plan.center.x, point.y - plan.center.y))
      }
      expect({ name, ...quality, far: Math.round(far) }).toMatchObject({
        nodeOverlaps: 0,
        edgeCrossings: 0,
        edgeNodeHits: 0
      })
      expect(far).toBeLessThan(200000)
    })
  }
})
