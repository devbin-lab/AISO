// 디스코드 봇 상태 — 사이드카(discordbot.py)가 /discord/status로 돌려주는 런타임 상태.
// 소유자·서버·채널·허용목록은 봇이 자동 판별/관리하므로 설정이 아니라 '상태'로 표시한다.
export interface DiscordStatus {
  running: boolean
  user?: string | null
  owner_id?: string
  app_id?: string
  guild_id?: string
  channel_id?: string
  allowlist?: string[]
  last_error?: string | null
  detail?: string
}

// 등록된 예약 1건 — 사이드카(discordsched.py)가 /discord/schedules로 돌려주는 형태.
export interface DiscordSchedule {
  id: string
  kind: 'message' | 'briefing' | 'channel_report'
  channel_name: string
  text: string
  repeat: 'once' | 'daily' | 'interval'
  interval_hours?: number
  source_channels?: Array<{ id: string; name: string; last_message_id: string }>
  last_reported_at?: string
  next_run: string // ISO(YYYY-MM-DDTHH:MM)
  created?: string
}
