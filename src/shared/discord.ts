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
