import { useEffect, useMemo, useState } from 'react'
import type { BackendInfo } from '../../../shared/backend'
import {
  type AgentToolAvailability,
  type AgentToolCatalogEntry,
  type AgentToolCategory,
  TOOL_LABEL
} from '../../../shared/agent'
import { fetchAgentToolCatalog } from '../lib/agent'

const CATEGORY_ORDER: AgentToolCategory[] = [
  'plan',
  'files',
  'execution',
  'research',
  'automation',
  'rag',
  'discord',
  'image'
]

const CATEGORY_LABEL: Record<AgentToolCategory, string> = {
  plan: '계획',
  files: '파일·폴더',
  execution: '실행·검증',
  research: '웹 조사',
  automation: '자동화 스킬',
  rag: 'RAG',
  discord: '디스코드',
  image: '이미지 생성'
}

const AVAILABILITY_LABEL: Record<AgentToolAvailability, string> = {
  always: '기본 제공',
  workspace: '작업 폴더 필요',
  rag: 'RAG 조건부',
  discord: 'Discord 조건부',
  image: '이미지 요청 시'
}

interface Props {
  backend: BackendInfo
  active: boolean
}

type ToolFilter = 'all' | AgentToolCategory

function approvalSummary(tool: AgentToolCatalogEntry): string {
  const modes = (['manual', 'read', 'auto'] as const)
    .filter((mode) => tool.approval[mode])
    .map((mode) => ({ manual: '수동', read: '읽기', auto: '자동' })[mode])
  if (modes.length === 0) return '승인 없음'
  if (modes.length === 3) return '모든 모드에서 승인'
  return `${modes.join('·')} 모드에서 승인`
}

function approvalBadge(tool: AgentToolCatalogEntry): string {
  const modes = (['manual', 'read', 'auto'] as const)
    .filter((mode) => tool.approval[mode])
    .map((mode) => ({ manual: '수동', read: '읽기', auto: '자동' })[mode])
  if (modes.length === 0) return '승인 없이 실행'
  if (modes.length === 3) return '항상 승인'
  return `${modes.join('·')} 승인`
}

function plain(text: string): string {
  return text.replaceAll('**', '')
}

export default function AgentToolCatalog({ backend, active }: Props): React.JSX.Element {
  const [tools, setTools] = useState<AgentToolCatalogEntry[]>([])
  const [loadState, setLoadState] = useState<'loading' | 'ready' | 'waiting' | 'error'>('waiting')
  const [error, setError] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const [filter, setFilter] = useState<ToolFilter>('all')

  useEffect(() => {
    if (!active) return
    if (backend.state !== 'ready' || backend.port == null) {
      setLoadState('waiting')
      setError('')
      return
    }

    let cancelled = false
    setLoadState('loading')
    setError('')
    void fetchAgentToolCatalog(backend.port)
      .then((catalog) => {
        if (cancelled) return
        setTools(catalog.tools)
        setLoadState('ready')
      })
      .catch((reason: unknown) => {
        if (cancelled) return
        setTools([])
        setLoadState('error')
        setError(reason instanceof Error ? reason.message : '도구 목록을 불러오지 못했습니다.')
      })

    return () => {
      cancelled = true
    }
  }, [active, backend.port, backend.state, refreshKey])

  const groups = useMemo(
    () => CATEGORY_ORDER
      .filter((category) => filter === 'all' || filter === category)
      .map((category) => ({ category, tools: tools.filter((tool) => tool.category === category) }))
      .filter((group) => group.tools.length > 0),
    [filter, tools]
  )

  const refresh = (): void => setRefreshKey((value) => value + 1)

  return (
    <section className="agent-tool-catalog" aria-busy={loadState === 'loading'}>
      <header className="agent-tool-catalog__header">
        <div className="agent-tool-catalog__heading">
          <div className="agent-tool-catalog__title-row">
            <h2>기본 도구</h2>
            {loadState === 'ready' && <span className="agent-tool-catalog__count">내장 {tools.length}개</span>}
          </div>
          <p>
            Aiso Agent의 내장 도구입니다. 필요한 항목만 세부 정보를 펼쳐 확인할 수 있습니다.
          </p>
        </div>
        <button
          className="btn btn--ghost2 btn--sm"
          type="button"
          disabled={loadState === 'loading'}
          onClick={refresh}
        >
          {loadState === 'loading' ? '불러오는 중…' : '새로 고침'}
        </button>
      </header>

      {loadState === 'waiting' && (
        <div className="agent-tool-catalog__notice" role="status">
          Agent 백엔드 준비 후 실제 도구 목록을 표시합니다.
        </div>
      )}
      {loadState === 'error' && (
        <div className="agent-tool-catalog__notice agent-tool-catalog__notice--error" role="alert">
          {error}
        </div>
      )}
      {loadState === 'ready' && (
        <>
          <div className="agent-tool-catalog__filters" aria-label="도구 범주 필터">
            <button
              className={`agent-tool-filter${filter === 'all' ? ' is-active' : ''}`}
              type="button"
              aria-pressed={filter === 'all'}
              onClick={() => setFilter('all')}
            >
              전체
            </button>
            {CATEGORY_ORDER.map((category) => (
              <button
                className={`agent-tool-filter${filter === category ? ' is-active' : ''}`}
                type="button"
                aria-pressed={filter === category}
                onClick={() => setFilter(category)}
                key={category}
              >
                {CATEGORY_LABEL[category]}
              </button>
            ))}
          </div>
          {groups.length === 0 ? (
            <div className="agent-tool-catalog__notice" role="status">
              이 범주에 표시할 도구가 없습니다.
            </div>
          ) : (
            <div className="agent-tool-catalog__groups">
              {groups.map((group) => (
                <section className="agent-tool-group" key={group.category}>
                  <header className="agent-tool-group__header">
                    <div>
                      <span
                        className={`agent-tool-group__mark agent-tool-group__mark--${group.category}`}
                        aria-hidden="true"
                      />
                      <h3>{CATEGORY_LABEL[group.category]}</h3>
                    </div>
                    <span>{group.tools.length}개</span>
                  </header>
                  <div className="agent-tool-grid">
                  {group.tools.map((tool) => (
                    <article className="agent-tool-card" key={tool.name}>
                      <div className="agent-tool-card__head">
                        <div className="agent-tool-card__identity">
                          <h4>{TOOL_LABEL[tool.name] ?? tool.name}</h4>
                        </div>
                        <span className="agent-tool-card__availability">
                          {AVAILABILITY_LABEL[tool.availability]}
                        </span>
                      </div>
                      <p className="agent-tool-card__description">{plain(tool.description)}</p>
                      <div className="agent-tool-card__meta">
                        <span>{approvalBadge(tool)}</span>
                        {tool.mutates && <span>작업 폴더 변경</span>}
                      </div>
                      <details className="agent-tool-card__details">
                        <summary>
                          <span>세부 정보</span>
                          <em>입력 {tool.parameters.length}개</em>
                        </summary>
                        <div className="agent-tool-card__detail-list">
                          <div>
                            <span>도구 ID</span>
                            <code>{tool.name}</code>
                          </div>
                          <div>
                            <span>승인 정책</span>
                            <b>{approvalSummary(tool)}</b>
                          </div>
                          {tool.mutates && (
                            <div>
                              <span>변경 범위</span>
                              <b>작업 폴더</b>
                            </div>
                          )}
                          {tool.requirements.length > 0 && (
                            <div>
                              <span>사용 조건</span>
                              <b>{tool.requirements.join(' · ')}</b>
                            </div>
                          )}
                        </div>
                        {tool.parameters.length > 0 && (
                          <ul className="agent-tool-card__parameters">
                            {tool.parameters.map((parameter) => (
                              <li key={parameter.name}>
                                <code>{parameter.name}</code>
                                {parameter.description && <span>{plain(parameter.description)}</span>}
                              </li>
                            ))}
                          </ul>
                        )}
                      </details>
                    </article>
                  ))}
                  </div>
                </section>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  )
}
