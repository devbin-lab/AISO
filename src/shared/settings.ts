export type ReasoningEffort = 'low' | 'medium' | 'high'
export type ThemeMode = 'dark' | 'light' | 'system'
/** 생성 온도 모드 (드롭다운으로 선택):
 *  - auto: 매 요청마다 내용을 보고 정리·분류 / 코딩·일반 중 자동 선택
 *  - organize / balanced: 온도값이 코드에 고정(조정 불가)
 *  - custom: 사용자가 슬라이더로 직접 값을 고정(유일하게 조정 가능) */
export type TempPreset = 'auto' | 'organize' | 'balanced' | 'custom'

export interface AppSettings {
  /** Ollama 모델 이름 */
  model: string
  /** Ollama 호스트 (FastAPI 사이드카가 이 주소로 통신) */
  ollamaHost: string
  /** 추론(think) 강도 — think 지원 모델(gemma4·gpt-oss 등)에 적용 */
  reasoningEffort: ReasoningEffort
  /** 현재 생성 온도 모드 (auto·organize·balanced·custom) */
  tempPreset: TempPreset
  /** 커스텀 모드에서 슬라이더로 정하는 온도 (organize·balanced는 코드 고정이라 저장 안 함) */
  tempCustom: number
  /** 컨텍스트 길이 num_ctx — 모델의 작업 기억(토큰 창). 클수록 긴 추론 가능하나 VRAM↑
   *  (4096 · 8192 · 16384 · 32768 · 65536 · 131072 중 하나) */
  contextLength: number
  /** 화면 테마 */
  theme: ThemeMode
  /** 에이전트 작업 폴더 (파일 툴이 이 안으로 confine) */
  workspace: string
  /** RAG 임베딩 모델 — 채팅 모델과 독립. 이 모델로 색인/검색한다. */
  embeddingModel: string
  /** RAG(검색 증강) 사용 — 에이전트가 색인된 작업 폴더에서 관련 조각을 자동 참고 */
  ragEnabled: boolean
  /** RAG 색인 범위 — 색인할 최대 파일 수. 크면 커버리지↑·색인시간↑ (한계 도달 시 늘리라고 권고) */
  ragMaxFiles: number
  /** 자동 주입/검색할 관련 조각 수 */
  ragTopK: number
  /** 모델 상주 유지(keep_alive) — 유휴 시 언로드로 인한 콜드 재로드(~5.8s) 방지.
   *  Ollama 형식: "30m"·"-1"(항상)·"0"(즉시 언로드). 상주는 VRAM을 계속 점유. */
  keepAlive: string
  /** 개발자 모드 — 설정에 공장초기화·온보딩 미리보기 등 개발용 도구를 노출 */
  devMode: boolean
  /** 온보딩(첫 설치 안내) 강제 표시 — 모델을 지우지 않고도 최초 설치 화면을 테스트(개발용) */
  forceOnboarding: boolean
}

/** 정리·분류 / 코딩·일반 고정 온도 — 사용자 조정 불가(코드 상수). */
export const TEMP_FIXED = { organize: 0.2, balanced: 0.5 } as const

/** 생성 온도 모드 드롭다운 옵션 (라벨·설명) — 설정 화면에서 선택용. */
export const TEMP_MODE_OPTIONS: { value: TempPreset; label: string; hint: string }[] = [
  { value: 'auto', label: '자동', hint: '요청 내용을 보고 정리·분류/코딩·일반 중 매번 자동 선택' },
  { value: 'organize', label: '정리·분류', hint: `파일 정리·데이터 추출 (온도 ${TEMP_FIXED.organize} 고정)` },
  { value: 'balanced', label: '코딩·일반', hint: `코딩·일반 작업 (온도 ${TEMP_FIXED.balanced} 고정)` },
  { value: 'custom', label: '커스텀', hint: '슬라이더로 직접 값을 고정' }
]

// 'auto' 분류용 키워드 — 정리 관련 문구면 organize, 그 외(코딩 지시·질문 등)는 balanced로 떨어진다.
const ORGANIZE_KEYWORDS = [
  '정리', '분류', '정돈', '폴더', '옮겨', '이동해', '삭제', '중복', '더미', '미완성',
  '파일 구조', '청소', 'organize', 'clean up', 'cleanup', 'sort files', 'move files',
  'duplicate files', 'delete file'
]

/** 사용자의 마지막 요청 문구로 정리/코딩 중 하나를 고른다('auto' 모드 전용, 키워드 휴리스틱). */
function classifyAutoPreset(text: string): 'organize' | 'balanced' {
  const t = text.toLowerCase()
  return ORGANIZE_KEYWORDS.some((k) => t.includes(k)) ? 'organize' : 'balanced'
}

/** 실제 생성 temperature를 뽑는다. auto는 lastUserText를 분류, organize·balanced는 고정 상수,
 *  custom은 사용자 슬라이더 값. */
export function resolveTemperature(s: AppSettings, lastUserText?: string): number {
  switch (s.tempPreset) {
    case 'auto':
      return TEMP_FIXED[classifyAutoPreset(lastUserText ?? '')]
    case 'organize':
      return TEMP_FIXED.organize
    case 'balanced':
      return TEMP_FIXED.balanced
    case 'custom':
      return typeof s.tempCustom === 'number' ? s.tempCustom : 0.5
    default:
      return 0.5
  }
}

/** RAG 색인 범위 프리셋 (최대 파일 수) — 설정 드롭다운용 */
export const RAG_MAX_FILES_OPTIONS: { value: number; label: string }[] = [
  { value: 100, label: '100개 (빠름)' },
  { value: 200, label: '200개' },
  { value: 300, label: '300개' },
  { value: 1000, label: '1,000개' },
  { value: 3000, label: '3,000개 (많음·느림)' }
]

export const DEFAULT_SETTINGS: AppSettings = {
  model: 'gemma4:12b',
  ollamaHost: 'http://localhost:11434',
  reasoningEffort: 'medium',
  tempPreset: 'auto',
  tempCustom: 0.7,
  contextLength: 16384,
  theme: 'dark',
  workspace: '',
  embeddingModel: 'bge-m3',
  ragEnabled: true,
  ragMaxFiles: 300,
  ragTopK: 5,
  keepAlive: '30m',
  devMode: false,
  forceOnboarding: false
}
