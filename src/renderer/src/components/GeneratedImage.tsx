import { Fragment, useEffect, useMemo, useState } from 'react'
import type {
  ComfyGeneratedImage,
  ComfyPipelineSnapshot,
  ComfyPromptPolicySnapshot
} from '../../../shared/agent'
import { authHeaders } from '../lib/backend'

interface Props {
  image: ComfyGeneratedImage
  backendPort: number | null
}

interface WorkflowEdge {
  from: string
  to: string
  input: string
  outputIndex: number
}

interface WorkflowNode {
  id: string
  classType: string
  inputs: Record<string, unknown>
  level: number
}

interface WorkflowGraph {
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  levels: WorkflowNode[][]
}

type JsonObject = Record<string, unknown>

const MAX_WORKFLOW_NODES = 100
const MAX_WORKFLOW_INPUTS = 80
const CORE_INPUT_ORDER = [
  'ckpt_name',
  'unet_name',
  'clip_name1',
  'clip_name2',
  'vae_name',
  'text',
  'width',
  'height',
  'batch_size',
  'seed',
  'noise_seed',
  'steps',
  'cfg',
  'guidance',
  'sampler_name',
  'scheduler',
  'denoise',
  'filename_prefix',
  'weight_dtype',
  'type'
] as const

const INPUT_LABEL: Record<string, string> = {
  ckpt_name: 'Checkpoint',
  unet_name: 'Diffusion model',
  clip_name1: 'CLIP-L',
  clip_name2: 'T5XXL',
  vae_name: 'VAE',
  text: 'Prompt',
  width: 'Width',
  height: 'Height',
  batch_size: 'Batch',
  seed: 'Seed',
  noise_seed: 'Noise seed',
  steps: 'Steps',
  cfg: 'CFG',
  guidance: 'Guidance',
  sampler_name: 'Sampler',
  scheduler: 'Scheduler',
  denoise: 'Denoise',
  filename_prefix: 'Filename prefix',
  weight_dtype: 'Weight dtype',
  type: 'Type'
}

function asObject(value: unknown): JsonObject | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as JsonObject
    : null
}

function safeText(value: unknown, maxLength = 4_000): string {
  if (typeof value !== 'string') return ''
  const normalized = value.trim()
  return normalized.slice(0, maxLength)
}

function nodeIdCompare(left: string, right: string): number {
  return left.localeCompare(right, undefined, { numeric: true, sensitivity: 'base' })
}

function connectionFrom(value: unknown, knownIds: Set<string>): { id: string; outputIndex: number } | null {
  if (!Array.isArray(value) || value.length !== 2) return null
  const rawId = value[0]
  const outputIndex = value[1]
  if ((typeof rawId !== 'string' && typeof rawId !== 'number') || typeof outputIndex !== 'number') {
    return null
  }
  const id = String(rawId)
  return knownIds.has(id) && Number.isInteger(outputIndex) && outputIndex >= 0
    ? { id, outputIndex }
    : null
}

function buildWorkflowGraph(value: unknown): WorkflowGraph | null {
  const raw = asObject(value)
  if (!raw) return null
  const parsed = Object.entries(raw)
    .slice(0, MAX_WORKFLOW_NODES)
    .flatMap(([id, nodeValue]) => {
      const node = asObject(nodeValue)
      const classType = safeText(node?.class_type, 120)
      const inputs = asObject(node?.inputs)
      if (!classType || !inputs) return []
      return [{ id: id.slice(0, 80), classType, inputs: Object.fromEntries(Object.entries(inputs).slice(0, MAX_WORKFLOW_INPUTS)) }]
    })
    .sort((left, right) => nodeIdCompare(left.id, right.id))
  if (parsed.length === 0) return null

  const knownIds = new Set(parsed.map((node) => node.id))
  const edges: WorkflowEdge[] = []
  for (const node of parsed) {
    for (const [input, value] of Object.entries(node.inputs)) {
      const connection = connectionFrom(value, knownIds)
      if (connection) {
        edges.push({ from: connection.id, to: node.id, input: input.slice(0, 80), outputIndex: connection.outputIndex })
      }
    }
  }

  const indegree = new Map(parsed.map((node) => [node.id, 0]))
  const outgoing = new Map(parsed.map((node) => [node.id, [] as WorkflowEdge[]]))
  const level = new Map(parsed.map((node) => [node.id, 0]))
  for (const edge of edges) {
    indegree.set(edge.to, (indegree.get(edge.to) ?? 0) + 1)
    outgoing.get(edge.from)?.push(edge)
  }
  const queue = parsed.filter((node) => indegree.get(node.id) === 0).map((node) => node.id)
  queue.sort(nodeIdCompare)
  const visited = new Set<string>()
  while (queue.length) {
    const id = queue.shift()!
    if (visited.has(id)) continue
    visited.add(id)
    for (const edge of outgoing.get(id) ?? []) {
      level.set(edge.to, Math.max(level.get(edge.to) ?? 0, (level.get(id) ?? 0) + 1))
      const remaining = (indegree.get(edge.to) ?? 1) - 1
      indegree.set(edge.to, remaining)
      if (remaining === 0) {
        queue.push(edge.to)
        queue.sort(nodeIdCompare)
      }
    }
  }
  const fallbackLevel = Math.max(0, ...level.values()) + 1
  for (const node of parsed) {
    if (!visited.has(node.id)) level.set(node.id, fallbackLevel)
  }
  const nodes: WorkflowNode[] = parsed.map((node) => ({ ...node, level: level.get(node.id) ?? 0 }))
  const grouped = new Map<number, WorkflowNode[]>()
  for (const node of nodes) {
    const group = grouped.get(node.level) ?? []
    group.push(node)
    grouped.set(node.level, group)
  }
  const levels = [...grouped.entries()]
    .sort(([left], [right]) => left - right)
    .map(([, group]) => group.sort((left, right) => nodeIdCompare(left.id, right.id)))
  return { nodes, edges, levels }
}

function formatInputValue(value: unknown, key: string): string | null {
  if (Array.isArray(value) && value.length === 2) return null
  if (typeof value === 'string') {
    const limit = key === 'text' ? 180 : 120
    return value.length > limit ? `${value.slice(0, limit)}…` : value
  }
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') return String(value)
  if (value == null) return null
  try {
    const rendered = JSON.stringify(value)
    return rendered.length > 120 ? `${rendered.slice(0, 120)}…` : rendered
  } catch {
    return null
  }
}

function coreInputs(node: WorkflowNode): { key: string; label: string; value: string }[] {
  return CORE_INPUT_ORDER.flatMap((key) => {
    if (!(key in node.inputs)) return []
    const value = formatInputValue(node.inputs[key], key)
    return value == null ? [] : [{ key, label: INPUT_LABEL[key] ?? key, value }]
  })
}

function normalizePolicy(value: unknown): ComfyPromptPolicySnapshot | null {
  const raw = asObject(value)
  if (!raw) return null
  const id = safeText(raw.id, 120)
  const label = safeText(raw.label, 160)
  const description = safeText(raw.description, 1_000)
  if (!id && !label && !description) return null
  const additions = (candidate: unknown): string[] => Array.isArray(candidate)
    ? candidate.filter((item): item is string => typeof item === 'string').map((item) => item.trim()).filter(Boolean).slice(0, 20)
    : []
  return {
    id: id || 'unknown',
    label: label || id || '프롬프트 정책',
    description,
    addedPositive: additions(raw.addedPositive),
    addedNegative: additions(raw.addedNegative)
  }
}

function normalizePipeline(value: unknown): ComfyPipelineSnapshot | null {
  const raw = asObject(value)
  if (!raw) return null
  if (
    !['aiso-built-in', 'user-workflow'].includes(String(raw.source)) ||
    typeof raw.nodeCount !== 'number' || !Number.isInteger(raw.nodeCount) || raw.nodeCount < 1 ||
    typeof raw.vaeDecode !== 'boolean' ||
    !['conditioning', 'positive-constraints', 'connected-empty', 'not-connected'].includes(String(raw.negativeMode)) ||
    typeof raw.scaleProcess !== 'boolean'
  ) return null
  const processingNodes = Array.isArray(raw.processingNodes)
    ? raw.processingNodes.filter((item): item is string => typeof item === 'string').slice(0, 20)
    : []
  return {
    source: raw.source as ComfyPipelineSnapshot['source'],
    nodeCount: raw.nodeCount,
    vaeDecode: raw.vaeDecode,
    negativeMode: raw.negativeMode as ComfyPipelineSnapshot['negativeMode'],
    scaleProcess: raw.scaleProcess,
    processingNodes
  }
}

function safeJson(value: unknown): string {
  try {
    const rendered = JSON.stringify(value, null, 2)
    return rendered.length > 120_000 ? `${rendered.slice(0, 120_000)}\n…(이하 생략)` : rendered
  } catch {
    return '워크플로 JSON을 표시할 수 없습니다.'
  }
}

function WorkflowNodeCard({ node, edges }: { node: WorkflowNode; edges: WorkflowEdge[] }): React.JSX.Element {
  const incoming = edges.filter((edge) => edge.to === node.id)
  const inputs = coreInputs(node)
  return (
    <article className="workflow-node">
      <header>
        <span className="mono">#{node.id}</span>
        <strong>{node.classType}</strong>
      </header>
      {incoming.length > 0 && (
        <div className="workflow-node__edges">
          {incoming.map((edge) => (
            <span key={`${edge.from}-${edge.to}-${edge.input}`} title={`출력 ${edge.outputIndex}`}>
              #{edge.from} → {INPUT_LABEL[edge.input] ?? edge.input}
            </span>
          ))}
        </div>
      )}
      {inputs.length > 0 && (
        <dl>
          {inputs.map((input) => (
            <Fragment key={input.key}>
              <dt>{input.label}</dt>
              <dd className={input.key === 'text' || input.key.includes('name') || input.key === 'filename_prefix' ? '' : 'mono'}>
                {input.value}
              </dd>
            </Fragment>
          ))}
        </dl>
      )}
    </article>
  )
}

function WorkflowInspector({ image }: { image: ComfyGeneratedImage }): React.JSX.Element | null {
  const workflowValue: unknown = image.workflow
  const graph = useMemo(() => buildWorkflowGraph(workflowValue), [workflowValue])
  const policy = useMemo(() => normalizePolicy(image.promptPolicy), [image.promptPolicy])
  const hasPromptTrace = Boolean(
    image.promptPolicy || image.originalPrompt || image.effectivePrompt || image.effectiveNegativePrompt
  )
  if (!graph && !hasPromptTrace) return null

  const originalPrompt = safeText(image.originalPrompt) || image.prompt
  const originalNegative = image.originalNegativePrompt === undefined
    ? image.negativePrompt
    : safeText(image.originalNegativePrompt)
  const effectivePrompt = safeText(image.effectivePrompt) || image.prompt
  const effectiveNegative = image.effectiveNegativePrompt === undefined
    ? image.negativePrompt
    : safeText(image.effectiveNegativePrompt)
  return (
    <details className="workflow-inspector">
      <summary>
        실제 ComfyUI 노드 워크플로 보기
        {graph && <span>{graph.nodes.length}개 노드 · {graph.edges.length}개 연결</span>}
      </summary>
      <div className="workflow-inspector__body">
        {hasPromptTrace && (
          <section className="prompt-trace" aria-label="프롬프트 적용 과정">
            <div className="prompt-trace__step">
              <span>1 · Agent 작성 원본 프롬프트</span>
              <p>{originalPrompt || '기록되지 않음'}</p>
              {originalNegative && (
                <div className="prompt-trace__negative">
                  <b>요청한 제외 요소</b>
                  <p>{originalNegative}</p>
                </div>
              )}
            </div>
            <div className="prompt-trace__arrow" aria-hidden="true">→</div>
            <div className="prompt-trace__step prompt-trace__policy">
              <span>2 · 적용 정책</span>
              <strong>{policy?.label ?? '정책 기록 없음'}</strong>
              {policy?.description && <p>{policy.description}</p>}
              {policy && (policy.addedPositive.length > 0 || policy.addedNegative.length > 0) && (
                <div className="prompt-policy-additions">
                  {policy.addedPositive.map((item) => <em className="is-positive" key={`p-${item}`}>+ {item}</em>)}
                  {policy.addedNegative.map((item) => <em className="is-negative" key={`n-${item}`}>− {item}</em>)}
                </div>
              )}
            </div>
            <div className="prompt-trace__arrow" aria-hidden="true">→</div>
            <div className="prompt-trace__step">
              <span>3 · 실제 제출 프롬프트</span>
              <p>{effectivePrompt || '기록되지 않음'}</p>
              {effectiveNegative && (
                <div className="prompt-trace__negative">
                  <b>Negative</b>
                  <p>{effectiveNegative}</p>
                </div>
              )}
            </div>
          </section>
        )}

        {graph ? (
          <section className="workflow-graph-wrap" aria-label="ComfyUI 노드 연결도">
            <div className="workflow-graph">
              {graph.levels.map((nodes, index) => (
                <Fragment key={`level-${index}`}>
                  <div className="workflow-level">
                    <div className="workflow-level__label">단계 {index + 1}</div>
                    {nodes.map((node) => <WorkflowNodeCard key={node.id} node={node} edges={graph.edges} />)}
                  </div>
                  {index < graph.levels.length - 1 && (
                    <div className="workflow-flow-arrow" aria-hidden="true">→</div>
                  )}
                </Fragment>
              ))}
            </div>
          </section>
        ) : workflowValue ? (
          <div className="workflow-inspector__invalid">워크플로 스냅샷 형식을 해석할 수 없습니다.</div>
        ) : null}

        {workflowValue != null && (
          <details className="workflow-raw">
            <summary>Raw JSON</summary>
            <pre className="mono">{safeJson(workflowValue)}</pre>
          </details>
        )}
      </div>
    </details>
  )
}

function GeneratedImage({ image, backendPort }: Props): React.JSX.Element {
  const [objectUrl, setObjectUrl] = useState('')
  const [error, setError] = useState('')
  const fileLabel = useMemo(
    () => [image.subfolder, image.filename].filter(Boolean).join('/'),
    [image.filename, image.subfolder]
  )
  const displayedPrompt = image.effectivePrompt ?? image.prompt
  const displayedNegativePrompt = image.effectiveNegativePrompt ?? image.negativePrompt
  const pipeline = useMemo(() => normalizePipeline(image.pipeline), [image.pipeline])
  const negativeModeLabel = pipeline?.negativeMode === 'conditioning'
    ? pipeline.source === 'user-workflow'
      ? '네거티브 입력 바인딩'
      : '네거티브 조건 적용'
    : pipeline?.negativeMode === 'positive-constraints'
      ? '제외 요소 긍정 변환'
      : pipeline?.negativeMode === 'connected-empty'
        ? '네거티브 입력 연결 · 내용 없음'
        : '네거티브 입력 미연결'

  useEffect(() => {
    if (backendPort == null) {
      setObjectUrl('')
      setError('Aiso 백엔드가 실행 중일 때 이미지를 불러올 수 있습니다.')
      return
    }

    const controller = new AbortController()
    let currentUrl = ''
    let disposed = false
    const params = new URLSearchParams({
      base_url: image.baseUrl,
      filename: image.filename,
      subfolder: image.subfolder,
      type: image.storageType
    })
    setError('')
    fetch(`http://127.0.0.1:${backendPort}/comfy/image?${params.toString()}`, {
      headers: authHeaders(),
      signal: controller.signal
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const blob = await response.blob()
        if (!blob.type.startsWith('image/')) throw new Error('이미지 형식이 아닙니다.')
        currentUrl = URL.createObjectURL(blob)
        if (disposed) URL.revokeObjectURL(currentUrl)
        else setObjectUrl(currentUrl)
      })
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') {
          setObjectUrl('')
          setError(`생성 이미지를 불러오지 못했습니다: ${String(reason)}`)
        }
      })

    return () => {
      disposed = true
      controller.abort()
      if (currentUrl) URL.revokeObjectURL(currentUrl)
    }
  }, [backendPort, image.baseUrl, image.filename, image.storageType, image.subfolder])

  return (
    <article className="generated-image">
      <div className="generated-image__head">
        <div>
          <strong>{image.profileName}</strong>
          <span>{image.modelName}</span>
        </div>
        <span className="mono">Seed {image.seed}</span>
      </div>
      {objectUrl ? (
        <img src={objectUrl} alt={image.prompt || 'ComfyUI 생성 이미지'} />
      ) : error ? (
        <div className="generated-image__error">{error}</div>
      ) : (
        <div className="generated-image__loading">이미지를 불러오는 중…</div>
      )}
      <div className="generated-image__meta">
        <span>{image.width} × {image.height}</span>
        <span>{image.steps} steps</span>
        <span>CFG {image.cfg}</span>
        <span>{image.sampler} · {image.scheduler}</span>
      </div>
      {pipeline && (
        <div className="generated-image__pipeline" aria-label="실제 생성 파이프라인 요약">
          <span>{pipeline.source === 'aiso-built-in' ? 'Aiso 계열별 기본 워크플로' : '사용자 연결 워크플로'}</span>
          <span>결과 경로 {pipeline.nodeCount}개 노드</span>
          <span className={pipeline.vaeDecode ? 'is-on' : 'is-off'}>
            VAE 디코드 {pipeline.vaeDecode ? '경로 포함' : '경로 미포함'}
          </span>
          <span>{negativeModeLabel}</span>
          <span className={pipeline.scaleProcess ? 'is-on' : 'is-off'}>
            스케일 처리 노드 {pipeline.scaleProcess ? '경로 포함' : '경로 미포함'}
          </span>
          {pipeline.processingNodes.length > 0 && (
            <span title={pipeline.processingNodes.join(', ')}>
              처리 노드 {pipeline.processingNodes.join(', ')}
            </span>
          )}
        </div>
      )}
      <details>
        <summary>생성 정보</summary>
        <dl>
          <dt>선택 이유</dt>
          <dd>{image.selectionReason}</dd>
          <dt>프롬프트</dt>
          <dd>{displayedPrompt}</dd>
          {displayedNegativePrompt && (
            <>
              <dt>네거티브</dt>
              <dd>{displayedNegativePrompt}</dd>
            </>
          )}
          <dt>결과 파일</dt>
          <dd className="mono">{fileLabel}</dd>
        </dl>
      </details>
      <WorkflowInspector image={image} />
      {objectUrl && (
        <a className="btn btn--ghost2 btn--sm" href={objectUrl} download={image.filename}>
          이미지 저장
        </a>
      )}
    </article>
  )
}

export default GeneratedImage
