import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AgentProject, ConversationMeta } from '../../../shared/conversation'
import Sidebar from './Sidebar'

const child: ConversationMeta = {
  id: 'agent-child-1',
  kind: 'agent',
  title: '프로젝트 구조 파악',
  createdAt: 1,
  updatedAt: 2,
  pinned: false
}

const standalone: ConversationMeta = {
  id: 'agent-standalone-1',
  kind: 'agent',
  title: '폴더 없이 시작한 에이전트 대화',
  createdAt: 3,
  updatedAt: 4,
  pinned: false
}

const project: AgentProject = {
  id: 'project-1',
  title: 'AISO',
  createdAt: 1,
  updatedAt: 2,
  conversationId: child.id,
  conversations: [child]
}

function installApi(createConversation = vi.fn().mockResolvedValue(child)): void {
  Object.defineProperty(window, 'api', {
    configurable: true,
    value: {
      conversations: {
        list: vi.fn().mockImplementation((kind: string) => Promise.resolve(kind === 'agent' ? [child, standalone] : [])),
        setPinned: vi.fn().mockResolvedValue(null),
        rename: vi.fn().mockResolvedValue(null),
        remove: vi.fn().mockResolvedValue(undefined)
      },
      projects: {
        list: vi.fn().mockResolvedValue([project]),
        create: vi.fn(),
        createConversation,
        start: vi.fn()
      }
    }
  })
}

afterEach(() => {
  delete (window as unknown as { api?: unknown }).api
})

describe('Sidebar projects', () => {
  it('labels the two top-level modes as Aiso chat and Aiso agent', async () => {
    const user = userEvent.setup()
    installApi()
    render(
      <Sidebar
        view="agent"
        activeConversation={null}
        onNavigate={vi.fn()}
        onNewConversation={vi.fn()}
        onOpenConversation={vi.fn()}
      />
    )

    expect(await screen.findByText('Aiso 에이전트')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: '작업 모드' }))
    expect(screen.getByRole('option', { name: 'Aiso 채팅' })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'Aiso 에이전트' })).toBeTruthy()
  })

  it('creates a standalone Agent chat from the button below new project', async () => {
    const user = userEvent.setup()
    const onNewConversation = vi.fn()
    installApi()
    render(
      <Sidebar
        view="agent"
        activeConversation={null}
        onNavigate={vi.fn()}
        onNewConversation={onNewConversation}
        onOpenConversation={vi.fn()}
      />
    )

    await user.click(await screen.findByRole('button', { name: '새 채팅' }))
    expect(onNewConversation).toHaveBeenCalledWith('agent')
    expect(onNewConversation).toHaveBeenCalledTimes(1)
  })

  it('renders project Agent chats below their project and keeps standalone Agent chats in recent', async () => {
    installApi()
    render(
      <Sidebar
        view="agent"
        activeConversation={null}
        onNavigate={vi.fn()}
        onNewConversation={vi.fn()}
        onOpenConversation={vi.fn()}
      />
    )

    const projectSection = await screen.findByLabelText('프로젝트')
    const recentSection = screen.getByLabelText('최근 대화')

    expect(within(projectSection).getByText('AISO')).toBeTruthy()
    expect(within(projectSection).getByText('프로젝트 구조 파악')).toBeTruthy()
    expect(within(recentSection).getByText('폴더 없이 시작한 에이전트 대화')).toBeTruthy()
    expect(within(recentSection).queryByText('프로젝트 구조 파악')).toBeNull()
  })

  it('creates and opens a child Agent chat from the project header', async () => {
    const user = userEvent.setup()
    const createConversation = vi.fn().mockResolvedValue(child)
    const onOpenConversation = vi.fn()
    installApi(createConversation)
    render(
      <Sidebar
        view="agent"
        activeConversation={null}
        onNavigate={vi.fn()}
        onNewConversation={vi.fn()}
        onOpenConversation={onOpenConversation}
      />
    )

    await user.click(await screen.findByRole('button', { name: 'AISO에 새 에이전트 대화 추가' }))
    await waitFor(() => expect(createConversation).toHaveBeenCalledWith('project-1'))
    expect(onOpenConversation).toHaveBeenCalledWith('agent', child.id)
  })
})
