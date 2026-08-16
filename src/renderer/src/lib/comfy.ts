import type { ComfyCheckpointsInfo, ComfyDeviceInfo, ComfyHealthInfo } from '../../../shared/comfy'
import { authHeaders } from './backend'

type JsonObject = Record<string, unknown>

function asObject(value: unknown): JsonObject | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as JsonObject)
    : null
}

function optionalText(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

/** 격리 화면과 Python 프록시에는 외부/LAN이 아닌 loopback HTTP 원점만 넘긴다. */
export function normalizeLocalComfyUrl(raw: string): string | null {
  let url: URL
  try {
    url = new URL(raw.trim())
  } catch {
    return null
  }
  if (url.protocol !== 'http:') return null
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) return null
  if (url.username || url.password || url.search || url.hash) return null
  if (url.pathname && url.pathname !== '/') return null
  const port = url.port ? Number(url.port) : 80
  if (!Number.isInteger(port) || port < 1 || port > 65535) return null
  return url.origin
}

async function getJson(url: string, signal?: AbortSignal): Promise<unknown> {
  const response = await fetch(url, { headers: authHeaders(), signal })
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    // 아래의 공통 오류 메시지로 처리한다.
  }
  if (!response.ok) {
    const detail = optionalText(asObject(body)?.detail)
    throw new Error(detail ?? `Aiso 백엔드 오류 (${response.status})`)
  }
  return body
}

export async function fetchComfyHealth(
  backendPort: number,
  baseUrl: string,
  signal?: AbortSignal
): Promise<ComfyHealthInfo> {
  const normalized = normalizeLocalComfyUrl(baseUrl)
  if (!normalized) throw new Error('ComfyUI 주소는 이 PC의 loopback HTTP 주소여야 합니다.')
  const raw = asObject(
    await getJson(
      `http://127.0.0.1:${backendPort}/comfy/health?base_url=${encodeURIComponent(normalized)}`,
      signal
    )
  )
  if (!raw || typeof raw.online !== 'boolean') {
    throw new Error('ComfyUI 상태 응답 형식이 올바르지 않습니다.')
  }
  const devices: ComfyDeviceInfo[] = Array.isArray(raw.devices)
    ? raw.devices.flatMap((item) => {
        const device = asObject(item)
        if (!device || typeof device.name !== 'string' || typeof device.type !== 'string') return []
        return [{
          name: device.name,
          type: device.type,
          vramTotal: typeof device.vram_total === 'number' ? device.vram_total : undefined,
          vramFree: typeof device.vram_free === 'number' ? device.vram_free : undefined
        }]
      })
    : []
  return {
    online: raw.online,
    baseUrl: optionalText(raw.base_url) ?? normalized,
    version: optionalText(raw.version),
    frontendVersion: optionalText(raw.frontend_version),
    devices,
    detail: optionalText(raw.detail)
  }
}

export async function fetchComfyCheckpoints(
  backendPort: number,
  baseUrl: string,
  signal?: AbortSignal
): Promise<ComfyCheckpointsInfo> {
  const normalized = normalizeLocalComfyUrl(baseUrl)
  if (!normalized) throw new Error('ComfyUI 주소는 이 PC의 loopback HTTP 주소여야 합니다.')
  const raw = asObject(
    await getJson(
      `http://127.0.0.1:${backendPort}/comfy/checkpoints?base_url=${encodeURIComponent(normalized)}`,
      signal
    )
  )
  if (!raw || !Array.isArray(raw.checkpoints) || !raw.checkpoints.every((v) => typeof v === 'string')) {
    throw new Error('ComfyUI 체크포인트 응답 형식이 올바르지 않습니다.')
  }
  return { checkpoints: raw.checkpoints }
}

const IMAGE_REQUEST_SUBJECTS = [
  '이미지', '그림', '캐릭터', '일러스트', '텍스처',
  'image', 'picture', 'illustration', 'texture'
] as const

export function looksLikeImageGenerationRequest(
  text: string,
  _previousAssistant = '',
  previousImageVerified = false
): boolean {
  const normalized = text.toLocaleLowerCase().replace(/\s+/g, ' ').trim()
  let unquoted = normalized
  for (const quoted of [/"[^"\n]*"/g, /(?<!\w)'[^'\n]*'(?!\w)/g, /“[^”\n]*”/g, /‘[^’\n]*’/g]) {
    unquoted = unquoted.replace(quoted, ' ')
  }
  const denialPatterns = [
    /(?:이미지|그림|사진|일러스트|텍스처).{0,24}(?:생성|그리|만들|뽑)(?:하)?지\s*(?:마|말)/,
    /(?:생성|그리|만들|뽑)(?:하)?지\s*(?:마|말)/,
    /(?:이미지|그림|사진|일러스트|텍스처).{0,24}원하지\s*않/,
    /(?:생성|그리|그려|만들|뽑).{0,24}싶지\s*않/,
    /(?:생성|그리|그려|만들|뽑).{0,24}필요(?:는|가)?\s*없/,
    /(?:생성|그리|만들|뽑)(?:하)?지\s*않아도/,
    /(?:이미지|그림|사진|일러스트|텍스처).{0,24}안\s*(?:해도|그려도|만들어도|뽑아도)/,
    /\b(?:do not|don't|never)\s+(?:generate|create|draw)\b/,
    /\b(?:i\s+)?(?:do not|don't)\s+want\s+(?:you\s+to\s+)?(?:generate|create|draw|an?\s+image)/,
    /^no image(?:\s|$)/
  ] as const
  if (denialPatterns.some((pattern) => pattern.test(unquoted))) return false
  const commandText = unquoted.replace(/\s+/g, ' ').trim()

  const metaNouns = [
    '방법', '하는 법', '과정', '절차', '사용법', '튜토리얼',
    '수 있는지', '가능한지', '어떻게 해야', '어떻게 하면'
  ] as const
  const metaEnd = /(?:설명(?:해\s*줘|해주세요|해줘|해\s*주세요)?|알려\s*(?:줘|주세요)|보여\s*(?:줘|주세요)|확인해\s*(?:줘|주세요)|말해\s*(?:줘|주세요)|요약해\s*(?:줘|주세요)|정리해\s*(?:줘|주세요)|번역해\s*(?:줘|주세요)|검토해\s*(?:줘|주세요)|분석해\s*(?:줘|주세요)|문서화해\s*(?:줘|주세요)|뭐야|무엇(?:이야|인가요)?|어디서\s*확인해)\s*[?.!]*$/.test(commandText)
  const proceduralEnd = /(?:방법|하는 법|과정|절차|사용법|튜토리얼|하려면|려면)\s*[?.!]*$/.test(commandText)
  if (proceduralEnd || (metaEnd && metaNouns.some((word) => commandText.includes(word)))) return false
  if (['how to generate', 'how to create', 'how to draw']
    .some((word) => commandText.startsWith(word))) return false

  const softwareRequests = [
    '기능을 만들어', '기능 만들어', '기능 구현', '워크플로를 만들어', '워크플로 만들어',
    '코드를 만들어', '코드 만들어', '모듈을 만들어', '모듈 만들어', '프로그램을 만들어',
    '서비스를 만들어', '플러그인을 만들어', '엔드포인트를 만들어', '앱을 만들어',
    '생성 버튼을 만들어', '생성 모듈을 만들어', '생성 기능을 만들어'
  ] as const
  if (softwareRequests.some((word) => commandText.includes(word))) return false
  if (/(?:그래프|다이어그램|순서도|프로젝트 구조|아키텍처 도식)(?:을|를|으로|로)?\s*(?:그려|만들어|생성)/.test(commandText)
    || /\b(?:draw|create)\s+(?:a\s+)?(?:flowchart|diagram|architecture chart)\b/.test(commandText)) return false

  if (/(?:그려\s*(?:줘|주세요|줄래)|그려서|그린\s*뒤)/.test(commandText)) return true

  const hasSubject = IMAGE_REQUEST_SUBJECTS.some((word) => commandText.includes(word))
    || ['사진', 'artwork', 'photo'].some((word) => commandText.includes(word))
  const generationCommand = /생성\s*(?:(?:좀|(?:한|두|세|네|\d+)\s*(?:번|장|개)(?:만)?|한번(?:만)?|하나(?:만)?)\s*)?(?:해\s*줘|해주세요|해\s*주세요|해\s*줄래|부탁해|부탁드립니다)|생성(?:하고|해서)/.test(commandText)
  const makeCommand = /(?:만들어|뽑아)\s*(?:줘|주세요|줄래)/.test(commandText)
  const nounRequest = /(?:이미지|그림|사진|일러스트|텍스처|캐릭터)(?:를|을)?\s*(?:(?:한\s*장|하나)(?:만)?\s*)?부탁(?:해|드립니다)/.test(commandText)
  if (hasSubject && (generationCommand || makeCommand || nounRequest)) return true

  if (['이미지 생성', '그림 생성', 'image generation']
    .some((word) => commandText.includes(word))) return false

  const englishRequests = [
    'generate ', 'create ', 'draw ', 'please generate ', 'please create ', 'please draw ',
    'can you generate ', 'can you create ', 'can you draw ',
    'could you generate ', 'could you create ', 'could you draw '
  ] as const
  const englishSoftwareRequest = /^(?:please\s+|can you\s+|could you\s+)?create\s+(?:a\s+|an\s+|the\s+)?(?:(?:python|typescript|javascript)\s+)?(?:script|code|program|module|service|plugin|endpoint|feature|generator|api|ui|button)\b/.test(commandText)
    || /^(?:please\s+|can you\s+|could you\s+)?create\s+(?:a\s+|an\s+|the\s+)?image generation\s+(?:feature|module|service|api|ui|button)\b/.test(commandText)
  if (englishSoftwareRequest) return false
  if (hasSubject && englishRequests.some((word) => commandText.startsWith(word))) return true

  // Text is never provenance.  Only a rendered/persisted image-result event
  // may unlock a visual correction request; completion-looking model prose is
  // deliberately ignored here.
  const contextIsImage = previousImageVerified
  const contextualActions = [
    '진행해줘', '진행해 줘', '그걸로 해줘', '그걸로 해 줘', '이걸로 해줘', '이걸로 해 줘',
    '그대로 해줘', '그대로 해 줘', '뽑아줘', '뽑아 줘', '한 장 부탁', '하나 더',
    '바꿔줘', '바꿔 줘', '수정해줘', '수정해 줘', '다시 생성해줘', '다시 생성해 줘',
    'go with that', 'use that one', 'one more', 'regenerate'
  ] as const
  const englishChange = /^(?:please\s+)?change\s+.+\s+to\s+.+[.!]*$/.test(commandText)
  // A correction can be phrased as visual feedback rather than an imperative
  // ("the expression is too dark").  It is actionable only with the verified
  // image-result provenance above; never let completion-looking prose alone
  // turn this into a GPU request.
  const visualSubjects = [
    '표정', '얼굴', '눈', '눈동자', '머리', '헤어', '머리카락', '의상', '옷',
    '포즈', '자세', '배경', '색', '색감', '색상', '체형', '구도', '스타일',
    'expression', 'face', 'eyes', 'eye', 'hair', 'outfit', 'clothes', 'pose',
    'background', 'color', 'colour', 'body', 'composition', 'style'
  ] as const
  const visualCorrection = visualSubjects.some((word) => commandText.includes(word))
    && /(?:너무|더|덜|밝|어둡|같(?:은|아)|처럼|으로|했으면|원하|아니|다르게|too|more|less|brighter|darker|like|rather|instead|should)/i
      .test(commandText)
  return contextIsImage && (
    contextualActions.some((word) => commandText.includes(word))
    || englishChange
    || visualCorrection
  )
}

function abortableDelay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const timer = window.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = (): void => {
      window.clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/** 명확한 Agent 이미지 요청에서 Portable ComfyUI를 필요할 때만 시작하고 준비 완료까지 기다린다. */
export async function ensureComfyReadyForAgent(
  backendPort: number,
  baseUrl: string,
  installPath: string,
  signal?: AbortSignal
): Promise<ComfyHealthInfo> {
  const normalized = normalizeLocalComfyUrl(baseUrl)
  if (!normalized) throw new Error('ComfyUI 주소는 이 PC의 loopback HTTP 주소여야 합니다.')
  const initial = await fetchComfyHealth(backendPort, normalized, signal)
  if (initial.online) return initial
  if (!installPath.trim()) {
    throw new Error(
      'ComfyUI가 실행 중이 아닙니다. 외부 ComfyUI를 직접 시작하거나 설정에서 Windows Portable 폴더를 선택해 주세요.'
    )
  }
  const launch = await window.api.comfy.start()
  if (!launch.ok) throw new Error(launch.detail ?? 'ComfyUI를 시작하지 못했습니다.')

  const deadline = Date.now() + 90_000
  let lastDetail = initial.detail
  while (Date.now() < deadline) {
    await abortableDelay(1_000, signal)
    const health = await fetchComfyHealth(backendPort, normalized, signal)
    if (health.online) return health
    lastDetail = health.detail ?? lastDetail
  }
  throw new Error(
    `90초 안에 ComfyUI가 준비되지 않았습니다.${lastDetail ? ` ${lastDetail}` : ' 실행 로그와 설정 주소를 확인해 주세요.'}`
  )
}
