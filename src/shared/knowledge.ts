export type KnowledgeNodeKind =
  | 'agent_session'
  | 'conversation'
  | 'document'
  | 'file'
  | 'calendar'
  | 'image'
  | 'integration'
  | 'skill'
  | 'topic'

export interface KnowledgeNode {
  id: string
  kind: KnowledgeNodeKind | string
  title: string
  metadata: Record<string, string>
  createdAt: string
  updatedAt: string
}

export interface KnowledgeEdge {
  id: string
  sourceId: string
  targetId: string
  relation: string
  metadata: Record<string, string>
  updatedAt: string
}

export interface KnowledgeChange {
  id: string
  createdAt: string
  toolName: string
  summary: string
  details: Record<string, string>
  targetNodeId: string | null
}

export interface KnowledgeGraphSnapshot {
  nodes: KnowledgeNode[]
  edges: KnowledgeEdge[]
  changes: KnowledgeChange[]
}

export interface CreateKnowledgeTopicInput {
  title: string
}

export interface CreateKnowledgeRelationInput {
  sourceId: string
  targetId: string
  relation: 'related' | 'references' | 'contains' | 'depends_on'
}
