import { describe, expect, it } from 'vitest'
import type { MyDbEdge, MyDbNode } from '../../../shared/mydb'
import { buildLibraryTreeRows } from './MyDbView'

const nodes: MyDbNode[] = [
  { id: 'course', kind: 'core', title: '게임 데이터 수업', createdAt: '2026-08-16T00:00:00Z', updatedAt: '2026-08-16T00:00:00Z' },
  { id: 'practice', kind: 'core', title: '수업 중 실습', createdAt: '2026-08-16T00:00:00Z', updatedAt: '2026-08-16T00:00:00Z' },
  { id: 'sheet', kind: 'file', title: '6강 실습.xlsx', fileType: 'spreadsheet', size: 1, createdAt: '2026-08-16T00:00:00Z', updatedAt: '2026-08-16T00:00:00Z' },
  { id: 'slides', kind: 'file', title: '6강 강의.pptx', fileType: 'slides', size: 1, createdAt: '2026-08-16T00:00:00Z', updatedAt: '2026-08-16T00:00:00Z' },
]

const edges: MyDbEdge[] = [
  { id: 'e1', sourceId: 'course', targetId: 'practice', relation: 'contains', createdAt: '2026-08-16T00:00:00Z', updatedAt: '2026-08-16T00:00:00Z' },
  { id: 'e2', sourceId: 'practice', targetId: 'sheet', relation: 'contains', createdAt: '2026-08-16T00:00:00Z', updatedAt: '2026-08-16T00:00:00Z' },
  { id: 'e3', sourceId: 'practice', targetId: 'slides', relation: 'contains', createdAt: '2026-08-16T00:00:00Z', updatedAt: '2026-08-16T00:00:00Z' },
]

describe('buildLibraryTreeRows', () => {
  it('keeps files under their core folders and filters by the exact extension', () => {
    const rows = buildLibraryTreeRows(nodes, edges, '', 'XLSX')

    expect(rows.map((row) => [row.node.id, row.depth])).toEqual([
      ['course', 0],
      ['practice', 1],
      ['sheet', 2],
    ])
  })

  it('hides only the collapsed core descendants', () => {
    const rows = buildLibraryTreeRows(nodes, edges, '', 'all', new Set(['course']))

    expect(rows.map((row) => [row.node.id, row.depth])).toEqual([
      ['course', 0],
    ])
  })

  it('keeps a core own files before nested core folders', () => {
    const index = { id: 'index', kind: 'file' as const, title: '00_목차.md', fileType: 'document' as const, size: 1, createdAt: '2026-08-16T00:00:00Z', updatedAt: '2026-08-16T00:00:00Z' }
    const rows = buildLibraryTreeRows(
      [...nodes, index],
      [...edges, { id: 'e4', sourceId: 'course', targetId: 'index', relation: 'contains', createdAt: '2026-08-16T00:00:00Z', updatedAt: '2026-08-16T00:00:00Z' }],
      '',
      'all'
    )

    expect(rows.map((row) => row.node.id)).toEqual(['course', 'index', 'practice', 'slides', 'sheet'])
  })
})
