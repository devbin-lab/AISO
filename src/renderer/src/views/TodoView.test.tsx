import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import TodoView from './TodoView'

const backend = { state: 'ready' as const, port: 8123 }
const todo = {
  id: 'todo-1',
  title: '핵심 전투 시스템 구현',
  priority: 'high' as const,
  dueDate: '2026-08-23',
  dueTime: '14:30',
  status: 'open' as const,
  createdAt: '2026-08-10T01:30:00.000Z',
  updatedAt: '2026-08-10T01:30:00.000Z',
  workspace: 'D:\\work'
}

const dispatchPointer = (target: Element | Document, type: 'pointerdown' | 'pointermove' | 'pointerup', init: { pointerId: number, button?: number, clientX: number, clientY: number }): void => {
  const event = new Event(type, { bubbles: true, cancelable: true })
  Object.defineProperties(event, {
    pointerId: { value: init.pointerId },
    button: { value: init.button ?? 0 },
    clientX: { value: init.clientX },
    clientY: { value: init.clientY }
  })
  fireEvent(target, event)
}

describe('TodoView', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('shows Agent-saved work in its due-date calendar and persists a completion update', async () => {
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      if (input.endsWith('/creator/todos')) return new Response(JSON.stringify({ items: [todo] }), { status: 200 })
      if (input.endsWith('/creator/todos/todo-1') && init?.method === 'PATCH') {
        return new Response(JSON.stringify({ item: { ...todo, status: 'done' } }), { status: 200 })
      }
      throw new Error(`unexpected endpoint: ${input}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<TodoView active backend={backend} />)

    expect(await screen.findByText('핵심 전투 시스템 구현', { selector: '.todo-row__content b' })).toBeTruthy()
    expect(screen.getByText('14:30', { selector: 'time' })).toBeTruthy()
    expect(screen.getByRole('button', { name: /2026년 8월 23일.*일정 1개/ })).toBeTruthy()
    expect(screen.getByRole('region', { name: '오늘의 작업' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /통계/ })).toBeNull()
    expect(screen.queryByRole('region', { name: '캘린더 현황' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '핵심 전투 시스템 구현 완료 처리' }))
    await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
      expect.stringContaining('/creator/todos/todo-1'),
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ status: 'done' }) })
    ))
  })

  it('switches to a yearly calendar from the year-month title and returns to a selected month', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ items: [todo] }), { status: 200 })))

    render(<TodoView active backend={backend} />)

    const yearToggle = await screen.findByRole('button', { name: /연간 보기$/ })
    fireEvent.click(yearToggle)
    const august = screen.getByRole('button', { name: /2026년 8월 일정 1개/ })
    fireEvent.click(august)

    expect(await screen.findByRole('button', { name: /2026년 연간 보기/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /2026년 8월 23일.*일정 1개/ })).toBeTruthy()
  })

  it('draws a multi-day task as one continuous bar in its calendar week', async () => {
    const ranged = { ...todo, startDate: '2026-08-23', endDate: '2026-08-26', dueDate: '2026-08-26' }
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ items: [ranged] }), { status: 200 })))

    render(<TodoView active backend={backend} />)

    await waitFor(() => expect(document.querySelectorAll('.todo-calendar__range-bar')).toHaveLength(1))
    const bar = document.querySelector('.todo-calendar__range-bar') as HTMLElement
    expect(bar.style.gridColumn).toBe('1 / 5')
    expect(bar.className).toContain('is-range-start')
    expect(bar.className).toContain('is-range-end')
  })

  it('draws a multi-day task as one continuous bar in the weekly view', async () => {
    const weekStart = new Date()
    weekStart.setHours(0, 0, 0, 0)
    weekStart.setDate(weekStart.getDate() - weekStart.getDay())
    const toKey = (value: Date): string => `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`
    const ranged = {
      ...todo,
      startDate: toKey(new Date(weekStart.getFullYear(), weekStart.getMonth(), weekStart.getDate() + 3)),
      endDate: toKey(new Date(weekStart.getFullYear(), weekStart.getMonth(), weekStart.getDate() + 5)),
      dueDate: toKey(new Date(weekStart.getFullYear(), weekStart.getMonth(), weekStart.getDate() + 5))
    }
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ items: [ranged] }), { status: 200 })))

    render(<TodoView active backend={backend} />)

    fireEvent.click(await screen.findByRole('tab', { name: '주간' }))
    await waitFor(() => expect(document.querySelectorAll('.todo-week__range-layer .todo-calendar__range-bar')).toHaveLength(1))
    const bar = document.querySelector('.todo-week__range-layer .todo-calendar__range-bar') as HTMLElement
    expect(bar.style.gridColumn).toBe('4 / 7')
    expect(document.querySelectorAll('.todo-week__surface .todo-task-chip')).toHaveLength(0)
  })

  it('expands a persisted weekly Aiso calendar event without exposing one-off drag handles', async () => {
    const recurring = {
      ...todo,
      id: 'weekly-shift',
      title: '알바',
      startDate: '2026-08-16',
      endDate: '2026-08-16',
      dueDate: '2026-08-16',
      dueTime: '10:00',
      endTime: '20:30',
      estimatedMinutes: 630,
      recurrence: { frequency: 'weekly' as const, weekdays: [0] },
      workspace: 'Aiso Calendar'
    }
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ items: [recurring] }), { status: 200 })))

    render(<TodoView active backend={backend} />)

    const augustSixteenth = await screen.findByRole('button', { name: /2026년 8월 16일.*일정 1개/ })
    fireEvent.click(augustSixteenth)
    expect(await screen.findByText('10:00–20:30 · 매주 일', { selector: 'time' })).toBeTruthy()
    const bars = Array.from(document.querySelectorAll<HTMLElement>('.todo-calendar__range-bar'))
    expect(bars).toHaveLength(3)
    expect(bars.every((bar) => bar.draggable === false)).toBe(true)
    expect((document.querySelector('.todo-row__check') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByTitle('시작일을 다른 날짜로 끌어 놓기')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /연간 보기$/ }))
    expect(await screen.findByRole('button', { name: /2026년 9월 일정 1개/ })).toBeTruthy()
  })

  it('edits an undated ToDo due date from its context menu', async () => {
    const undated = { ...todo, id: 'todo-undated', dueDate: null, dueTime: null }
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      if (input.endsWith('/creator/todos')) return new Response(JSON.stringify({ items: [undated] }), { status: 200 })
      if (input.endsWith('/creator/todos/todo-undated') && init?.method === 'PATCH') {
        return new Response(JSON.stringify({ item: { ...undated, dueDate: '2026-09-01' } }), { status: 200 })
      }
      throw new Error(`unexpected endpoint: ${input}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<TodoView active backend={backend} />)

    const title = await screen.findByText('핵심 전투 시스템 구현', { selector: '.todo-row__content b' })
    fireEvent.contextMenu(title.closest('.todo-row')!)
    fireEvent.click(screen.getByRole('menuitem', { name: '기한 지정' }))
    fireEvent.change(screen.getByLabelText('기한 날짜'), { target: { value: '2026-09-01' } })
    fireEvent.click(screen.getByRole('button', { name: '저장' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/creator/todos/todo-undated'),
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ dueDate: '2026-09-01', startDate: '2026-09-01', endDate: '2026-09-01' })
      })
    ))
  })

  it('deletes a calendar task only after the custom confirmation panel is accepted', async () => {
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      if (input.endsWith('/creator/todos')) return new Response(JSON.stringify({ items: [todo] }), { status: 200 })
      if (input.endsWith('/creator/todos/todo-1') && init?.method === 'DELETE') {
        return new Response(JSON.stringify({ id: 'todo-1' }), { status: 200 })
      }
      throw new Error(`unexpected endpoint: ${input}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<TodoView active backend={backend} />)

    const calendarTask = await screen.findByTitle(/핵심 전투 시스템 구현 · P1 높음/)
    fireEvent.contextMenu(calendarTask)
    fireEvent.click(screen.getByRole('menuitem', { name: '삭제' }))
    const confirmation = screen.getByRole('alertdialog', { name: '일정 삭제 확인' })
    expect(confirmation).toBeTruthy()
    expect(confirmation.textContent).toContain('이 작업은 되돌릴 수 없습니다.')

    fireEvent.click(screen.getByRole('button', { name: '삭제하기' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/creator/todos/todo-1'),
      expect.objectContaining({ method: 'DELETE' })
    ))
    await waitFor(() => expect(screen.queryByTitle(/핵심 전투 시스템 구현 · P1 높음/)).toBeNull())
  })

  it('loads saved ToDos without a current workspace', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ items: [] }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    render(<TodoView active backend={backend} />)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8123/creator/todos',
      expect.objectContaining({ headers: expect.anything() })
    ))
  })

  it('switches planner views and exposes P1–P3 priority controls from a task context menu', async () => {
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      if (input.endsWith('/creator/todos')) return new Response(JSON.stringify({ items: [todo] }), { status: 200 })
      if (input.endsWith('/creator/todos/todo-1') && init?.method === 'PATCH') {
        return new Response(JSON.stringify({ item: { ...todo, priority: 'low' } }), { status: 200 })
      }
      throw new Error(`unexpected endpoint: ${input}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<TodoView active backend={backend} />)

    await screen.findByText('핵심 전투 시스템 구현', { selector: '.todo-row__content b' })
    fireEvent.click(screen.getByRole('tab', { name: '목록' }))
    expect(await screen.findByRole('heading', { name: '모든 일정' })).toBeTruthy()

    const title = screen.getByText('핵심 전투 시스템 구현', { selector: '.todo-row__content b' })
    fireEvent.contextMenu(title.closest('.todo-row')!)
    expect(screen.getByLabelText('우선순위 변경')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'P3' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/creator/todos/todo-1'),
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ priority: 'low' }) })
    ))
  })

  it('moves a task range and resizes its end date through calendar drag controls', async () => {
    const ranged = { ...todo, startDate: '2026-08-23', endDate: '2026-08-24', dueDate: '2026-08-24', estimatedMinutes: 120 }
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      if (input.endsWith('/creator/todos')) return new Response(JSON.stringify({ items: [ranged] }), { status: 200 })
      if (input.endsWith('/creator/todos/todo-1') && init?.method === 'PATCH') {
        return new Response(JSON.stringify({ item: { ...ranged, endDate: '2026-08-26', dueDate: '2026-08-26' } }), { status: 200 })
      }
      throw new Error(`unexpected endpoint: ${input}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const stored = new Map<string, string>()
    const dataTransfer = {
      effectAllowed: '',
      setData: (type: string, value: string) => stored.set(type, value),
      getData: (type: string) => stored.get(type) ?? ''
    }

    render(<TodoView active backend={backend} />)

    const endHandle = await screen.findByTitle('종료일을 다른 날짜로 끌어 놓기')
    fireEvent.dragStart(endHandle, { dataTransfer })
    expect(stored.get('application/x-aiso-todo')).toContain('"kind":"end"')
    // While dragging, a transparent date grid sits above range bars. It keeps
    // the bar continuous while still exposing every exact calendar day.
    const target = await waitFor(() => {
      const element = document.querySelector<HTMLElement>('.todo-calendar__drop-target[data-todo-date="2026-08-26"]')
      expect(element).toBeTruthy()
      return element!
    })
    fireEvent.dragOver(target, { dataTransfer })
    fireEvent.drop(target, { dataTransfer })

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/creator/todos/todo-1'),
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ startDate: '2026-08-23', endDate: '2026-08-26', dueDate: '2026-08-26' })
      })
    ))
  })

  it('moves a listed ToDo onto a calendar day even when Electron loses the custom drag payload', async () => {
    const ranged = { ...todo, startDate: '2026-08-23', endDate: '2026-08-24', dueDate: '2026-08-24', estimatedMinutes: 120 }
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      if (input.endsWith('/creator/todos')) return new Response(JSON.stringify({ items: [ranged] }), { status: 200 })
      if (input.endsWith('/creator/todos/todo-1') && init?.method === 'PATCH') {
        return new Response(JSON.stringify({ item: { ...ranged, startDate: '2026-08-26', endDate: '2026-08-27', dueDate: '2026-08-27' } }), { status: 200 })
      }
      throw new Error(`unexpected endpoint: ${input}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const dataTransfer = {
      effectAllowed: '',
      setData: vi.fn(),
      getData: () => ''
    }

    render(<TodoView active backend={backend} />)

    const listedRow = await waitFor(() => {
      const row = document.querySelector<HTMLElement>('.todo-row[draggable="true"]')
      expect(row).toBeTruthy()
      return row!
    })
    const targetCell = document.querySelector<HTMLElement>('[data-todo-date="2026-08-26"]')
    const calendarWeek = targetCell?.closest<HTMLElement>('.todo-calendar__week')
    expect(calendarWeek).toBeTruthy()
    vi.spyOn(calendarWeek!, 'getBoundingClientRect').mockReturnValue({
      x: 0, y: 0, width: 700, height: 160, top: 0, right: 700, bottom: 160, left: 0, toJSON: () => ({})
    } as DOMRect)

    fireEvent.dragStart(listedRow, { dataTransfer })
    const dragOver = new Event('dragover', { bubbles: true, cancelable: true })
    Object.defineProperties(dragOver, {
      clientX: { value: 350 },
      dataTransfer: { value: dataTransfer }
    })
    const drop = new Event('drop', { bubbles: true, cancelable: true })
    Object.defineProperties(drop, {
      clientX: { value: 350 },
      dataTransfer: { value: dataTransfer }
    })
    fireEvent(calendarWeek!, dragOver)
    fireEvent(calendarWeek!, drop)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/creator/todos/todo-1'),
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ startDate: '2026-08-26', endDate: '2026-08-27', dueDate: '2026-08-27' })
      })
    ))
  })

  it('moves a one-day calendar bar with a pointer gesture', async () => {
    const oneDay = { ...todo, startDate: '2026-08-14', endDate: '2026-08-14', dueDate: '2026-08-14' }
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      if (input.endsWith('/creator/todos')) return new Response(JSON.stringify({ items: [oneDay] }), { status: 200 })
      if (input.endsWith('/creator/todos/todo-1') && init?.method === 'PATCH') {
        return new Response(JSON.stringify({ item: { ...oneDay, startDate: '2026-08-16', endDate: '2026-08-16', dueDate: '2026-08-16' } }), { status: 200 })
      }
      throw new Error(`unexpected endpoint: ${input}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<TodoView active backend={backend} />)

    const bar = await waitFor(() => {
      const element = document.querySelector<HTMLElement>('.todo-calendar__range-bar')
      expect(element).toBeTruthy()
      return element!
    })
    const target = document.querySelector<HTMLElement>('[data-todo-date="2026-08-16"]')
    expect(target).toBeTruthy()
    dispatchPointer(bar, 'pointerdown', { pointerId: 7, clientX: 100, clientY: 100 })
    dispatchPointer(target!, 'pointermove', { pointerId: 7, clientX: 110, clientY: 100 })
    await waitFor(() => {
      const preview = document.querySelector<HTMLElement>('.todo-calendar__range-bar.is-drag-preview')
      expect(preview).toBeTruthy()
      expect(preview?.getAttribute('aria-label')).toContain('일정 이동')
      expect(bar.classList.contains('is-drag-source')).toBe(true)
    })
    dispatchPointer(target!, 'pointerup', { pointerId: 7, clientX: 110, clientY: 100 })

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/creator/todos/todo-1'),
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ startDate: '2026-08-16', endDate: '2026-08-16', dueDate: '2026-08-16' })
      })
    ))
  })

  it('extends a one-day task from its end handle with a pointer gesture', async () => {
    const oneDay = { ...todo, startDate: '2026-08-14', endDate: '2026-08-14', dueDate: '2026-08-14' }
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      if (input.endsWith('/creator/todos')) return new Response(JSON.stringify({ items: [oneDay] }), { status: 200 })
      if (input.endsWith('/creator/todos/todo-1') && init?.method === 'PATCH') {
        return new Response(JSON.stringify({ item: { ...oneDay, endDate: '2026-08-18', dueDate: '2026-08-18' } }), { status: 200 })
      }
      throw new Error(`unexpected endpoint: ${input}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<TodoView active backend={backend} />)

    const endHandle = await screen.findByTitle('종료일을 다른 날짜로 끌어 놓기')
    const sourceBar = endHandle.closest<HTMLElement>('.todo-calendar__range-bar')
    const target = document.querySelector<HTMLElement>('[data-todo-date="2026-08-18"]')
    expect(target).toBeTruthy()
    dispatchPointer(endHandle, 'pointerdown', { pointerId: 8, clientX: 100, clientY: 100 })
    dispatchPointer(target!, 'pointermove', { pointerId: 8, clientX: 110, clientY: 100 })
    await waitFor(() => {
      const preview = document.querySelector<HTMLElement>('.todo-calendar__range-bar.is-drag-preview')
      expect(preview).toBeTruthy()
      expect(preview?.getAttribute('aria-label')).toContain('종료일 변경')
      expect(sourceBar?.classList.contains('is-drag-source')).toBe(true)
    })
    dispatchPointer(target!, 'pointerup', { pointerId: 8, clientX: 110, clientY: 100 })

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/creator/todos/todo-1'),
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ startDate: '2026-08-14', endDate: '2026-08-18', dueDate: '2026-08-18' })
      })
    ))
  })

  it('shows a missed-work redistribution before explicitly applying it', async () => {
    const overdue = { ...todo, dueDate: '2020-01-01', startDate: '2020-01-01', endDate: '2020-01-01', estimatedMinutes: 120 }
    const proposal = {
      asOf: '2026-08-18',
      dailyCapacityMinutes: 120,
      totalMinutes: 120,
      unallocatedMinutes: 0,
      plans: [{
        todoId: 'todo-1', title: overdue.title, priority: 'high', totalMinutes: 120, unallocatedMinutes: 0,
        assignments: [{ date: '2026-08-19', minutes: 60 }, { date: '2026-08-20', minutes: 60 }]
      }]
    }
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      if (input.endsWith('/creator/todos')) return new Response(JSON.stringify({ items: [overdue] }), { status: 200 })
      if (input.endsWith('/creator/todos/replan-preview') && init?.method === 'POST') {
        return new Response(JSON.stringify(proposal), { status: 200 })
      }
      if (input.endsWith('/creator/todos/replan-apply') && init?.method === 'POST') {
        return new Response(JSON.stringify({ items: [{ ...overdue, startDate: '2026-08-19', endDate: '2026-08-20', dueDate: '2026-08-20' }], proposal }), { status: 200 })
      }
      throw new Error(`unexpected endpoint: ${input}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<TodoView active backend={backend} />)

    const previewButton = await screen.findByRole('button', { name: '제안 보기' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    fireEvent.click(previewButton)
    expect(await screen.findByRole('dialog', { name: '미완료 작업 일정 제안' })).toBeTruthy()
    expect(screen.getByText('2026년 8월 19일 수 1시간 · 2026년 8월 20일 목 1시간')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: '제안 적용' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/creator/todos/replan-apply'),
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ asOf: '2026-08-18' }) })
    ))
  })

  it('creates a precise recurring planner task without a workspace', async () => {
    const created = {
      ...todo, id: 'todo-new', title: '주간 빌드 검토', startDate: '2026-08-17', endDate: '2026-08-17',
      dueDate: '2026-08-17', dueTime: '13:30', endTime: '15:00', estimatedMinutes: 90,
      recurrence: { frequency: 'weekly', weekdays: [1] }
    }
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      if (input.endsWith('/creator/todos') && !init?.method) return new Response(JSON.stringify({ items: [] }), { status: 200 })
      if (input.endsWith('/creator/todos') && init?.method === 'POST') return new Response(JSON.stringify({ item: created }), { status: 200 })
      throw new Error(`unexpected endpoint: ${input}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<TodoView active backend={backend} />)
    fireEvent.click(await screen.findByRole('button', { name: '＋ 새 작업' }))
    const dialog = screen.getByRole('dialog', { name: '새 작업 등록' })
    expect(dialog.querySelectorAll('input[type="date"], input[type="time"], select')).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('작업 이름', { selector: 'input' }), { target: { value: '주간 빌드 검토' } })
    fireEvent.click(screen.getByRole('button', { name: '시작일' }))
    fireEvent.click(screen.getByRole('button', { name: /2026년 8월 17일.*선택/ }))
    fireEvent.click(screen.getByRole('button', { name: '종료일' }))
    fireEvent.click(screen.getByRole('button', { name: /2026년 8월 17일.*선택/ }))
    fireEvent.click(screen.getByRole('button', { name: '시작 시각' }))
    fireEvent.click(screen.getByRole('option', { name: '13:30' }))
    fireEvent.click(screen.getByRole('button', { name: '종료 시각' }))
    fireEvent.click(screen.getByRole('option', { name: '15:00' }))
    fireEvent.click(screen.getByRole('button', { name: '반복' }))
    fireEvent.click(screen.getByRole('option', { name: '매주' }))
    fireEvent.click(screen.getByRole('button', { name: '작업 등록' }))

    await waitFor(() => expect(dialog.isConnected).toBe(false))
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(String(post?.[1]?.body))).toEqual(expect.objectContaining({
      title: '주간 빌드 검토', startDate: '2026-08-17', dueTime: '13:30', endTime: '15:00',
      recurrence: { frequency: 'weekly', weekdays: [1] }
    }))
  })

  it('shows day and week views on an hourly axis', async () => {
    const timed = { ...todo, startDate: todo.dueDate, endDate: todo.dueDate, endTime: '15:30', estimatedMinutes: 60 }
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ items: [timed] }), { status: 200 })))
    render(<TodoView active backend={backend} />)

    fireEvent.click(await screen.findByRole('tab', { name: '일간' }))
    expect(document.querySelectorAll('.todo-time-planner__hour-line')).toHaveLength(24)
    const dayEvent = screen.getByRole('button', { name: /핵심 전투 시스템 구현/ }) as HTMLElement
    expect(dayEvent.style.top).toBe(`${14.5 * 56}px`)
    expect(dayEvent.style.height).toBe(`${56}px`)

    fireEvent.click(screen.getByRole('tab', { name: '주간' }))
    expect(document.querySelectorAll('.todo-time-planner__column')).toHaveLength(7)
    expect(document.querySelectorAll('.todo-time-planner__hour-line')).toHaveLength(168)
  })
})
