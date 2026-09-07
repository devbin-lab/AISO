import type { AppSettings } from '../shared/settings.ts'

export interface SanitizedSettingsPatch {
  patch: Partial<AppSettings>
  /** 일반 저장 경로에서 무시한 Discord 공급자 요청. 없으면 null. */
  ignoredDiscordProvider: AppSettings['discordLlmProvider'] | null
}

/**
 * 일반 설정 저장 패치에서 `discordLlmProvider` 를 걷어낸다.
 *
 * 이 필드는 전용 동의 IPC(`discord:set-llm-provider`)만 쓴다. 예전에는 일반 저장 경로가
 * 'nvidia' 를 보면 **예외를 던졌는데**, 설정 화면은 폼 전체를 한 덩어리로 보내므로
 * 저장된 값과 폼이 이 한 필드에서만 어긋나도 API 키·모델·봇 켜기까지 전부 저장되지
 * 않았다. 실측으로 사용자의 settings.json 이 2주 넘게 갱신되지 않았고, 화면에는
 * "권한·디스크 상태를 확인하라"는 엉뚱한 안내만 떴다.
 *
 * 무시는 승격이 아니다 — 저장된 값이 그대로 남으므로 NVIDIA 가 몰래 켜지지 않는다.
 * 거절 대신 무시를 고른 이유는, 이 필드가 화면에서 폼으로 편집되는 값이 아니라
 * 저장된 값의 메아리이기 때문이다. 메아리를 이유로 저장 전체를 막을 근거가 없다.
 */
export function sanitizeSettingsPatch(patch: Partial<AppSettings>): SanitizedSettingsPatch {
  if (!patch || !('discordLlmProvider' in patch)) {
    return { patch: patch ?? {}, ignoredDiscordProvider: null }
  }
  const requested = patch.discordLlmProvider ?? null
  const next: Partial<AppSettings> = { ...patch }
  delete next.discordLlmProvider
  return { patch: next, ignoredDiscordProvider: requested }
}
