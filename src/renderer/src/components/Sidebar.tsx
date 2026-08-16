import { useEffect, useMemo, useState } from 'react'
import type { AgentProject, ConversationKind, ConversationMeta } from '../../../shared/conversation'
import { relTime } from '../lib/conversations'
import {
  AgentIcon,
  ChatIcon,
  ComfyIcon,
  EditIcon,
  FolderIcon,
  GraphIcon,
  PinIcon,
  SlidersIcon,
  TodoIcon,
  TrashIcon
} from './icons'
import Dropdown, { type DropdownOption } from './Dropdown'
import { confirmDialog } from './ConfirmDialog'

export type ViewKey = 'home' | 'chat' | 'agent' | 'todo' | 'graph' | 'comfy' | 'settings'

export interface ConversationRequest {
  kind: ConversationKind
  id: string | null
  nonce: number
}

interface Props {
  view: ViewKey
  onNavigate: (view: ViewKey) => void
  onNewConversation: (kind: ConversationKind) => void
  onOpenConversation: (kind: ConversationKind, id: string) => void
  activeConversation: ConversationRequest | null
  collapsed?: boolean
}

const SECONDARY_NAV: { key: ViewKey; label: string; Icon: typeof TodoIcon }[] = [
  { key: 'todo', label: '캘린더', Icon: TodoIcon },
  { key: 'graph', label: 'My DB', Icon: GraphIcon },
  { key: 'comfy', label: 'ComfyUI', Icon: ComfyIcon }
]

const MODE_OPTIONS: DropdownOption[] = [
  { value: 'chat', label: 'Aiso 채팅' },
  { value: 'agent', label: 'Aiso 에이전트' }
]

function Sidebar({
  view,
  onNavigate,
  onNewConversation,
  onOpenConversation,
  activeConversation,
  collapsed = false
}: Props): React.JSX.Element {
  const [mode, setMode] = useState<ConversationKind>(view === 'agent' ? 'agent' : 'chat')
  const [conversations, setConversations] = useState<ConversationMeta[]>([])
  const [projects, setProjects] = useState<AgentProject[]>([])
  const [creatingProject, setCreatingProject] = useState(false)
  const [projectTitle, setProjectTitle] = useState('')
  const [projectError, setProjectError] = useState<string | null>(null)
  const [creatingProjectConversationId, setCreatingProjectConversationId] = useState<string | null>(null)
  const [editingConversationId, setEditingConversationId] = useState<string | null>(null)
  const [conversationTitle, setConversationTitle] = useState('')

  const refresh = (): void => {
    Promise.all([window.api.conversations.list('chat'), window.api.conversations.list('agent')])
      .then(([chat, agent]) => setConversations([...chat, ...agent].sort((a, b) => b.updatedAt - a.updatedAt)))
      .catch(() => {})
    window.api.projects.list().then(setProjects).catch(() => {})
  }

  useEffect(() => {
    if (view === 'chat' || view === 'agent') setMode(view)
  }, [view])

  useEffect(() => {
    refresh()
    window.addEventListener('aiso:conversations-changed', refresh)
    return () => window.removeEventListener('aiso:conversations-changed', refresh)
  }, [])

  const projectConversationIds = useMemo(
    () => new Set(projects.flatMap((project) => project.conversations.map((conversation) => conversation.id))),
    [projects]
  )

  const standalone = useMemo(
    () => conversations.filter((conversation) =>
      conversation.kind === mode && (mode !== 'agent' || !projectConversationIds.has(conversation.id))
    ),
    [conversations, mode, projectConversationIds]
  )

  const recent = useMemo(
    () => standalone.filter((conversation) => !conversation.pinned),
    [standalone]
  )

  const pinned = useMemo(
    () => standalone.filter((conversation) => conversation.pinned),
    [standalone]
  )

  const modeLabel = mode === 'agent' ? '에이전트' : '채팅'
  const newLabel = mode === 'agent' ? '새 프로젝트' : '새 채팅'
  const ModeIcon = mode === 'agent' ? AgentIcon : ChatIcon
  const isActive = (conversation: ConversationMeta): boolean =>
    activeConversation?.id === conversation.id && activeConversation.kind === conversation.kind

  const changeMode = (next: ConversationKind): void => {
    setMode(next)
    onNavigate(next)
  }

  const beginNew = (): void => {
    if (mode === 'chat') {
      onNewConversation('chat')
      return
    }
    setProjectError(null)
    setProjectTitle('')
    setCreatingProject(true)
  }

  const beginStandaloneAgentConversation = (): void => {
    onNewConversation('agent')
  }

  const createProject = async (): Promise<void> => {
    const title = projectTitle.trim()
    if (!title) {
      setProjectError('프로젝트 이름을 입력해 주세요.')
      return
    }
    try {
      const project = await window.api.projects.create(title)
      const conversation = await window.api.projects.createConversation(project.id)
      setCreatingProject(false)
      setProjectTitle('')
      setProjectError(null)
      refresh()
      if (conversation) onOpenConversation('agent', conversation.id)
    } catch (error) {
      setProjectError(error instanceof Error ? error.message : '프로젝트를 만들지 못했습니다.')
    }
  }

  const createProjectConversation = async (project: AgentProject): Promise<void> => {
    setCreatingProjectConversationId(project.id)
    try {
      const conversation = await window.api.projects.createConversation(project.id)
      if (conversation) {
        onOpenConversation('agent', conversation.id)
        refresh()
      }
    } catch (error) {
      setProjectError(error instanceof Error ? error.message : '프로젝트 대화를 만들지 못했습니다.')
    } finally {
      setCreatingProjectConversationId(null)
    }
  }

  const startRenameConversation = (conversation: ConversationMeta): void => {
    setEditingConversationId(conversation.id)
    setConversationTitle(conversation.title)
  }

  const finishRenameConversation = async (): Promise<void> => {
    const id = editingConversationId
    const title = conversationTitle.trim()
    if (!id) return
    if (!title) {
      setEditingConversationId(null)
      return
    }
    try {
      const renamed = await window.api.conversations.rename(id, title)
      if (!renamed) return
      window.dispatchEvent(new CustomEvent('aiso:conversation-renamed', {
        detail: { id: renamed.id, title: renamed.title }
      }))
      setEditingConversationId(null)
      refresh()
    } catch {
      // 입력은 유지해 사용자가 바로 다시 고칠 수 있게 한다.
    }
  }

  const toggleConversationPin = async (conversation: ConversationMeta): Promise<void> => {
    await window.api.conversations.setPinned(conversation.id, !conversation.pinned)
    refresh()
  }

  const deleteConversation = async (conversation: ConversationMeta): Promise<void> => {
    const confirmed = await confirmDialog({
      title: '대화 삭제',
      message: `'${conversation.title}' 대화를 삭제할까요?`,
      confirmLabel: '삭제',
      danger: true
    })
    if (!confirmed) return
    await window.api.conversations.remove(conversation.id)
    if (isActive(conversation)) onNewConversation(conversation.kind)
    refresh()
  }

  const renderConversation = (conversation: ConversationMeta): React.JSX.Element => {
    const editing = editingConversationId === conversation.id
    return (
      <div
        className={`sidebar__conversation-row ${isActive(conversation) ? 'sidebar__conversation-row--active' : ''}`}
        key={conversation.id}
      >
        {editing ? (
          <input
            className="sidebar__conversation-edit"
            autoFocus
            value={conversationTitle}
            aria-label="대화 이름"
            onChange={(event) => setConversationTitle(event.target.value)}
            onBlur={() => void finishRenameConversation()}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                event.currentTarget.blur()
              } else if (event.key === 'Escape') {
                setEditingConversationId(null)
              }
            }}
          />
        ) : (
          <button
            type="button"
            className="sidebar__conversation"
            onClick={() => onOpenConversation(conversation.kind, conversation.id)}
            onDoubleClick={() => startRenameConversation(conversation)}
            title={conversation.title}
          >
            <span className="sidebar__conversation-title">
              {conversation.pinned && <span className="sidebar__conversation-pin" aria-label="고정됨"><PinIcon size={11} filled /></span>}
              {conversation.title}
            </span>
            <span className="sidebar__conversation-meta">{relTime(conversation.updatedAt)}</span>
          </button>
        )}
        {!editing && (
          <div className="sidebar__conversation-actions" aria-label={`${conversation.title} 관리`}>
            <button
              type="button"
              className={`sidebar__conversation-action ${conversation.pinned ? 'sidebar__conversation-action--on' : ''}`}
              aria-label={conversation.pinned ? '고정 해제' : '고정'}
              title={conversation.pinned ? '고정 해제' : '고정'}
              onClick={() => void toggleConversationPin(conversation)}
            >
              <PinIcon size={13} filled={conversation.pinned} />
            </button>
            <button
              type="button"
              className="sidebar__conversation-action"
              aria-label="이름 변경"
              title="이름 변경"
              onClick={() => startRenameConversation(conversation)}
            >
              <EditIcon size={13} />
            </button>
            <button
              type="button"
              className="sidebar__conversation-action sidebar__conversation-action--delete"
              aria-label="대화 삭제"
              title="대화 삭제"
              onClick={() => void deleteConversation(conversation)}
            >
              <TrashIcon size={13} />
            </button>
          </div>
        )}
      </div>
    )
  }

  return (
    <aside className={`sidebar ${collapsed ? 'sidebar--collapsed' : ''}`} aria-label="주 탐색">
      <div className="sidebar__scroll">
        <section className="sidebar__top" aria-label="작업 모드">
          <div className="sidebar__mode">
            <Dropdown
              value={mode}
              options={MODE_OPTIONS}
              onChange={(value) => changeMode(value as ConversationKind)}
              title="작업 모드"
            />
          </div>
          <button type="button" className="sidebar__new" onClick={beginNew}>
            <ModeIcon size={16} />
            <span>{newLabel}</span>
          </button>
          {mode === 'agent' && (
            <button type="button" className="sidebar__new" onClick={beginStandaloneAgentConversation}>
              <ChatIcon size={16} />
              <span>새 채팅</span>
            </button>
          )}
        </section>

        <section className="sidebar__section sidebar__section--tools" aria-label="도구">
          <div className="sidebar__section-title">도구</div>
          {SECONDARY_NAV.map(({ key, label, Icon }) => (
            <button
              key={key}
              type="button"
              className={`sidebar__nav ${view === key ? 'sidebar__nav--active' : ''}`}
              onClick={() => onNavigate(key)}
            >
              <Icon size={16} />
              <span>{label}</span>
            </button>
          ))}
        </section>

        {mode === 'agent' && (
          <section className="sidebar__section" aria-label="프로젝트">
            <div className="sidebar__section-title">프로젝트</div>
            {creatingProject && (
              <form
                className="sidebar__project-form"
                onSubmit={(event) => {
                  event.preventDefault()
                  void createProject()
                }}
              >
                <input
                  autoFocus
                  value={projectTitle}
                  placeholder="프로젝트 이름"
                  aria-label="프로젝트 이름"
                  onChange={(event) => {
                    setProjectTitle(event.target.value)
                    setProjectError(null)
                  }}
                />
                {projectError && <p>{projectError}</p>}
                <div>
                  <button type="button" onClick={() => setCreatingProject(false)}>취소</button>
                  <button type="submit">등록</button>
                </div>
              </form>
            )}
            {projects.length > 0 && (
              <div className="sidebar__project-items">
                {projects.map((project) => (
                  <div className="sidebar__project-group" key={project.id}>
                    <div className="sidebar__project-heading">
                      <div className="sidebar__project-title" title={project.title}>
                        <FolderIcon size={15} />
                        <span>{project.title}</span>
                      </div>
                      <button
                        type="button"
                        className="sidebar__project-add"
                        aria-label={`${project.title}에 새 에이전트 대화 추가`}
                        title="새 에이전트 대화 추가"
                        disabled={creatingProjectConversationId === project.id}
                        onClick={() => void createProjectConversation(project)}
                      >
                        +
                      </button>
                    </div>
                    <div className="sidebar__project-conversations">
                      {project.conversations.map(renderConversation)}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>
        )}

        {pinned.length > 0 && (
          <section className="sidebar__section sidebar__section--pinned" aria-label="고정한 대화">
            <div className="sidebar__section-title">고정</div>
            {pinned.map(renderConversation)}
          </section>
        )}

        <section className="sidebar__section sidebar__section--recent" aria-label="최근 대화">
          <div className="sidebar__section-title">최근</div>
          {recent.length === 0 ? (
            <p className="sidebar__empty">아직 {modeLabel} 최근 대화가 없습니다.</p>
          ) : (
            recent.map(renderConversation)
          )}
        </section>

      </div>

      <div className="sidebar__bottom">
        <button
          type="button"
          className={`sidebar__nav ${view === 'settings' ? 'sidebar__nav--active' : ''}`}
          onClick={() => onNavigate('settings')}
        >
          <SlidersIcon size={16} />
          <span>설정</span>
        </button>
      </div>
    </aside>
  )
}

export default Sidebar
