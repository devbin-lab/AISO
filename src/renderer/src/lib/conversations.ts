/** 새 대화 id. */
export function newConversationId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : String(Math.random()).slice(2)
}

/** 첫 사용자 메시지에서 대화 제목 생성(공백 정리 후 40자로 자름). */
export function titleFromText(text: string): string {
  const t = text.trim().replace(/\s+/g, ' ')
  if (!t) return '새 대화'
  return t.length > 40 ? `${t.slice(0, 40)}…` : t
}

/** 상대 시간 표시. */
export function relTime(ms: number): string {
  const s = Math.floor((Date.now() - ms) / 1000)
  if (s < 60) return '방금'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}분 전`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}시간 전`
  return `${Math.floor(h / 24)}일 전`
}
