/**
 * 렌더러 창에 붙일 Content-Security-Policy 문자열 조립 — 순수 함수.
 *
 * 렌더러에는 CSP가 전혀 없었다(`grep onHeadersReceived` 0건, index.html에 meta 없음).
 * Electron이 DevTools 콘솔에 "Insecure Content-Security-Policy" 경고를 띄우던 상태다.
 *
 * meta 태그가 아니라 세션 헤더로 붙이는 이유가 둘 있다.
 *  1) index.html은 dev·prod가 공유하는 단일 템플릿이고, dev에서 Vite가 여기에 인라인
 *     react-refresh 스크립트를 주입한다. meta로 script-src 'self'를 박으면 npm run dev가
 *     즉시 죽는다. 헤더는 런타임에 dev/prod를 분기할 수 있다.
 *  2) 더 중요한 이유 — 사이드카 포트는 매 기동마다 달라진다. 헤더 방식이면 **실제 포트**를
 *     connect-src에 정확히 적을 수 있다. 정적 meta로는 http://127.0.0.1:* 와일드카드가
 *     강제되는데, 그건 로컬의 아무 포트나 여는 셈이라 방어 가치가 크게 떨어진다.
 *
 * prod가 file:// 로드라 헤더가 안 먹을 수 있다는 우려는 실측으로 해소했다 —
 * Electron 43에서 onHeadersReceived가 file:// 문서 요청에 발동하고, script-src 'none'을
 * 주면 인라인 스크립트가 실제로 차단된다.
 */

export interface CspInput {
  /** 사이드카 포트. 아직 준비 전이면 null. */
  sidecarPort: number | null
  /** 개발 모드(Vite dev 서버에서 렌더러를 로드) 여부. */
  isDev: boolean
  /** dev에서 렌더러를 로드하는 오리진(ELECTRON_RENDERER_URL). */
  devRendererUrl?: string | null
}

function originOf(url: string): string | null {
  try {
    return new URL(url).origin
  } catch {
    return null
  }
}

export function buildRendererCsp({ sidecarPort, isDev, devRendererUrl }: CspInput): string {
  // 사이드카가 아직 안 떴으면 로컬 연결을 아예 열지 않는다. 준비되면 다시 계산해 붙인다.
  const sidecar = sidecarPort === null ? [] : [`http://127.0.0.1:${sidecarPort}`]

  const script = ["'self'"]
  const style = ["'self'"]
  const connect = [...sidecar]
  // 미리보기 iframe이 사이드카의 /f/ 를 연다. 없으면 미리보기가 통째로 안 뜬다.
  const frame = [...sidecar]

  if (isDev) {
    // Vite가 index.html에 주입하는 react-refresh preamble과, HMR이 만드는 <style> 주입.
    // dev 전용이며 배포 산출물에는 인라인이 하나도 없다(빌드된 index.html은 외부
    // <script src>와 <link rel=stylesheet>뿐).
    script.push("'unsafe-inline'")
    style.push("'unsafe-inline'")
    const devOrigin = devRendererUrl ? originOf(devRendererUrl) : null
    if (devOrigin) {
      script.push(devOrigin)
      style.push(devOrigin)
      connect.push(devOrigin)
      // HMR WebSocket.
      connect.push(devOrigin.replace(/^http/, 'ws'))
    }
  }

  const directives: Array<[string, string[]]> = [
    ['default-src', ["'none'"]],
    ['script-src', script],
    ['style-src', style],
    // 스크린샷은 data:, ComfyUI 결과는 createObjectURL(blob:)로 표시된다.
    ['img-src', ["'self'", 'data:', 'blob:', ...sidecar]],
    ['font-src', ["'self'", 'data:']],
    ['connect-src', connect],
    ['frame-src', frame],
    ['object-src', ["'none'"]],
    ['base-uri', ["'none'"]],
    ['form-action', ["'none'"]]
  ]

  return directives
    // 값이 빈 지시어는 생략한다. connect-src를 빈 채로 두면 파서가 무시할 수 있어
    // 의도와 달리 열리는 것보다, default-src 'none'이 덮게 두는 편이 안전하다.
    .filter(([, values]) => values.length > 0)
    .map(([name, values]) => `${name} ${values.join(' ')}`)
    .join('; ')
}
