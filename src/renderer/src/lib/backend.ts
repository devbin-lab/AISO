/** 사이드카 인증 헤더 — 모든 fetch에 실어 백엔드의 X-Aiso-Token 검사를 통과한다.
 *  토큰은 preload(window.api.backend.token)로만 노출되며, 없으면 빈 헤더(개발 편의). */
export function authHeaders(): Record<string, string> {
  const token = window.api?.backend?.token?.() ?? ''
  return token ? { 'X-Aiso-Token': token } : {}
}
