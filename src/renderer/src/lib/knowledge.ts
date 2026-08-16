import type {
  CreateKnowledgeRelationInput,
  CreateKnowledgeTopicInput,
  KnowledgeGraphSnapshot,
  KnowledgeNode
} from '../../../shared/knowledge'
import { authHeaders } from './backend'

function endpoint(port: number, path: string): string {
  return `http://127.0.0.1:${port}${path}`
}

async function request<T>(port: number, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(endpoint(port, path), {
    ...init,
    headers: {
      ...authHeaders(),
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init?.headers ?? {})
    }
  })
  if (!response.ok) {
    const body = await response.text().catch(() => '')
    if (response.status === 404 && path.startsWith('/knowledge/')) {
      // A renderer can update before the development backend reloads.  Never
      // surface FastAPI's raw {"detail":"Not Found"} payload as UI text.
      throw new Error('My DB 백엔드가 최신 화면을 준비하는 중입니다. Aiso를 다시 시작한 뒤 새로고침해 주세요.')
    }
    try {
      const parsed = JSON.parse(body) as { detail?: unknown }
      if (typeof parsed.detail === 'string' && parsed.detail.trim()) throw new Error(parsed.detail)
    } catch (reason) {
      if (reason instanceof Error && reason.message !== body) throw reason
    }
    throw new Error(`My DB 요청에 실패했습니다. (${response.status})`)
  }
  return response.json() as Promise<T>
}

export function fetchKnowledgeGraph(port: number): Promise<KnowledgeGraphSnapshot> {
  return request<KnowledgeGraphSnapshot>(port, '/knowledge/graph?limit=240')
}

export function createKnowledgeTopic(port: number, input: CreateKnowledgeTopicInput): Promise<KnowledgeNode> {
  return request<{ ok: boolean; node: KnowledgeNode }>(port, '/knowledge/topics', {
    method: 'POST',
    body: JSON.stringify(input)
  }).then((response) => response.node)
}

export function createKnowledgeRelation(
  port: number,
  input: CreateKnowledgeRelationInput
): Promise<void> {
  return request<{ ok: boolean }>(port, '/knowledge/relations', {
    method: 'POST',
    body: JSON.stringify(input)
  }).then(() => undefined)
}
