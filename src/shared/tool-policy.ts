export const AGENT_TOOL_IDS = [
  'update_plan',
  'get_system_time',
  'list_calendar_events',
  'create_calendar_event',
  'manage_calendar_event',
  'list_mydb_library',
  'list_mydb_history',
  'list_mydb_trash',
  'restore_mydb_trash_node',
  'list_dir',
  'list_tree',
  'read_file',
  'convert_document',
  'analyze_document_calendar',
  'grep',
  'glob',
  'create_dir',
  'move',
  'write_file',
  'edit_file',
  'multi_edit',
  'write_code_file',
  'edit_code_file',
  'multi_edit_code_file',
  'delete_file',
  'delete_dir',
  'run_web',
  'run_code',
  'run_command',
  'web_fetch',
  'web_search',
  'create_skill',
  'run_skill',
  'search_docs',
  'discord_server_map',
  'discord_server_apply',
  'discord_send',
  'discord_schedule_add',
  'discord_channel_report_add',
  'discord_schedule_list',
  'discord_schedule_remove',
  'generate_image'
] as const

export type AgentToolId = typeof AGENT_TOOL_IDS[number]

/** 프로젝트 코드 작성·편집·실행을 한 번에 제어하는 UI 편의 그룹입니다. */
export const PROGRAMMING_AGENT_TOOL_IDS = [
  'write_code_file',
  'edit_code_file',
  'multi_edit_code_file',
  'run_web',
  'run_code',
  'run_command'
] as const satisfies readonly AgentToolId[]

/** 현재 데스크톱 NVIDIA Agent의 Main 승인 manifest가 지원하는 도구입니다. */
export const NVIDIA_SUPPORTED_AGENT_TOOL_IDS = [
  'update_plan',
  'get_system_time',
  'list_calendar_events',
  'create_calendar_event',
  'manage_calendar_event',
  'list_mydb_library',
  'list_mydb_history',
  'list_mydb_trash',
  'restore_mydb_trash_node',
  'list_dir',
  'list_tree',
  'read_file',
  'convert_document',
  'analyze_document_calendar',
  'grep',
  'glob',
  'create_dir',
  'move',
  'write_file',
  'edit_file',
  'multi_edit',
  'write_code_file',
  'edit_code_file',
  'multi_edit_code_file',
  'delete_file',
  'delete_dir',
  'run_web',
  'run_code',
  'run_command',
  'search_docs',
  'generate_image'
] as const satisfies readonly AgentToolId[]

const PROGRAMMING_SET = new Set<AgentToolId>(PROGRAMMING_AGENT_TOOL_IDS)
const NVIDIA_SUPPORTED_SET = new Set<AgentToolId>(NVIDIA_SUPPORTED_AGENT_TOOL_IDS)

/** 새 설치와 이전 설정 마이그레이션은 프로젝트 프로그래밍 기능을 안전하게 끈 상태로 시작합니다. */
export const DEFAULT_ENABLED_AGENT_TOOL_IDS: readonly AgentToolId[] = AGENT_TOOL_IDS.filter(
  (toolId) => !PROGRAMMING_SET.has(toolId)
)

export interface AgentToolPolicy {
  ollama: AgentToolId[]
  nvidia: AgentToolId[]
}

export function createDefaultAgentToolPolicy(): AgentToolPolicy {
  return {
    ollama: [...DEFAULT_ENABLED_AGENT_TOOL_IDS],
    nvidia: DEFAULT_ENABLED_AGENT_TOOL_IDS.filter((toolId) => NVIDIA_SUPPORTED_SET.has(toolId))
  }
}

/** Legacy persisted IDs are migrated to the canonical calendar tool IDs. */
const LEGACY_CALENDAR_TOOL_ID_ALIASES: Readonly<Record<string, AgentToolId>> = {
  list_saved_todos: 'list_calendar_events',
  create_todo_event: 'create_calendar_event',
  manage_todo: 'manage_calendar_event',
  analyze_document_todos: 'analyze_document_calendar'
}

/**
 * My DB is now a personal library, not an Agent audit trail.  Retire the
 * history-reader ID from persisted v7 policies before their normal validation
 * runs.  Keep this migration intentionally narrow: unknown IDs still fail
 * closed instead of being silently accepted.
 */
export function removeRetiredAgentTools(value: unknown): unknown {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value
  const policy = value as Record<string, unknown>
  const withoutHistory = (items: unknown): unknown => Array.isArray(items)
    ? items.filter((item) => item !== 'list_change_history')
    : items
  return {
    ...policy,
    ollama: withoutHistory(policy.ollama),
    nvidia: withoutHistory(policy.nvidia)
  }
}

/** Add Aiso-owned central-data tools once when upgrading a stored tool policy. */
export function addSavedTodoToolToPolicy(policy: AgentToolPolicy): AgentToolPolicy {
  const add = (tools: readonly AgentToolId[], provider: keyof AgentToolPolicy): AgentToolId[] =>
    AGENT_TOOL_IDS.filter((toolId) => tools.includes(toolId) || (
      (
        toolId === 'list_calendar_events' || toolId === 'create_calendar_event' || toolId === 'manage_calendar_event' ||
        toolId === 'list_mydb_library' || toolId === 'list_mydb_history' ||
        toolId === 'list_mydb_trash' || toolId === 'restore_mydb_trash_node'
      ) && agentToolSupportedByProvider(toolId, provider)
    ))
  return {
    ollama: add(policy.ollama, 'ollama'),
    nvidia: add(policy.nvidia, 'nvidia')
  }
}

export function isAgentToolId(value: unknown): value is AgentToolId {
  return typeof value === 'string' && (AGENT_TOOL_IDS as readonly string[]).includes(value)
}

export function canonicalizeEnabledAgentTools(
  value: unknown,
  supported: ReadonlySet<AgentToolId> = new Set(AGENT_TOOL_IDS)
): AgentToolId[] {
  if (!Array.isArray(value)) throw new Error('활성 도구 목록은 배열이어야 합니다.')
  const requested = new Set<AgentToolId>()
  for (const rawItem of value) {
    const item = typeof rawItem === 'string' && rawItem in LEGACY_CALENDAR_TOOL_ID_ALIASES
      ? LEGACY_CALENDAR_TOOL_ID_ALIASES[rawItem]
      : rawItem
    if (!isAgentToolId(item)) throw new Error(`지원하지 않는 Agent 도구입니다: ${String(item)}`)
    if (!supported.has(item)) throw new Error(`현재 공급자에서 지원하지 않는 Agent 도구입니다: ${item}`)
    if (requested.has(item)) throw new Error(`Agent 도구가 중복되었습니다: ${item}`)
    requested.add(item)
  }
  return AGENT_TOOL_IDS.filter((toolId) => requested.has(toolId))
}

export function canonicalizeAgentToolPolicy(value: unknown): AgentToolPolicy {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Agent 도구 정책 형식이 올바르지 않습니다.')
  }
  const policy = value as Record<string, unknown>
  if (Object.keys(policy).some((key) => key !== 'ollama' && key !== 'nvidia')) {
    throw new Error('Agent 도구 정책에 허용되지 않은 항목이 있습니다.')
  }
  return {
    ollama: canonicalizeEnabledAgentTools(policy.ollama),
    nvidia: canonicalizeEnabledAgentTools(policy.nvidia, NVIDIA_SUPPORTED_SET)
  }
}

export function agentToolSupportedByProvider(
  toolId: AgentToolId,
  provider: keyof AgentToolPolicy
): boolean {
  return provider === 'ollama' || NVIDIA_SUPPORTED_SET.has(toolId)
}
