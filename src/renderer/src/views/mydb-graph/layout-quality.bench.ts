import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import type { MyDbEdge, MyDbNode } from '../../../../shared/mydb'
import { buildCoreGraphStructure, createInitialLayout } from '../MyDbView'
import { measureLayoutQuality, type LayoutQuality } from './layout-quality'

/**
 * 실제 사용자 라이브러리(코어 41 · 파일 116 · 간선 152)로 배치 품질을 잰다.
 *
 * 합성 그래프로는 이 증상이 재현되지 않는다 — 꼬임은 한쪽에 몰린 깊은 계층과
 * 파일이 많이 달린 코어가 섞일 때 생긴다. 픽스처는 앱에서 그대로 떠 왔다.
 */

const fixture = JSON.parse(
  readFileSync(resolve('src/renderer/src/views/mydb-graph/__fixtures__/real-library.json'), 'utf8')
) as { nodes: MyDbNode[]; edges: MyDbEdge[] }

export const REAL_LIBRARY = fixture

/** 화면이 실제로 그리는 반지름과 같은 산식. 겹침 판정이 화면과 어긋나면 안 된다. */
export function radiusForNodes(nodes: MyDbNode[], edges: MyDbEdge[]): (node: MyDbNode) => number {
  const { coreRadii } = buildCoreGraphStructure(nodes, edges)
  return (node) => (node.kind === 'core' ? coreRadii.get(node.id) ?? 10 : 6)
}

export function measureCurrentLayout(
  width = 1600,
  height = 900
): LayoutQuality & { placed: number } {
  const { nodes, edges } = fixture
  const plan = createInitialLayout(nodes, edges, width, height)
  const quality = measureLayoutQuality({
    nodes,
    edges,
    positions: plan.positions,
    radiusOf: radiusForNodes(nodes, edges)
  })
  return { ...quality, placed: plan.positions.size }
}
