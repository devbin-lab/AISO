// 디스코드 봇 상태 — 사이드카(discordbot.py)가 /discord/status로 돌려주는 런타임 상태.
// 소유자·서버·채널·허용목록은 봇이 자동 판별/관리하므로 설정이 아니라 '상태'로 표시한다.
/** 봇이 붙어 있는 서버 하나의 상태. */
export interface DiscordGuildStatus {
  guild_id: string
  guild_name: string
  /** 그 서버의 명령 채널. 비어 있으면 그 서버에서는 아직 대화할 수 없다. */
  channel_id: string
  allowlist: string[]
}

export interface DiscordStatus {
  running: boolean
  user?: string | null
  owner_id?: string
  app_id?: string
  /**
    * 붙어 있는 서버 전부. 봇은 여러 서버에서 동시에 동작하며, 서버마다 명령 채널과
    * 허용목록을 따로 둔다 — 한 서버에서 허용한 사용자가 다른 서버를 조작할 수 없다.
    */
  guilds?: DiscordGuildStatus[]
  /** 첫 서버의 값. 서버가 하나뿐인 화면·도구가 그대로 동작하도록 함께 싣는다. */
  guild_id?: string
  guild_name?: string
  channel_id?: string
  allowlist?: string[]
  /**
   * 봇에 **실제로 주입된** 대화 모델 공급자와 모델. 저장된 설정이 아니라 지금 돌고 있는 값이다.
   *
   * 둘은 어긋날 수 있다(설정 저장 실패, 전용 동의 흐름으로만 바뀌는 공급자 등). 그때
   * 저장 파일만 봐서는 "로컬로 도는 건지 NVIDIA로 도는 건지" 확인할 길이 없었다.
   * 봇이 꺼져 있으면 비어 있다 — 멈춘 봇이 공급자를 표시하면 그 자체가 거짓말이다.
   */
  provider?: string
  model?: string
  last_error?: string | null
  detail?: string
}

/**
 * branch 필드의 '모든 브랜치' 표식(discordsched.ALL_BRANCHES).
 *
 * git 이 브랜치 이름으로 허용하지 않는 문자라 실제 ref 와 헷갈릴 일이 없다.
 */
export const ALL_BRANCHES = '*'

// 등록된 예약 1건 — 사이드카(discordsched.py)가 /discord/schedules로 돌려주는 형태.
export interface DiscordSchedule {
  id: string
  kind: 'message' | 'briefing' | 'channel_report' | 'repo_report'
  channel_name: string
  text: string
  repeat: 'once' | 'daily' | 'interval'
  interval_hours?: number
  /** 저장소 보고가 매일 고정 시각으로 돌 때의 'HH:MM'. repeat 이 daily 일 때만. */
  daily_at?: string
  source_channels?: Array<{ id: string; name: string; last_message_id: string }>
  /**
   * 저장소 보고 — 등록 시점에 고정된 클론 경로와 로그 대상 ref(예: origin/main).
   * branch 가 ALL_BRANCHES('*') 면 한 브랜치가 아니라 원격의 모든 브랜치를 함께 본다.
   */
  repo_path?: string
  branch?: string
  /**
   * 모든 브랜치 모드의 ref 별 기준 커밋.
   *
   * 브랜치가 각자 다른 속도로 움직이므로 sha 하나로는 "어디까지 봤는가"를 적을 수 없다.
   */
  branch_cursors?: Record<string, string>
  /**
   * 저장소 보고가 마지막으로 보고를 마친 커밋 — "지금 어디까지 봤는가".
   *
   * 이 기능은 새 커밋이 없으면 아무 말도 하지 않는 것이 정상이다. 그래서 화면에
   * 이 값이 없으면 사용자는 '조용해서 정상'과 '고장나서 조용함'을 구분할 수 없다.
   */
  last_commit?: string
  last_reported_at?: string
  /**
   * 마지막 시도가 실패한 사유. 성공하면 비워진다.
   *
   * 실패는 명령 채널(#aiso)로도 알리지만, 같은 사유가 이어지면 한 번만 알린다 —
   * 며칠 뒤에 화면을 보는 사람에게는 이 값이 유일한 단서다.
   */
  last_failure?: string
  /** 같은 저장소·브랜치의 이전 예약이 본 지점을 이어받았으면 그 시각. 등록 응답에만 의미가 있다. */
  resumed_from?: string
  next_run: string // ISO(YYYY-MM-DDTHH:MM)
  created?: string
}

/** 봇이 실제로 보는 서버 하나와 그 글 채널들 — 보고 채널 선택 목록을 채운다. */
export interface DiscordGuildChannels {
  guild_id: string
  guild_name: string
  channels: Array<{ id: string; name: string }>
  command_channel_id: string
}

/**
 * 저장소가 가진 브랜치 목록.
 *
 * 사람이 브랜치를 손으로 적으면 `main` 과 `origin/main` 을 혼동한다. fetch 가 움직이는
 * 것은 `origin/main` 이라, 로컬을 고르면 다른 사람이 올린 커밋이 하나도 잡히지 않는다.
 * 그런데 새 커밋이 없을 때 침묵하는 것이 정상 동작이므로 그 실수는 드러나지 않는다.
 */
export interface RepoBranches {
  ok: boolean
  repo_path?: string
  /** 지금 체크아웃된 로컬 브랜치. 분리된 HEAD 면 비어 있다. */
  current?: string
  local?: string[]
  remote?: string[]
  /** 먼저 권할 ref — 거의 언제나 원격 추적 ref 다. */
  recommended?: string
  /** 목록은 만들었지만 원격을 새로 받지 못했을 때의 사유. */
  warning?: string
  detail?: string
}

export interface RepoReportInput {
  repoPath: string
  branch: string
  guildId: string
  channelId: string
  /** 둘 중 하나. dailyAt('HH:MM')이 있으면 매일 그 시각, 없으면 intervalHours 마다. */
  intervalHours?: number
  dailyAt?: string
  instruction: string
}
