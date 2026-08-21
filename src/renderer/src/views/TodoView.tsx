import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type DragEvent as ReactDragEvent, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react'
import type { BackendInfo } from '../../../shared/backend'
import { authHeaders } from '../lib/backend'
import { RefreshIcon } from '../components/icons'
import Dropdown, { type DropdownOption } from '../components/Dropdown'

type Priority = 'high' | 'medium' | 'low'
type TodoStatus = 'open' | 'done'
type TodoFilter = 'all' | TodoStatus
type PlannerView = 'today' | 'day' | 'week' | 'month' | 'list'
type CalendarMode = 'month' | 'year'
type CalendarMotion = 'backward' | 'forward' | null
type ContextEditor = 'actions' | 'due' | 'rename' | 'plan' | 'delete'
type DragKind = 'move' | 'start' | 'end'
type RecurrenceMode = 'none' | TodoRecurrence['frequency']
type TodoRecurrence =
  | { frequency: 'daily' }
  | { frequency: 'weekly'; weekdays: number[] }
  | { frequency: 'monthly'; day: number }
  | { frequency: 'yearly'; month: number; day: number }

interface TodoItem {
  id: string
  title: string
  priority: Priority
  dueDate: string | null
  dueTime?: string | null
  endTime?: string | null
  startDate?: string | null
  endDate?: string | null
  estimatedMinutes?: number | null
  recurrence?: TodoRecurrence | null
  scheduleBlocks?: Array<{ date: string; minutes: number }>
  status: TodoStatus
  createdAt: string
  updatedAt: string
  workspace?: string
}

interface ReplanAssignment {
  date: string
  minutes: number
}

interface ReplanPlan {
  todoId: string
  title: string
  priority: Priority
  totalMinutes: number
  assignments: ReplanAssignment[]
  unallocatedMinutes: number
}

interface ReplanPreview {
  asOf: string
  dailyCapacityMinutes: number
  plans: ReplanPlan[]
  totalMinutes: number
  unallocatedMinutes: number
}

interface TodoContextMenu {
  item: TodoItem
  x: number
  y: number
  editor: ContextEditor
}

interface DragPayload {
  id: string
  kind: DragKind
}

interface PointerDragState {
  payload: DragPayload
  pointerId: number
  originX: number
  originY: number
}

interface DragSchedulePreview {
  item: TodoItem
  startDate: string
  endDate: string
}

interface TodoEditorDraft {
  title: string
  priority: Priority
  startDate: string
  endDate: string
  startTime: string
  endTime: string
  estimatedMinutes: string
  recurrenceMode: RecurrenceMode
  weekdays: number[]
}

interface CalendarRangeBar {
  item: TodoItem
  startColumn: number
  endColumn: number
  lane: number
  startsAtRangeStart: boolean
  endsAtRangeEnd: boolean
}

interface CalendarWeekLayout {
  cells: Array<Date | null>
  bars: CalendarRangeBar[]
  laneCount: number
}

interface Props {
  active: boolean
  backend: BackendInfo
}

const DAY_MS = 24 * 60 * 60 * 1000
const HOUR_HEIGHT = 56
const MINUTES_PER_DAY = 24 * 60
const TIME_OPTIONS: DropdownOption[] = Array.from({ length: 96 }, (_, index) => {
  const hour = Math.floor(index / 4)
  const minute = (index % 4) * 15
  const value = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
  return { value, label: value }
})
const PRIORITY_OPTIONS: DropdownOption[] = [
  { value: 'high', label: 'P1 · 높음' },
  { value: 'medium', label: 'P2 · 보통' },
  { value: 'low', label: 'P3 · 낮음' }
]
const RECURRENCE_OPTIONS: DropdownOption[] = [
  { value: 'none', label: '반복 없음' },
  { value: 'daily', label: '매일' },
  { value: 'weekly', label: '매주' },
  { value: 'monthly', label: '매월 같은 날짜' },
  { value: 'yearly', label: '매년 같은 날짜' }
]

function emptyTodoDraft(today: string): TodoEditorDraft {
  return {
    title: '', priority: 'medium', startDate: today, endDate: today,
    startTime: '09:00', endTime: '10:00', estimatedMinutes: '60',
    recurrenceMode: 'none', weekdays: [dateFromKey(today)?.getDay() ?? 1]
  }
}

function clockMinutes(value?: string | null): number | null {
  const match = /^(\d{2}):(\d{2})$/.exec(value ?? '')
  if (!match) return null
  const minutes = Number(match[1]) * 60 + Number(match[2])
  return minutes >= 0 && minutes < MINUTES_PER_DAY ? minutes : null
}

function clockFromMinutes(value: number): string {
  const checked = Math.max(0, Math.min(MINUTES_PER_DAY - 1, Math.round(value)))
  return `${String(Math.floor(checked / 60)).padStart(2, '0')}:${String(checked % 60).padStart(2, '0')}`
}

function recurrenceFromDraft(draft: TodoEditorDraft): TodoRecurrence | null {
  const anchor = dateFromKey(draft.startDate) ?? new Date()
  if (draft.recurrenceMode === 'daily') return { frequency: 'daily' }
  if (draft.recurrenceMode === 'weekly') return { frequency: 'weekly', weekdays: draft.weekdays.length ? [...draft.weekdays].sort() : [anchor.getDay()] }
  if (draft.recurrenceMode === 'monthly') return { frequency: 'monthly', day: anchor.getDate() }
  if (draft.recurrenceMode === 'yearly') return { frequency: 'yearly', month: anchor.getMonth() + 1, day: anchor.getDate() }
  return null
}

function endpoint(backend: BackendInfo, path: string): string | null {
  return backend.state === 'ready' && backend.port != null ? `http://127.0.0.1:${backend.port}${path}` : null
}

async function responseJson<T>(response: Response): Promise<T> {
  const body = await response.json().catch(() => ({})) as { detail?: string }
  if (!response.ok) throw new Error(body.detail || `요청 실패 (${response.status})`)
  return body as T
}

function dateKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

function dateFromKey(value: string): Date | null {
  const matched = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!matched) return null
  const date = new Date(Number(matched[1]), Number(matched[2]) - 1, Number(matched[3]))
  return Number.isNaN(date.getTime()) ? null : date
}

function addDays(value: string, delta: number): string {
  const date = dateFromKey(value) ?? new Date()
  date.setDate(date.getDate() + delta)
  return dateKey(date)
}

function daysBetween(start: string, end: string): number {
  const startDate = dateFromKey(start)
  const endDate = dateFromKey(end)
  if (!startDate || !endDate) return 0
  return Math.max(0, Math.round((endDate.getTime() - startDate.getTime()) / DAY_MS))
}

function startOfWeek(value: string): Date {
  const date = dateFromKey(value) ?? new Date()
  date.setDate(date.getDate() - date.getDay())
  return date
}

function dayLabel(value: string): string {
  const date = dateFromKey(value)
  return date
    ? new Intl.DateTimeFormat('ko-KR', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'short' }).format(date)
    : value
}

function monthLabel(date: Date): string {
  return new Intl.DateTimeFormat('ko-KR', { year: 'numeric', month: 'long' }).format(date)
}

function monthOnlyLabel(date: Date): string {
  return new Intl.DateTimeFormat('ko-KR', { month: 'long' }).format(date)
}

function dateFieldLabel(value: string): string {
  const date = dateFromKey(value)
  return date
    ? new Intl.DateTimeFormat('ko-KR', { year: 'numeric', month: 'long', day: 'numeric' }).format(date)
    : '날짜 선택'
}

interface PlannerDateFieldProps {
  label: string
  value: string
  min?: string
  onChange: (value: string) => void
}

function PlannerDateField({ label, value, min, onChange }: PlannerDateFieldProps): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const [cursor, setCursor] = useState(() => {
    const date = dateFromKey(value) ?? new Date()
    return new Date(date.getFullYear(), date.getMonth(), 1)
  })

  useEffect(() => {
    if (!open) {
      const date = dateFromKey(value) ?? new Date()
      setCursor(new Date(date.getFullYear(), date.getMonth(), 1))
    }
  }, [open, value])

  useEffect(() => {
    if (!open) return
    const closeOnOutsideClick = (event: MouseEvent): void => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', closeOnOutsideClick)
    return () => document.removeEventListener('mousedown', closeOnOutsideClick)
  }, [open])

  const firstDay = new Date(cursor.getFullYear(), cursor.getMonth(), 1)
  const gridStart = new Date(cursor.getFullYear(), cursor.getMonth(), 1 - firstDay.getDay())
  const days = Array.from({ length: 42 }, (_, index) => {
    const date = new Date(gridStart)
    date.setDate(gridStart.getDate() + index)
    return date
  })

  return (
    <div className="todo-editor__field" ref={rootRef}>
      <span>{label}</span>
      <button type="button" className="todo-editor__picker" aria-label={label} aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        <span>{dateFieldLabel(value)}</span><i aria-hidden="true">⌄</i>
      </button>
      {open && <div className="todo-date-picker" role="dialog" aria-label={`${label} 달력`}>
        <header><button type="button" aria-label="이전 달" onClick={() => setCursor((current) => new Date(current.getFullYear(), current.getMonth() - 1, 1))}>‹</button><b>{monthLabel(cursor)}</b><button type="button" aria-label="다음 달" onClick={() => setCursor((current) => new Date(current.getFullYear(), current.getMonth() + 1, 1))}>›</button></header>
        <div className="todo-date-picker__weekdays" aria-hidden="true">{['일', '월', '화', '수', '목', '금', '토'].map((weekday) => <span key={weekday}>{weekday}</span>)}</div>
        <div className="todo-date-picker__days">{days.map((date) => {
          const key = dateKey(date)
          const outside = date.getMonth() !== cursor.getMonth()
          const disabled = Boolean(min && key < min)
          return <button key={key} type="button" disabled={disabled} aria-label={`${dayLabel(key)} 선택`} aria-pressed={key === value} className={`${outside ? 'is-outside ' : ''}${key === value ? 'is-selected ' : ''}${key === dateKey(new Date()) ? 'is-today' : ''}`.trim()} onClick={() => { onChange(key); setOpen(false) }}>{date.getDate()}</button>
        })}</div>
        <footer><button type="button" onClick={() => { const today = dateKey(new Date()); if (!min || today >= min) { onChange(today); setOpen(false) } }}>오늘</button></footer>
      </div>}
    </div>
  )
}

function priorityCode(priority: Priority): string {
  return priority === 'high' ? 'P1' : priority === 'medium' ? 'P2' : 'P3'
}

function priorityLabel(priority: Priority): string {
  return priority === 'high' ? 'P1 높음' : priority === 'medium' ? 'P2 보통' : 'P3 낮음'
}

function priorityRank(priority: Priority): number {
  return priority === 'high' ? 0 : priority === 'medium' ? 1 : 2
}

function timeLabel(item: TodoItem): string {
  const range = item.dueTime ? `${item.dueTime}${item.endTime ? `–${item.endTime}` : ''}` : '시간 미정'
  return item.recurrence ? `${range} · ${recurrenceLabel(item.recurrence)}` : range
}

function recurrenceLabel(recurrence: TodoRecurrence): string {
  if (recurrence.frequency === 'daily') return '매일'
  if (recurrence.frequency === 'weekly') {
    const names = ['일', '월', '화', '수', '목', '금', '토']
    return `매주 ${recurrence.weekdays.map((day) => names[day] ?? '').filter(Boolean).join('·')}`
  }
  if (recurrence.frequency === 'monthly') return `매월 ${recurrence.day}일`
  return `매년 ${recurrence.month}월 ${recurrence.day}일`
}

function durationLabel(item: TodoItem): string {
  const minutes = item.estimatedMinutes ?? 30
  if (minutes < 60) return `${minutes}분`
  return `${Math.floor(minutes / 60)}시간${minutes % 60 ? ` ${minutes % 60}분` : ''}`
}

function minuteLabel(minutes: number): string {
  if (minutes < 60) return `${minutes}분`
  return `${Math.floor(minutes / 60)}시간${minutes % 60 ? ` ${minutes % 60}분` : ''}`
}

function createdTimeLabel(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '등록 시간 없음'
  return new Intl.DateTimeFormat('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}

function workspaceLabel(value?: string): string {
  if (!value) return 'Aiso 저장 작업'
  const parts = value.replace(/\\/g, '/').split('/').filter(Boolean)
  return parts.at(-1) || value
}

function scheduledStart(item: TodoItem): string | null {
  return item.startDate || item.endDate || item.dueDate || null
}

function scheduledEnd(item: TodoItem): string | null {
  return item.endDate || item.dueDate || item.startDate || null
}

function previewDragSchedule(item: TodoItem, payload: DragPayload, targetDate: string): DragSchedulePreview | null {
  const start = scheduledStart(item) ?? targetDate
  const end = scheduledEnd(item) ?? start
  let nextStart = start
  let nextEnd = end
  if (payload.kind === 'move') {
    nextStart = targetDate
    nextEnd = addDays(targetDate, daysBetween(start, end))
  } else if (payload.kind === 'start') {
    if (targetDate > end) return null
    nextStart = targetDate
  } else {
    if (targetDate < start) return null
    nextEnd = targetDate
  }
  return {
    item: { ...item, startDate: nextStart, endDate: nextEnd, dueDate: nextEnd, scheduleBlocks: undefined },
    startDate: nextStart,
    endDate: nextEnd,
  }
}

function isRecurringOn(item: TodoItem, day: string): boolean | null {
  const recurrence = item.recurrence
  if (!recurrence) return null
  const date = dateFromKey(day)
  const start = scheduledStart(item)
  if (!date || !start || day < start) return false
  if (recurrence.frequency === 'daily') return true
  if (recurrence.frequency === 'weekly') return recurrence.weekdays.includes(date.getDay())
  if (recurrence.frequency === 'monthly') return date.getDate() === recurrence.day
  return date.getMonth() + 1 === recurrence.month && date.getDate() === recurrence.day
}

function isScheduledOn(item: TodoItem, day: string): boolean {
  const recurring = isRecurringOn(item, day)
  if (recurring !== null) return recurring
  if (item.scheduleBlocks?.length) return item.scheduleBlocks.some((block) => block.date === day)
  const start = scheduledStart(item)
  const end = scheduledEnd(item)
  return Boolean(start && end && start <= day && day <= end)
}

function isScheduledInMonth(item: TodoItem, month: Date): boolean {
  const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate()
  return Array.from({ length: days }, (_, index) => (
    isScheduledOn(item, dateKey(new Date(month.getFullYear(), month.getMonth(), index + 1)))
  )).some(Boolean)
}

function sortTodos(items: TodoItem[]): TodoItem[] {
  return [...items].sort((left, right) => (
    priorityRank(left.priority) - priorityRank(right.priority)
    || (left.dueTime ?? '99:99').localeCompare(right.dueTime ?? '99:99')
    || (left.createdAt ?? '').localeCompare(right.createdAt ?? '')
    || left.title.localeCompare(right.title, 'ko')
  ))
}

function monthCells(month: Date): Array<Date | null> {
  const first = new Date(month.getFullYear(), month.getMonth(), 1)
  const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate()
  const total = Math.ceil((first.getDay() + days) / 7) * 7
  return Array.from({ length: total }, (_, index) => {
    const day = index - first.getDay() + 1
    return day >= 1 && day <= days ? new Date(month.getFullYear(), month.getMonth(), day) : null
  })
}

function buildCalendarWeekLayouts(cells: Array<Date | null>, items: TodoItem[]): CalendarWeekLayout[] {
  // Keep an item's lane while it continues into the following week.  A date
  // range is therefore drawn as one uninterrupted bar within each week row.
  const preferredLanes = new Map<string, number>()
  const sortedItems = sortTodos(items)

  return Array.from({ length: Math.ceil(cells.length / 7) }, (_, weekIndex) => {
    const weekCells = cells.slice(weekIndex * 7, weekIndex * 7 + 7)
    const segments: Omit<CalendarRangeBar, 'lane'>[] = []

    for (const item of sortedItems) {
      let segmentStart = -1
      for (let column = 0; column <= weekCells.length; column += 1) {
        const date = weekCells[column] ? dateKey(weekCells[column]!) : null
        const active = Boolean(date && isScheduledOn(item, date))
        if (active && segmentStart === -1) {
          segmentStart = column
          continue
        }
        if (segmentStart === -1 || active) continue

        const endColumn = column - 1
        const startDate = weekCells[segmentStart] ? dateKey(weekCells[segmentStart]!) : null
        const endDate = weekCells[endColumn] ? dateKey(weekCells[endColumn]!) : null
        if (startDate && endDate) {
          segments.push({
            item,
            startColumn: segmentStart,
            endColumn,
            startsAtRangeStart: !isScheduledOn(item, addDays(startDate, -1)),
            endsAtRangeEnd: !isScheduledOn(item, addDays(endDate, 1)),
          })
        }
        segmentStart = -1
      }
    }

    const laneEnds: number[] = []
    const bars = [...segments]
      .sort((left, right) => (
        // Continuing bars take their existing lane before new work is placed.
        Number(!preferredLanes.has(left.item.id)) - Number(!preferredLanes.has(right.item.id))
        || priorityRank(left.item.priority) - priorityRank(right.item.priority)
        || left.startColumn - right.startColumn
        || right.endColumn - left.endColumn
        || left.item.title.localeCompare(right.item.title, 'ko')
      ))
      .map((segment) => {
        const preferred = preferredLanes.get(segment.item.id)
        let lane = typeof preferred === 'number' && (laneEnds[preferred] ?? -1) < segment.startColumn
          ? preferred
          : laneEnds.findIndex((end) => end < segment.startColumn)
        if (lane < 0) lane = laneEnds.length
        laneEnds[lane] = segment.endColumn
        preferredLanes.set(segment.item.id, lane)
        return { ...segment, lane }
      })
      .sort((left, right) => left.lane - right.lane || left.startColumn - right.startColumn)

    return { cells: weekCells, bars, laneCount: Math.max(1, laneEnds.length) }
  })
}

function TodoView({ active, backend }: Props): React.JSX.Element {
  const today = dateKey(new Date())
  const [items, setItems] = useState<TodoItem[]>([])
  const [filter, setFilter] = useState<TodoFilter>('all')
  const [plannerView, setPlannerView] = useState<PlannerView>('month')
  const [selectedDate, setSelectedDate] = useState(today)
  const [month, setMonth] = useState(() => new Date(new Date().getFullYear(), new Date().getMonth(), 1))
  const [calendarMode, setCalendarMode] = useState<CalendarMode>('month')
  const [calendarMotion, setCalendarMotion] = useState<CalendarMotion>(null)
  const [loading, setLoading] = useState(false)
  const [updatingId, setUpdatingId] = useState<string | null>(null)
  const [message, setMessage] = useState('')
  const [editorOpen, setEditorOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [editorDraft, setEditorDraft] = useState<TodoEditorDraft>(() => emptyTodoDraft(today))
  const [contextMenu, setContextMenu] = useState<TodoContextMenu | null>(null)
  const [contextValue, setContextValue] = useState('')
  const [contextSchedule, setContextSchedule] = useState<TodoEditorDraft>(() => emptyTodoDraft(today))
  const [activeDrag, setActiveDrag] = useState<DragPayload | null>(null)
  const [dragPreviewDate, setDragPreviewDate] = useState<string | null>(null)
  // Electron can omit a custom MIME value at the final drop target. Keep a
  // synchronous copy of the active drag payload in addition to React state.
  const activeDragRef = useRef<DragPayload | null>(null)
  const pointerDragRef = useRef<PointerDragState | null>(null)
  const timeScrollRef = useRef<HTMLDivElement | null>(null)
  const [replanPreview, setReplanPreview] = useState<ReplanPreview | null>(null)
  const [replanLoading, setReplanLoading] = useState(false)
  const [replanApplying, setReplanApplying] = useState(false)

  const base = endpoint(backend, '')
  const ready = Boolean(base)

  const refresh = useCallback(async (): Promise<void> => {
    if (!base) {
      setItems([])
      return
    }
    setLoading(true)
    setMessage('')
    try {
      const result = await responseJson<{ items: TodoItem[] }>(await fetch(`${base}/creator/todos`, { headers: authHeaders() }))
      const next = Array.isArray(result.items) ? result.items : []
      const firstScheduled = next.find((item) => scheduledEnd(item))
      setItems(next)
      setSelectedDate((current) => next.some((item) => isScheduledOn(item, current)) ? current : scheduledEnd(firstScheduled ?? {} as TodoItem) ?? current)
      setMonth((current) => {
        const fallback = firstScheduled ? dateFromKey(scheduledEnd(firstScheduled) ?? '') : null
        if (!fallback) return current
        return current.getFullYear() !== fallback.getFullYear() || current.getMonth() !== fallback.getMonth()
          ? new Date(fallback.getFullYear(), fallback.getMonth(), 1)
          : current
      })
    } catch (error) {
      setItems([])
      setMessage(error instanceof Error ? error.message : '캘린더 목록을 불러오지 못했습니다.')
    } finally {
      setLoading(false)
    }
  }, [base])

  useEffect(() => {
    if (active) void refresh()
  }, [active, refresh])

  const patchTodo = async (item: TodoItem, patch: Record<string, unknown>): Promise<boolean> => {
    if (!base) return false
    setUpdatingId(item.id)
    setMessage('')
    try {
      const result = await responseJson<{ item: TodoItem }>(await fetch(`${base}/creator/todos/${encodeURIComponent(item.id)}`, {
        method: 'PATCH',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify(patch)
      }))
      setItems((current) => current.map((entry) => entry.id === item.id ? { ...result.item, workspace: entry.workspace } : entry))
      return true
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '일정을 수정하지 못했습니다.')
      return false
    } finally {
      setUpdatingId(null)
    }
  }

  const openCreateEditor = (date = selectedDate): void => {
    setEditorDraft(emptyTodoDraft(date))
    setEditorOpen(true)
    setMessage('')
  }

  const createTodo = async (): Promise<void> => {
    if (!base || !editorDraft.title.trim()) {
      setMessage('새 작업의 이름을 입력하세요.')
      return
    }
    if (editorDraft.startDate > editorDraft.endDate) {
      setMessage('시작일은 종료일보다 늦을 수 없습니다.')
      return
    }
    if (editorDraft.startTime && editorDraft.endTime && editorDraft.startTime >= editorDraft.endTime) {
      setMessage('종료 시각은 시작 시각보다 늦어야 합니다.')
      return
    }
    setCreating(true)
    setMessage('')
    try {
      const result = await responseJson<{ item: TodoItem }>(await fetch(`${base}/creator/todos`, {
        method: 'POST',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: editorDraft.title.trim(), priority: editorDraft.priority,
          startDate: editorDraft.startDate || null, endDate: editorDraft.endDate || null,
          dueTime: editorDraft.startTime || null, endTime: editorDraft.endTime || null,
          estimatedMinutes: Number(editorDraft.estimatedMinutes), recurrence: recurrenceFromDraft(editorDraft),
        })
      }))
      setItems((current) => [result.item, ...current])
      if (result.item.startDate) selectDate(result.item.startDate)
      setEditorOpen(false)
      setMessage('새 작업을 Aiso 플래너에 등록했습니다.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '새 작업을 등록하지 못했습니다.')
    } finally {
      setCreating(false)
    }
  }

  const updateStatus = async (item: TodoItem): Promise<void> => {
    await patchTodo(item, { status: item.status === 'done' ? 'open' : 'done' })
  }

  const previewReplan = async (): Promise<void> => {
    if (!base) return
    setReplanLoading(true)
    setMessage('')
    try {
      const result = await responseJson<ReplanPreview>(await fetch(`${base}/creator/todos/replan-preview`, {
        method: 'POST',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ asOf: today })
      }))
      setReplanPreview(result)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '일정 제안을 만들지 못했습니다.')
    } finally {
      setReplanLoading(false)
    }
  }

  const applyReplan = async (): Promise<void> => {
    if (!base || !replanPreview) return
    setReplanApplying(true)
    setMessage('')
    try {
      const result = await responseJson<{ items: TodoItem[], proposal: ReplanPreview }>(await fetch(`${base}/creator/todos/replan-apply`, {
        method: 'POST',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ asOf: replanPreview.asOf })
      }))
      setItems(result.items)
      const firstDate = result.proposal.plans[0]?.assignments[0]?.date
      if (firstDate) selectDate(firstDate)
      setReplanPreview(null)
      setMessage('제안한 작업 기간과 일별 분배를 적용했습니다.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '일정 제안을 적용하지 못했습니다.')
    } finally {
      setReplanApplying(false)
    }
  }

  const openContextMenu = (event: ReactMouseEvent<HTMLElement>, item: TodoItem): void => {
    event.preventDefault()
    const maxLeft = Math.max(12, window.innerWidth - 282)
    const maxTop = Math.max(12, window.innerHeight - 310)
    setContextValue('')
    setContextMenu({ item, x: Math.min(event.clientX, maxLeft), y: Math.min(event.clientY, maxTop), editor: 'actions' })
  }

  const beginContextEdit = (editor: ContextEditor): void => {
    if (!contextMenu) return
    if (editor === 'plan') {
      const start = scheduledStart(contextMenu.item) ?? ''
      const end = scheduledEnd(contextMenu.item) ?? start
      const recurrence = contextMenu.item.recurrence
      setContextSchedule({
        title: contextMenu.item.title,
        priority: contextMenu.item.priority,
        startDate: start,
        endDate: end,
        startTime: contextMenu.item.dueTime ?? '',
        endTime: contextMenu.item.endTime ?? '',
        estimatedMinutes: String(contextMenu.item.estimatedMinutes ?? 30),
        recurrenceMode: recurrence?.frequency ?? 'none',
        weekdays: recurrence?.frequency === 'weekly' ? recurrence.weekdays : [dateFromKey(start)?.getDay() ?? 1],
      })
    } else {
      setContextValue(editor === 'rename' ? contextMenu.item.title : contextMenu.item.dueDate ?? '')
    }
    setContextMenu({ ...contextMenu, editor })
  }

  const saveContextEdit = async (): Promise<void> => {
    if (!contextMenu) return
    if (contextMenu.editor === 'rename') {
      if (!contextValue.trim()) {
        setMessage('일정 이름을 입력하세요.')
        return
      }
      if (await patchTodo(contextMenu.item, { title: contextValue.trim() })) setContextMenu(null)
      return
    }
    if (contextMenu.editor === 'due') {
      if (!contextValue) {
        setMessage('기한 날짜를 선택하세요.')
        return
      }
      if (await patchTodo(contextMenu.item, { dueDate: contextValue, startDate: contextValue, endDate: contextValue })) setContextMenu(null)
      return
    }
    if (contextMenu.editor === 'plan') {
      if (!contextSchedule.startDate || !contextSchedule.endDate) {
        setMessage('시작일과 종료일을 모두 선택하세요.')
        return
      }
      if (contextSchedule.startDate > contextSchedule.endDate) {
        setMessage('시작일은 종료일보다 늦을 수 없습니다.')
        return
      }
      if (contextSchedule.startTime && contextSchedule.endTime && contextSchedule.startTime >= contextSchedule.endTime) {
        setMessage('종료 시각은 시작 시각보다 늦어야 합니다.')
        return
      }
      if (await patchTodo(contextMenu.item, {
        startDate: contextSchedule.startDate,
        endDate: contextSchedule.endDate,
        dueDate: contextSchedule.endDate,
        dueTime: contextSchedule.startTime || null,
        endTime: contextSchedule.endTime || null,
        estimatedMinutes: Number(contextSchedule.estimatedMinutes),
        recurrence: recurrenceFromDraft(contextSchedule),
      })) setContextMenu(null)
    }
  }

  const changePriority = async (item: TodoItem, priority: Priority): Promise<void> => {
    if (await patchTodo(item, { priority })) setContextMenu(null)
  }

  const deleteTodo = async (item: TodoItem): Promise<void> => {
    if (!base) return
    setUpdatingId(item.id)
    setMessage('')
    try {
      await responseJson<{ id: string }>(await fetch(`${base}/creator/todos/${encodeURIComponent(item.id)}`, {
        method: 'DELETE', headers: authHeaders()
      }))
      setItems((current) => current.filter((entry) => entry.id !== item.id))
      setContextMenu(null)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '일정을 삭제하지 못했습니다.')
    } finally {
      setUpdatingId(null)
    }
  }

  useEffect(() => {
    if (!contextMenu) return
    const closeOnOutsidePress = (event: PointerEvent): void => {
      const target = event.target
      if (target instanceof Element && target.closest('.todo-context-menu')) return
      setContextMenu(null)
    }
    const closeOnEscape = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') setContextMenu(null)
    }
    window.addEventListener('pointerdown', closeOnOutsidePress)
    window.addEventListener('keydown', closeOnEscape)
    return () => {
      window.removeEventListener('pointerdown', closeOnOutsidePress)
      window.removeEventListener('keydown', closeOnEscape)
    }
  }, [contextMenu])

  const visibleItems = useMemo(() => filter === 'all' ? items : items.filter((item) => item.status === filter), [filter, items])
  const scheduledItems = useMemo(() => visibleItems.filter((item) => scheduledStart(item) && scheduledEnd(item)), [visibleItems])
  const unscheduledItems = useMemo(() => sortTodos(visibleItems.filter((item) => !scheduledStart(item))), [visibleItems])
  const selectedItems = useMemo(() => sortTodos(scheduledItems.filter((item) => isScheduledOn(item, selectedDate))), [scheduledItems, selectedDate])
  const todayItems = useMemo(() => sortTodos(scheduledItems.filter((item) => item.status === 'open' && isScheduledOn(item, today))), [scheduledItems, today])
  const overdueItems = useMemo(() => sortTodos(items.filter((item) => item.status === 'open' && !item.recurrence && Boolean(scheduledEnd(item)) && scheduledEnd(item)! < today)), [items, today])
  const soonItems = useMemo(() => sortTodos(items.filter((item) => item.status === 'open' && !item.recurrence && Boolean(scheduledEnd(item)) && scheduledEnd(item)! >= today && scheduledEnd(item)! <= addDays(today, 3))), [items, today])
  const replanCandidates = useMemo(() => sortTodos(items.filter((item) => item.status === 'open' && !item.recurrence && Boolean(scheduledEnd(item)) && scheduledEnd(item)! <= today)), [items, today])
  const cells = monthCells(month)
  const calendarWeeks = buildCalendarWeekLayouts(cells, scheduledItems)
  const yearMonths = Array.from({ length: 12 }, (_, index) => new Date(month.getFullYear(), index, 1))
  const weekDays = Array.from({ length: 7 }, (_, index) => addDays(dateKey(startOfWeek(selectedDate)), index))
  const dragSchedulePreview = activeDrag && dragPreviewDate
    ? (() => {
        const item = items.find((entry) => entry.id === activeDrag.id)
        return item ? previewDragSchedule(item, activeDrag, dragPreviewDate) : null
      })()
    : null
  const previewCalendarWeeks = buildCalendarWeekLayouts(cells, dragSchedulePreview ? [dragSchedulePreview.item] : [])

  useEffect(() => {
    if (plannerView !== 'day' && plannerView !== 'week') return
    const frame = window.requestAnimationFrame(() => {
      if (timeScrollRef.current) timeScrollRef.current.scrollTop = 7 * HOUR_HEIGHT
    })
    return () => window.cancelAnimationFrame(frame)
  }, [plannerView])

  const moveMonth = (delta: number): void => {
    setCalendarMotion(delta > 0 ? 'forward' : 'backward')
    setMonth((current) => new Date(current.getFullYear(), current.getMonth() + delta, 1))
  }
  const moveYear = (delta: number): void => {
    setCalendarMotion(delta > 0 ? 'forward' : 'backward')
    setMonth((current) => new Date(current.getFullYear() + delta, current.getMonth(), 1))
  }
  const selectMonth = (nextMonth: Date): void => {
    setCalendarMotion(null)
    setMonth(nextMonth)
    setCalendarMode('month')
  }
  const selectDate = (date: string): void => {
    setSelectedDate(date)
    const value = dateFromKey(date)
    if (value) setMonth(new Date(value.getFullYear(), value.getMonth(), 1))
  }
  const toggleCalendarMode = (): void => {
    setCalendarMotion(null)
    setCalendarMode((current) => current === 'month' ? 'year' : 'month')
  }

  const startTaskDrag = (event: ReactDragEvent<HTMLElement>, item: TodoItem, kind: DragKind = 'move'): void => {
    const payload = { id: item.id, kind } satisfies DragPayload
    activeDragRef.current = payload
    setActiveDrag(payload)
    setDragPreviewDate(null)
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('application/x-aiso-todo', JSON.stringify(payload))
    event.dataTransfer.setData('text/plain', item.title)
  }

  const clearTaskDrag = (): void => {
    activeDragRef.current = null
    pointerDragRef.current = null
    setActiveDrag(null)
    setDragPreviewDate(null)
    document.body.classList.remove('todo-pointer-dragging')
  }

  const readDragPayload = (event: ReactDragEvent<HTMLElement>): DragPayload | null => {
    const fallback = activeDragRef.current ?? activeDrag
    try {
      const raw = JSON.parse(event.dataTransfer.getData('application/x-aiso-todo')) as Partial<DragPayload>
      return typeof raw.id === 'string' && (raw.kind === 'move' || raw.kind === 'start' || raw.kind === 'end') ? raw as DragPayload : fallback
    } catch {
      // Electron/Chromium can drop a custom MIME type on nested draggables.
      // Keep the same in-memory payload for this one drag as a safe fallback.
      return fallback
    }
  }

  const applyTaskDrop = async (payload: DragPayload, targetDate: string): Promise<void> => {
    const item = payload ? items.find((entry) => entry.id === payload.id) : null
    if (!item) {
      clearTaskDrag()
      return
    }
    const start = scheduledStart(item) ?? targetDate
    const end = scheduledEnd(item) ?? start
    let nextStart = start
    let nextEnd = end
    if (payload.kind === 'move') {
      const span = daysBetween(start, end)
      nextStart = targetDate
      nextEnd = addDays(targetDate, span)
    } else if (payload.kind === 'start') {
      if (targetDate > end) {
        setMessage('시작일은 종료일보다 늦을 수 없습니다.')
        clearTaskDrag()
        return
      }
      nextStart = targetDate
    } else {
      if (targetDate < start) {
        setMessage('종료일은 시작일보다 빠를 수 없습니다.')
        clearTaskDrag()
        return
      }
      nextEnd = targetDate
    }
    try {
      await patchTodo(item, { startDate: nextStart, endDate: nextEnd, dueDate: nextEnd })
    } finally {
      clearTaskDrag()
    }
  }

  const dropTaskOnDate = async (event: ReactDragEvent<HTMLElement>, targetDate: string): Promise<void> => {
    event.preventDefault()
    const payload = readDragPayload(event)
    if (!payload) {
      clearTaskDrag()
      return
    }
    await applyTaskDrop(payload, targetDate)
  }

  const dropTaskOnTimeColumn = async (event: ReactDragEvent<HTMLElement>, targetDate: string): Promise<void> => {
    event.preventDefault()
    const payload = readDragPayload(event)
    const item = payload ? items.find((entry) => entry.id === payload.id) : null
    if (!payload || !item) {
      clearTaskDrag()
      return
    }
    if (payload.kind !== 'move') {
      await applyTaskDrop(payload, targetDate)
      return
    }
    const bounds = event.currentTarget.getBoundingClientRect()
    const rawMinutes = ((event.clientY - bounds.top) / HOUR_HEIGHT) * 60
    const startMinutes = Math.max(0, Math.min(MINUTES_PER_DAY - 15, Math.round(rawMinutes / 15) * 15))
    const previousStart = clockMinutes(item.dueTime)
    const previousEnd = clockMinutes(item.endTime)
    const duration = previousStart != null && previousEnd != null && previousEnd > previousStart
      ? previousEnd - previousStart
      : Math.max(15, item.estimatedMinutes ?? 30)
    const endMinutes = Math.min(MINUTES_PER_DAY - 1, startMinutes + duration)
    try {
      await patchTodo(item, {
        startDate: targetDate, endDate: targetDate, dueDate: targetDate,
        dueTime: clockFromMinutes(startMinutes), endTime: clockFromMinutes(endMinutes),
      })
      selectDate(targetDate)
    } finally {
      clearTaskDrag()
    }
  }

  const calendarDropDate = (target: EventTarget | null, clientX: number): string | null => {
    if (!(target instanceof Element)) return null
    const exactDate = target.closest<HTMLElement>('[data-todo-date]')?.dataset.todoDate
    if (exactDate) return exactDate

    const grid = target.closest<HTMLElement>('.todo-week__surface, .todo-calendar__week')
    if (!grid) return null
    const bounds = grid.getBoundingClientRect()
    if (bounds.width <= 0 || !Number.isFinite(clientX)) return null
    const column = Math.max(0, Math.min(6, Math.floor(((clientX - bounds.left) / bounds.width) * 7)))

    if (grid.classList.contains('todo-week__surface')) return weekDays[column] ?? null
    const monthWeeks = Array.from(document.querySelectorAll<HTMLElement>('.todo-calendar__week'))
    const weekIndex = monthWeeks.indexOf(grid)
    const date = weekIndex >= 0 ? calendarWeeks[weekIndex]?.cells[column] : null
    return date ? dateKey(date) : null
  }

  useEffect(() => {
    const allowCalendarDrop = (event: DragEvent): void => {
      if (!activeDragRef.current) return
      const targetDate = calendarDropDate(event.target, event.clientX)
      setDragPreviewDate(targetDate)
      if (!targetDate) return
      event.preventDefault()
    }

    const finishCalendarDrop = (event: DragEvent): void => {
      if (!activeDragRef.current) return
      const targetDate = calendarDropDate(event.target, event.clientX)
      if (!targetDate) return
      // Capture this event before the bar/cell React handlers. This path also
      // works when a native drag reaches a range bar instead of the temporary
      // transparent day overlay.
      event.preventDefault()
      event.stopPropagation()
      void dropTaskOnDate(event as unknown as ReactDragEvent<HTMLElement>, targetDate)
    }

    document.addEventListener('dragover', allowCalendarDrop, true)
    document.addEventListener('drop', finishCalendarDrop, true)
    return () => {
      document.removeEventListener('dragover', allowCalendarDrop, true)
      document.removeEventListener('drop', finishCalendarDrop, true)
    }
  }, [calendarWeeks, weekDays, dropTaskOnDate])

  useEffect(() => {
    const onPointerMove = (event: PointerEvent): void => {
      const drag = pointerDragRef.current
      if (!drag || drag.pointerId !== event.pointerId) return
      const moved = Math.hypot(event.clientX - drag.originX, event.clientY - drag.originY)
      if (moved < 4) return
      const hitTarget = (typeof document.elementFromPoint === 'function'
        ? document.elementFromPoint(event.clientX, event.clientY)
        : null) ?? event.target
      setDragPreviewDate(calendarDropDate(hitTarget, event.clientX))
      event.preventDefault()
    }

    const finishPointerDrag = (event: PointerEvent): void => {
      const drag = pointerDragRef.current
      if (!drag || drag.pointerId !== event.pointerId) return
      const moved = Math.hypot(event.clientX - drag.originX, event.clientY - drag.originY)
      const hitTarget = (typeof document.elementFromPoint === 'function'
        ? document.elementFromPoint(event.clientX, event.clientY)
        : null) ?? event.target
      const targetDate = moved >= 4 ? calendarDropDate(hitTarget, event.clientX) : null
      if (targetDate) void applyTaskDrop(drag.payload, targetDate)
      else clearTaskDrag()
    }

    const cancelPointerDrag = (event: PointerEvent): void => {
      if (pointerDragRef.current?.pointerId === event.pointerId) clearTaskDrag()
    }

    document.addEventListener('pointermove', onPointerMove, { passive: false })
    document.addEventListener('pointerup', finishPointerDrag)
    document.addEventListener('pointercancel', cancelPointerDrag)
    return () => {
      document.removeEventListener('pointermove', onPointerMove)
      document.removeEventListener('pointerup', finishPointerDrag)
      document.removeEventListener('pointercancel', cancelPointerDrag)
    }
  }, [applyTaskDrop, calendarDropDate])

  const startPointerTaskDrag = (event: ReactPointerEvent<HTMLElement>, item: TodoItem, kind: DragKind = 'move'): void => {
    if (event.button !== 0 || item.recurrence) return
    if (kind === 'move' && event.target instanceof HTMLElement && event.target.closest('button, input, select, textarea, a, .todo-calendar__range-handle')) return
    const payload = { id: item.id, kind } satisfies DragPayload
    event.preventDefault()
    activeDragRef.current = payload
    pointerDragRef.current = { payload, pointerId: event.pointerId, originX: event.clientX, originY: event.clientY }
    setActiveDrag(payload)
    setDragPreviewDate(null)
    document.body.classList.add('todo-pointer-dragging')
    try {
      event.currentTarget.setPointerCapture(event.pointerId)
    } catch {
      // The document-level listeners still complete the gesture when capture
      // is unavailable in an older Electron or a test environment.
    }
  }

  const renderCalendarRangeBar = (bar: CalendarRangeBar): React.JSX.Element => {
    const { item, startColumn, endColumn, lane, startsAtRangeStart, endsAtRangeEnd } = bar
    const recurring = Boolean(item.recurrence)
    const style: CSSProperties = { gridColumn: `${startColumn + 1} / ${endColumn + 2}`, gridRow: lane + 1 }
    return <div
      key={`${item.id}-${startColumn}-${endColumn}`}
      className={`todo-calendar__range-bar todo-calendar__range-bar--${item.priority}${item.status === 'done' ? ' is-done' : ''}${activeDrag?.id === item.id ? ' is-drag-source' : ''}${startsAtRangeStart ? ' is-range-start' : ' is-continuation-start'}${endsAtRangeEnd ? ' is-range-end' : ' is-continuation-end'}`}
      style={style}
      draggable={!recurring}
      title={`${item.title} · ${priorityLabel(item.priority)} · ${durationLabel(item)}${item.recurrence ? ` · ${recurrenceLabel(item.recurrence)}` : ''}${recurring ? ' · 반복 일정은 우클릭으로 관리' : ''}`}
      onDragStart={(event) => {
        if (pointerDragRef.current) { event.preventDefault(); return }
        if (recurring) return
        if (event.target instanceof HTMLElement && event.target.closest('.todo-calendar__range-handle')) return
        startTaskDrag(event, item)
      }}
      onDragEnd={clearTaskDrag}
      onPointerDown={(event) => startPointerTaskDrag(event, item)}
      onContextMenu={(event) => openContextMenu(event, item)}
    >
      {!recurring && startsAtRangeStart && <span className="todo-calendar__range-handle" draggable title="시작일을 다른 날짜로 끌어 놓기" aria-label="시작일을 다른 날짜로 끌어 놓기" onPointerDown={(event) => { event.stopPropagation(); startPointerTaskDrag(event, item, 'start') }} onDragStart={(event) => { event.stopPropagation(); if (pointerDragRef.current) { event.preventDefault(); return }; startTaskDrag(event, item, 'start') }}>‹</span>}
      <span className="todo-calendar__range-title"><em>{priorityCode(item.priority)}</em>{item.title}</span>
      {!recurring && endsAtRangeEnd && <span className="todo-calendar__range-handle" draggable title="종료일을 다른 날짜로 끌어 놓기" aria-label="종료일을 다른 날짜로 끌어 놓기" onPointerDown={(event) => { event.stopPropagation(); startPointerTaskDrag(event, item, 'end') }} onDragStart={(event) => { event.stopPropagation(); if (pointerDragRef.current) { event.preventDefault(); return }; startTaskDrag(event, item, 'end') }}>›</span>}
    </div>
  }

  const renderCalendarPreviewBar = (bar: CalendarRangeBar): React.JSX.Element => {
    const label = activeDrag?.kind === 'start' ? '시작일 변경' : activeDrag?.kind === 'end' ? '종료일 변경' : '일정 이동'
    return <div
      key={`preview-${bar.item.id}-${bar.startColumn}-${bar.endColumn}`}
      className={`todo-calendar__range-bar todo-calendar__range-bar--${bar.item.priority} is-drag-preview${bar.startsAtRangeStart ? ' is-range-start' : ' is-continuation-start'}${bar.endsAtRangeEnd ? ' is-range-end' : ' is-continuation-end'}`}
      style={{ gridColumn: `${bar.startColumn + 1} / ${bar.endColumn + 2}`, gridRow: bar.lane + 1 }}
      aria-label={`${label}: ${bar.item.title}`}
    >
      <span className="todo-calendar__range-title"><em>{label}</em>{bar.item.title}</span>
    </div>
  }

  const renderTodoRow = (item: TodoItem): React.JSX.Element => <article
    key={item.id}
    className={`todo-row${item.status === 'done' ? ' is-done' : ''}${activeDrag?.id === item.id ? ' is-drag-source' : ''}`}
    draggable={!item.recurrence}
    onDragStart={(event) => { if (pointerDragRef.current) { event.preventDefault(); return }; if (!item.recurrence) startTaskDrag(event, item) }}
    onDragEnd={clearTaskDrag}
    onPointerDown={(event) => startPointerTaskDrag(event, item)}
    onContextMenu={(event) => openContextMenu(event, item)}
  >
    <button
      type="button"
      className="todo-row__check"
      aria-label={item.recurrence ? `${item.title} 반복 일정` : `${item.title} ${item.status === 'done' ? '다시 열기' : '완료 처리'}`}
      title={item.recurrence ? '반복 일정은 한 번 완료하면 전체 시리즈가 사라질 수 있어 우클릭 메뉴에서 관리합니다.' : undefined}
      disabled={Boolean(item.recurrence) || updatingId === item.id}
      onClick={() => { if (!item.recurrence) void updateStatus(item) }}
    >{item.status === 'done' && '✓'}</button>
    <time>{timeLabel(item)}</time>
    <div className="todo-row__content"><b>{item.title}</b><span>{workspaceLabel(item.workspace)} · {durationLabel(item)} · 등록 {createdTimeLabel(item.createdAt)}</span></div>
    <em className={`todo-priority todo-priority--${item.priority}`}>{priorityCode(item.priority)}</em>
  </article>

  const renderTimePlanner = (days: string[], label: string, moveBy: number): React.JSX.Element => {
    const hours = Array.from({ length: 24 }, (_, hour) => hour)
    const untimed = days.flatMap((day) => sortTodos(scheduledItems.filter((item) => isScheduledOn(item, day) && !item.dueTime)).map((item) => ({ day, item })))
    const allDayRangeLayout = days.length > 1
      ? buildCalendarWeekLayouts(days.map(dateFromKey), scheduledItems.filter((item) => !item.recurrence && scheduledStart(item) !== scheduledEnd(item)))[0]!
      : null
    return <section className={`todo-time-planner${days.length === 1 ? ' todo-time-planner--day' : ''}`}>
      <header className="todo-time-planner__head">
        <div><span className="todo-section-label">{days.length === 1 ? 'DAY' : 'WEEK'}</span><h2>{label}</h2><p>시간이 지정된 작업은 15분 단위로 끌어 옮길 수 있습니다. 우클릭하면 정확한 기간·시간·반복을 편집합니다.</p></div>
        <div className="todo-day-controls"><button type="button" onClick={() => selectDate(addDays(selectedDate, -moveBy))}>‹ 이전</button><button type="button" onClick={() => selectDate(today)}>오늘</button><button type="button" onClick={() => selectDate(addDays(selectedDate, moveBy))}>다음 ›</button></div>
      </header>
      <div className="todo-time-planner__day-head" style={{ '--todo-time-days': days.length } as CSSProperties}>
        <span aria-hidden="true" />
        {days.map((day) => <button key={day} type="button" className={day === today ? 'is-today' : ''} onClick={() => selectDate(day)}><small>{new Intl.DateTimeFormat('ko-KR', { weekday: 'short' }).format(dateFromKey(day) ?? new Date())}</small><b>{dateFromKey(day)?.getDate()}</b></button>)}
      </div>
      <div className="todo-time-planner__all-day" style={{ '--todo-time-days': days.length } as CSSProperties}>
        <span>종일</span>
        {days.map((day) => <div key={day} onDragOver={(event) => event.preventDefault()} onDrop={(event) => void dropTaskOnDate(event, day)}>{untimed.filter((entry) => entry.day === day && (days.length === 1 || Boolean(entry.item.recurrence) || scheduledStart(entry.item) === scheduledEnd(entry.item))).map(({ item }) => <button key={`${day}-${item.id}`} type="button" draggable={!item.recurrence} onDragStart={(event) => { if (!item.recurrence) startTaskDrag(event, item) }} onDragEnd={clearTaskDrag} onContextMenu={(event) => openContextMenu(event, item)} className={`todo-time-event todo-time-event--all-day todo-time-event--${item.priority}${item.status === 'done' ? ' is-done' : ''}`}><em>{priorityCode(item.priority)}</em><span>{item.title}</span></button>)}</div>)}
        {allDayRangeLayout && <div className="todo-calendar__range-layer todo-week__range-layer todo-time-planner__range-layer" aria-label="주간 기간 작업">{allDayRangeLayout.bars.map(renderCalendarRangeBar)}</div>}
      </div>
      <div className="todo-time-planner__scroll" ref={timeScrollRef}>
        <div className="todo-time-planner__canvas" style={{ '--todo-time-days': days.length, height: `${24 * HOUR_HEIGHT}px` } as CSSProperties}>
          <div className="todo-time-planner__hours">{hours.map((hour) => <span key={hour} style={{ top: `${hour * HOUR_HEIGHT}px` }}>{String(hour).padStart(2, '0')}:00</span>)}</div>
          <div className="todo-time-planner__columns">
            {days.map((day) => <div key={day} data-todo-date={day} className={`todo-time-planner__column${day === today ? ' is-today' : ''}`} onDragOver={(event) => event.preventDefault()} onDrop={(event) => void dropTaskOnTimeColumn(event, day)}>
              {hours.map((hour) => <span key={hour} className="todo-time-planner__hour-line" style={{ top: `${hour * HOUR_HEIGHT}px` }} />)}
              {sortTodos(scheduledItems.filter((item) => isScheduledOn(item, day) && Boolean(item.dueTime) && (scheduledStart(item) === scheduledEnd(item) || scheduledStart(item) === day || Boolean(item.recurrence)))).map((item) => {
                const start = clockMinutes(item.dueTime) ?? 9 * 60
                const explicitEnd = clockMinutes(item.endTime)
                const end = explicitEnd != null && explicitEnd > start ? explicitEnd : Math.min(MINUTES_PER_DAY, start + Math.max(15, item.estimatedMinutes ?? 30))
                return <button key={`${day}-${item.id}`} type="button" draggable onDragStart={(event) => startTaskDrag(event, item)} onDragEnd={clearTaskDrag} onContextMenu={(event) => openContextMenu(event, item)} className={`todo-time-event todo-time-event--${item.priority}${item.status === 'done' ? ' is-done' : ''}`} style={{ top: `${(start / 60) * HOUR_HEIGHT}px`, height: `${Math.max(24, ((end - start) / 60) * HOUR_HEIGHT)}px` }} title={`${item.title} · ${timeLabel(item)}`}><em>{priorityCode(item.priority)}</em><b>{item.title}</b><span>{item.dueTime}{item.endTime ? `–${item.endTime}` : ''}</span></button>
              })}
            </div>)}
          </div>
        </div>
      </div>
    </section>
  }

  const plannerTabs: Array<[PlannerView, string]> = [['today', '오늘'], ['day', '일간'], ['week', '주간'], ['month', '월간'], ['list', '목록']]

  return (
    <div className="view todo-view">
      <header className="view__head todo-view__head">
        <div><h1>캘린더</h1></div>
        <div className="todo-view__actions">
          <div className="todo-view-tabs" role="tablist" aria-label="캘린더 보기 전환">
            {plannerTabs.map(([view, label]) => <button key={view} type="button" role="tab" aria-selected={plannerView === view} className={plannerView === view ? 'is-active' : ''} onClick={() => setPlannerView(view)}>{label}</button>)}
          </div>
          <button type="button" className="btn btn--accent" disabled={!ready} onClick={() => openCreateEditor()}>＋ 새 작업</button>
          <button type="button" className="btn btn--ghost2" disabled={!ready || loading} onClick={() => void refresh()}><RefreshIcon /> {loading ? '불러오는 중' : '새로고침'}</button>
        </div>
      </header>

      {!ready ? (
        <section className="todo-empty"><b>백엔드를 준비하고 있습니다.</b><p>준비가 끝나면 중앙 캘린더 저장소를 불러옵니다.</p></section>
      ) : <>
        <section className="todo-focus" aria-label="오늘의 작업">
          <article className="todo-focus__card todo-focus__card--today"><span>오늘 해야 할 일</span><strong>{todayItems.length}개</strong></article>
          <article className="todo-focus__card todo-focus__card--urgent"><span>마감 임박</span><strong>{soonItems.length + overdueItems.length}개</strong></article>
          <article className="todo-focus__card todo-focus__card--planner"><span>일정 제안</span><strong>{Math.ceil(replanCandidates.reduce((total, item) => total + (item.estimatedMinutes ?? 30), 0) / 60)}시간</strong>{replanCandidates.length > 0 && <button type="button" disabled={replanLoading} onClick={() => void previewReplan()}>{replanLoading ? '계산 중' : '제안 보기'}</button>}</article>
        </section>

        {replanPreview && <section className="todo-replan" role="dialog" aria-label="미완료 작업 일정 제안">
          <header>
            <div><span className="todo-section-label">SCHEDULE PROPOSAL</span><h2>미완료 작업 {minuteLabel(replanPreview.totalMinutes)}을 다시 배치할까요?</h2><p>평일 하루 최대 {minuteLabel(replanPreview.dailyCapacityMinutes)} 기준으로 계산했습니다. 적용 전에는 기존 일정이 바뀌지 않습니다.</p></div>
            <button type="button" aria-label="일정 제안 닫기" onClick={() => setReplanPreview(null)}>×</button>
          </header>
          {replanPreview.plans.length === 0 ? <p className="todo-replan__empty">재배치가 필요한 미완료 작업이 없습니다.</p> : <div className="todo-replan__plans">{replanPreview.plans.map((plan) => <article key={plan.todoId}>
            <div><em className={`todo-priority todo-priority--${plan.priority}`}>{priorityCode(plan.priority)}</em><b>{plan.title}</b><span>{minuteLabel(plan.totalMinutes)}</span></div>
            {plan.assignments.length ? <p>{plan.assignments.map((assignment) => `${dayLabel(assignment.date)} ${minuteLabel(assignment.minutes)}`).join(' · ')}</p> : <p>배정 가능한 평일을 찾지 못했습니다.</p>}
            {plan.unallocatedMinutes > 0 && <small>{minuteLabel(plan.unallocatedMinutes)}을 더 배정해야 합니다.</small>}
          </article>)}</div>}
          <footer><button type="button" className="btn btn--ghost2" onClick={() => setReplanPreview(null)}>취소</button><button type="button" className="btn btn--accent" disabled={replanApplying || replanPreview.plans.length === 0 || replanPreview.unallocatedMinutes > 0} onClick={() => void applyReplan()}>{replanApplying ? '적용 중' : '제안 적용'}</button></footer>
        </section>}

        {plannerView === 'today' && <div className="todo-planner-grid todo-planner-grid--today">
          <section className="todo-day-list"><header><div><div className="tool-panel__eyebrow">TODAY</div><h2>{dayLabel(today)}</h2></div><span>{todayItems.length}개</span></header><div className="todo-day-list__body">{todayItems.length ? todayItems.map(renderTodoRow) : <div className="todo-day-list__empty">오늘 예정된 일정이 없습니다.</div>}</div></section>
          <section className="todo-day-list"><header><div><div className="tool-panel__eyebrow">DEADLINE</div><h2>마감 임박 작업</h2></div><span>{soonItems.length + overdueItems.length}개</span></header><div className="todo-day-list__body">{[...overdueItems, ...soonItems.filter((item) => !overdueItems.some((overdue) => overdue.id === item.id))].map(renderTodoRow)}</div></section>
        </div>}

        {plannerView === 'day' && renderTimePlanner([selectedDate], dayLabel(selectedDate), 1)}

        {plannerView === 'week' && renderTimePlanner(weekDays, `${dayLabel(weekDays[0])} – ${dayLabel(weekDays[6])}`, 7)}

        {plannerView === 'month' && <div className="todo-layout">
          <section className="todo-calendar">
            <div className="todo-calendar__top"><div><span className="todo-section-label">CALENDAR</span><b>{calendarMode === 'year' ? '연간 일정' : '월간 일정'}</b></div><div className="todo-filters" aria-label="캘린더 상태 필터">{([['all', '전체'], ['open', '진행'], ['done', '완료']] as const).map(([value, label]) => <button key={value} type="button" aria-pressed={filter === value} className={filter === value ? 'is-active' : ''} onClick={() => setFilter(value)}>{label}</button>)}</div></div>
            <div className="todo-calendar__month"><button type="button" aria-label={calendarMode === 'year' ? '이전 연도' : '이전 달'} onClick={() => calendarMode === 'year' ? moveYear(-1) : moveMonth(-1)}>‹</button><button type="button" className="todo-calendar__month-title" aria-label={calendarMode === 'year' ? `${month.getFullYear()}년 월간 보기` : `${month.getFullYear()}년 연간 보기`} onClick={toggleCalendarMode}>{calendarMode === 'year' ? `${month.getFullYear()}년` : monthLabel(month)}</button><button type="button" aria-label={calendarMode === 'year' ? '다음 연도' : '다음 달'} onClick={() => calendarMode === 'year' ? moveYear(1) : moveMonth(1)}>›</button></div>
            <div key={`${calendarMode}-${month.getFullYear()}-${month.getMonth()}`} className={`todo-calendar__body${calendarMotion ? ` todo-calendar__body--${calendarMotion}` : ''}`}>
              {calendarMode === 'month' ? <>
                <div className="todo-calendar__weekdays" aria-hidden="true">{['일', '월', '화', '수', '목', '금', '토'].map((weekday) => <span key={weekday}>{weekday}</span>)}</div>
                <div className="todo-calendar__weeks">
                  {calendarWeeks.map((week, weekIndex) => <div key={`week-${weekIndex}`} className="todo-calendar__week" style={{ '--todo-range-lanes': week.laneCount, '--todo-range-height': `${week.laneCount * 23}px` } as CSSProperties}>
                    <div className="todo-calendar__week-days">
                      {week.cells.map((date, index) => date ? <section key={dateKey(date)} data-todo-date={dateKey(date)} className={`todo-calendar__day${dateKey(date) === selectedDate ? ' is-selected' : ''}${dateKey(date) === today ? ' is-today' : ''}`} onDragOver={(event) => event.preventDefault()} onDrop={(event) => void dropTaskOnDate(event, dateKey(date))}>
                        <button type="button" className="todo-calendar__day-date" aria-label={`${dayLabel(dateKey(date))}${scheduledItems.filter((item) => isScheduledOn(item, dateKey(date))).length ? ` 일정 ${scheduledItems.filter((item) => isScheduledOn(item, dateKey(date))).length}개` : ''}`} onClick={() => selectDate(dateKey(date))}><b>{date.getDate()}</b></button>
                      </section> : <span className="todo-calendar__blank" key={`blank-${index}`} />)}
                    </div>
                    <div className="todo-calendar__range-layer" aria-label={`${monthLabel(month)} 기간 작업`}>{week.bars.map(renderCalendarRangeBar)}</div>
                    {dragSchedulePreview && <div className="todo-calendar__range-layer todo-calendar__preview-layer" aria-live="polite" aria-label={`${dragSchedulePreview.startDate}부터 ${dragSchedulePreview.endDate}까지 변경 미리보기`}>{previewCalendarWeeks[weekIndex]?.bars.map(renderCalendarPreviewBar)}</div>}
                    {activeDrag && <div className="todo-calendar__drop-targets" aria-label="일정 날짜 변경 영역">
                      {week.cells.map((date, index) => date ? <div key={dateKey(date)} className="todo-calendar__drop-target" data-todo-date={dateKey(date)} onDragOver={(event) => event.preventDefault()} onDrop={(event) => void dropTaskOnDate(event, dateKey(date))} /> : <span key={`drop-blank-${index}`} />)}
                    </div>}
                  </div>)}
                </div>
              </> : <div className="todo-calendar__year" aria-label={`${month.getFullYear()}년 월 선택`}>{yearMonths.map((yearMonth) => { const prefix = `${yearMonth.getFullYear()}-${String(yearMonth.getMonth() + 1).padStart(2, '0')}-`; const count = scheduledItems.filter((item) => isScheduledInMonth(item, yearMonth)).length; const isCurrentMonth = yearMonth.getFullYear() === month.getFullYear() && yearMonth.getMonth() === month.getMonth(); const isTodayMonth = yearMonth.getFullYear() === new Date().getFullYear() && yearMonth.getMonth() === new Date().getMonth(); return <button key={prefix} type="button" aria-label={`${monthLabel(yearMonth)}${count ? ` 일정 ${count}개` : ''}`} className={`todo-year-month${isCurrentMonth ? ' is-selected' : ''}${isTodayMonth ? ' is-today' : ''}`} onClick={() => selectMonth(yearMonth)}><b>{monthOnlyLabel(yearMonth)}</b><span>{count ? `${count}개` : ''}</span></button>})}</div>}
            </div>
            {message && <div className="todo-message" role="status">{message}</div>}
          </section>
          <section className="todo-day-list"><header><div><div className="tool-panel__eyebrow">SCHEDULE</div><h2>{dayLabel(selectedDate)}</h2></div><span>{selectedItems.length}개 · 시간순</span></header><div className="todo-day-list__body" onDragOver={(event) => event.preventDefault()} onDrop={(event) => void dropTaskOnDate(event, selectedDate)}>{selectedItems.length ? selectedItems.map(renderTodoRow) : <div className="todo-day-list__empty">이 날짜에 등록된 일정이 없습니다.</div>}{unscheduledItems.length > 0 && <section className="todo-unscheduled"><h3>기한 미정 <span>{unscheduledItems.length}</span></h3>{unscheduledItems.map(renderTodoRow)}</section>}</div></section>
        </div>}

        {plannerView === 'list' && <section className="todo-day-list todo-day-list--wide"><header><div><div className="tool-panel__eyebrow">LIST</div><h2>모든 일정</h2></div><span>{visibleItems.length}개</span></header><div className="todo-day-list__body">{sortTodos([...scheduledItems, ...unscheduledItems]).map(renderTodoRow)}</div></section>}
        {message && plannerView !== 'month' && <div className="todo-message" role="status">{message}</div>}
      </>}

      {contextMenu && <div className={`todo-context-menu${contextMenu.editor === 'delete' ? ' todo-context-menu--confirm' : ''}`} role={contextMenu.editor === 'delete' ? 'alertdialog' : 'menu'} aria-label={contextMenu.editor === 'delete' ? '일정 삭제 확인' : undefined} style={{ left: contextMenu.x, top: contextMenu.y }}>
        {contextMenu.editor === 'actions' ? <>
          <strong>{contextMenu.item.title}</strong>
          <button type="button" role="menuitem" onClick={() => beginContextEdit('due')}>{contextMenu.item.dueDate ? '기한을 하루로 변경' : '기한 지정'}</button>
          <button type="button" role="menuitem" onClick={() => beginContextEdit('plan')}>작업 기간·소요시간</button>
          <button type="button" role="menuitem" onClick={() => beginContextEdit('rename')}>이름 변경</button>
          <div className="todo-context-menu__priorities" aria-label="우선순위 변경"><button type="button" onClick={() => void changePriority(contextMenu.item, 'high')}>P1</button><button type="button" onClick={() => void changePriority(contextMenu.item, 'medium')}>P2</button><button type="button" onClick={() => void changePriority(contextMenu.item, 'low')}>P3</button></div>
          <button type="button" role="menuitem" className="todo-context-menu__delete" onClick={() => setContextMenu({ ...contextMenu, editor: 'delete' })}>삭제</button>
        </> : contextMenu.editor === 'delete' ? <>
          <strong>일정 삭제</strong>
          <p>“{contextMenu.item.title}”을 삭제할까요?<br />이 작업은 되돌릴 수 없습니다.</p>
          <div className="todo-context-menu__confirm-actions"><button type="button" onClick={() => setContextMenu({ ...contextMenu, editor: 'actions' })}>취소</button><button type="button" className="todo-context-menu__delete" disabled={updatingId === contextMenu.item.id} onClick={() => void deleteTodo(contextMenu.item)}>{updatingId === contextMenu.item.id ? '삭제 중' : '삭제하기'}</button></div>
        </> : <form onSubmit={(event) => { event.preventDefault(); void saveContextEdit() }}>
          {contextMenu.editor === 'plan' ? <><label>시작일<input autoFocus type="date" value={contextSchedule.startDate} onChange={(event) => setContextSchedule((current) => ({ ...current, startDate: event.target.value, weekdays: current.recurrenceMode === 'weekly' ? [dateFromKey(event.target.value)?.getDay() ?? 1] : current.weekdays }))} /></label><label>종료일<input type="date" value={contextSchedule.endDate} onChange={(event) => setContextSchedule((current) => ({ ...current, endDate: event.target.value }))} /></label><div className="todo-context-menu__time-row"><label>시작 시각<input type="time" step="900" value={contextSchedule.startTime} onChange={(event) => setContextSchedule((current) => ({ ...current, startTime: event.target.value }))} /></label><label>종료 시각<input type="time" step="900" value={contextSchedule.endTime} onChange={(event) => setContextSchedule((current) => ({ ...current, endTime: event.target.value }))} /></label></div><label>예상 소요시간(분)<input type="number" min="5" max="1440" step="5" value={contextSchedule.estimatedMinutes} onChange={(event) => setContextSchedule((current) => ({ ...current, estimatedMinutes: event.target.value }))} /></label><label>반복<select value={contextSchedule.recurrenceMode} onChange={(event) => setContextSchedule((current) => ({ ...current, recurrenceMode: event.target.value as RecurrenceMode }))}><option value="none">반복 없음</option><option value="daily">매일</option><option value="weekly">매주</option><option value="monthly">매월</option><option value="yearly">매년</option></select></label>{contextSchedule.recurrenceMode === 'weekly' && <div className="todo-context-menu__weekdays">{['일', '월', '화', '수', '목', '금', '토'].map((name, weekday) => <button key={name} type="button" className={contextSchedule.weekdays.includes(weekday) ? 'is-active' : ''} onClick={() => setContextSchedule((current) => ({ ...current, weekdays: current.weekdays.includes(weekday) ? current.weekdays.filter((value) => value !== weekday) : [...current.weekdays, weekday] }))}>{name}</button>)}</div>}</> : <label>{contextMenu.editor === 'due' ? '기한 날짜' : '일정 이름'}<input autoFocus type={contextMenu.editor === 'due' ? 'date' : 'text'} value={contextValue} onChange={(event) => setContextValue(event.target.value)} /></label>}
          <div><button type="button" onClick={() => setContextMenu({ ...contextMenu, editor: 'actions' })}>취소</button><button type="submit">저장</button></div>
        </form>}
      </div>}
      {editorOpen && <div className="todo-editor-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) setEditorOpen(false) }}>
        <section className="todo-editor" role="dialog" aria-modal="true" aria-label="새 작업 등록">
          <header><div><span className="todo-section-label">NEW TASK</span><h2>새 작업</h2></div><button type="button" aria-label="닫기" onClick={() => setEditorOpen(false)}>×</button></header>
          <form onSubmit={(event) => { event.preventDefault(); void createTodo() }}>
            <label className="todo-editor__full">작업 이름<input autoFocus type="text" value={editorDraft.title} onChange={(event) => setEditorDraft((current) => ({ ...current, title: event.target.value }))} placeholder="예: 캐릭터 이동 시스템 검증" /></label>
            <PlannerDateField label="시작일" value={editorDraft.startDate} onChange={(value) => setEditorDraft((current) => ({ ...current, startDate: value, endDate: current.endDate < value ? value : current.endDate, weekdays: [dateFromKey(value)?.getDay() ?? 1] }))} />
            <PlannerDateField label="종료일" value={editorDraft.endDate} min={editorDraft.startDate} onChange={(value) => setEditorDraft((current) => ({ ...current, endDate: value }))} />
            <div className="todo-editor__field"><span>시작 시각</span><Dropdown value={editorDraft.startTime} options={TIME_OPTIONS} title="시작 시각" onChange={(value) => setEditorDraft((current) => ({ ...current, startTime: value }))} /></div>
            <div className="todo-editor__field"><span>종료 시각</span><Dropdown value={editorDraft.endTime} options={TIME_OPTIONS} title="종료 시각" onChange={(value) => setEditorDraft((current) => ({ ...current, endTime: value }))} /></div>
            <div className="todo-editor__field"><span>우선순위</span><Dropdown value={editorDraft.priority} options={PRIORITY_OPTIONS} title="우선순위" onChange={(value) => setEditorDraft((current) => ({ ...current, priority: value as Priority }))} /></div>
            <label>예상 소요시간(분)<input type="number" min="5" max="1440" step="5" value={editorDraft.estimatedMinutes} onChange={(event) => setEditorDraft((current) => ({ ...current, estimatedMinutes: event.target.value }))} /></label>
            <div className="todo-editor__field todo-editor__full"><span>반복</span><Dropdown value={editorDraft.recurrenceMode} options={RECURRENCE_OPTIONS} title="반복" onChange={(value) => setEditorDraft((current) => ({ ...current, recurrenceMode: value as RecurrenceMode }))} /></div>
            {editorDraft.recurrenceMode === 'weekly' && <fieldset className="todo-editor__weekdays"><legend>반복 요일</legend>{['일', '월', '화', '수', '목', '금', '토'].map((name, weekday) => <button key={name} type="button" className={editorDraft.weekdays.includes(weekday) ? 'is-active' : ''} onClick={() => setEditorDraft((current) => ({ ...current, weekdays: current.weekdays.includes(weekday) ? current.weekdays.filter((value) => value !== weekday) : [...current.weekdays, weekday] }))}>{name}</button>)}</fieldset>}
            <footer><button type="button" className="btn btn--ghost2" onClick={() => setEditorOpen(false)}>취소</button><button type="submit" className="btn btn--accent" disabled={creating}>{creating ? '등록 중' : '작업 등록'}</button></footer>
          </form>
        </section>
      </div>}
    </div>
  )
}

export default TodoView
