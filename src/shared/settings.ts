export type ReasoningEffort = 'low' | 'medium' | 'high'
export type ThemeMode = 'dark' | 'light' | 'system'
export type ActiveLlmProvider = 'ollama' | 'nvidia'
export type DiscordLlmProvider = 'ollama' | 'nvidia'
export type { NvidiaDeploymentMode } from './nvidia.ts'
import {
  NVIDIA_BUILD_BASE_URL,
  canonicalizeNvidiaNimEndpoint,
  type NvidiaDeploymentMode
} from './nvidia.ts'
/** ComfyUI 이미지 생성에서 모델을 고르는 주체. */
export type ComfyModelSelectionMode = 'auto' | 'manual'
/** 생성 온도 모드 (드롭다운으로 선택):
 *  - auto: 매 요청마다 내용을 보고 정리·분류 / 코딩·일반 중 자동 선택
 *  - organize / balanced: 온도값이 코드에 고정(조정 불가)
 *  - custom: 사용자가 슬라이더로 직접 값을 고정(유일하게 조정 가능) */
export type TempPreset = 'auto' | 'organize' | 'balanced' | 'custom'

export interface SettingsRecoveryStatus {
  kind: 'none' | 'migrated' | 'quarantined' | 'blocked'
  message?: string
  backupPath?: string
}

export interface AppSettings {
  /** Persisted settings contract. v0.4.x uses schema 4. */
  schemaVersion: 4
  /** Explicit LLM provider. Migration always starts existing users on Ollama. */
  activeLlmProvider: ActiveLlmProvider
  /** Ollama 모델 이름 */
  model: string
  /** Ollama 호스트 (FastAPI 사이드카가 이 주소로 통신) */
  ollamaHost: string
  /** NVIDIA deployment target. Build uses a fixed base URL; NIM is experimental. */
  nvidiaDeploymentMode: NvidiaDeploymentMode
  /** NVIDIA model identifier. Model discovery is added in a later gate. */
  nvidiaModel: string
  /** Canonical user-hosted NIM base URL. Empty until the user configures NIM. */
  nvidiaNimEndpoint: string
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
  /** 채팅 웹 검색 — 켜지면 채팅이 필요할 때 자동으로 web_search·web_fetch로 인터넷을 조사한다.
   *  기본 켜짐(모델이 알아서 판단해 검색). 완전 로컬만 원하면 설정에서 끌 수 있다. */
  chatWebSearch: boolean
  /** 개발자 모드 — 설정에 공장초기화·온보딩 미리보기 등 개발용 도구를 노출 */
  devMode: boolean
  /** 온보딩(첫 설치 안내) 강제 표시 — 모델을 지우지 않고도 최초 설치 화면을 테스트(개발용) */
  forceOnboarding: boolean
  /** 디스코드 봇(MVP: 기본 채팅) — 켜고 토큰만 넣으면 소유자·채널·허용목록은 봇이 자동 처리한다.
   *  봇 토큰은 여기 저장하지 않고 별도로 암호화(safeStorage) 보관한다. */
  discordEnabled: boolean
  /** Discord is an independent opt-in. Existing and migrated installs stay on Ollama. */
  discordLlmProvider: DiscordLlmProvider
  /** 백그라운드 상주 — 켜지면 창을 닫아도 앱이 트레이에 남아 디스코드 봇·예약이 계속 작동한다.
   *  꺼지면 창을 닫을 때 앱이 종료되어 봇도 멈춘다(트레이 '완전 종료'로 언제든 완전 종료 가능). */
  trayResident: boolean
  /** 윈도우 로그인 시 자동 실행 — 켜지면 부팅 후 앱이 트레이로 자동 실행돼 봇이 재연결된다. */
  autoLaunch: boolean
  /** 사용자가 설치한 로컬 ComfyUI 서버 주소. 원격 주소는 연동 계층에서 거부한다. */
  comfyBaseUrl: string
  /** ComfyUI Windows Portable의 최상위 폴더. 비어 있으면 사용자가 직접 실행한 서버에만 연결한다. */
  comfyInstallPath: string
  /**
   * ComfyUI 이미지 생성 모델 선택 방식.
   * - auto: Agent가 등록·검증된 후보 중 요청과 태그를 기준으로 고른다.
   * - manual: 사용자가 Agent 화면에서 고른 정확한 등록 프로필만 실행한다.
   */
  comfyModelSelectionMode: ComfyModelSelectionMode
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

/** ComfyUI 설정 화면의 모델 선택 제어 옵션. */
export const COMFY_MODEL_SELECTION_MODE_OPTIONS: {
  value: ComfyModelSelectionMode
  label: string
  hint: string
}[] = [
  {
    value: 'auto',
    label: '자동 선택',
    hint: 'Agent가 등록·검증된 모델의 태그와 우선순위를 기준으로 선택합니다.'
  },
  {
    value: 'manual',
    label: '직접 선택',
    hint: '이미지 요청 전 사용자가 고른 등록 모델만 사용합니다.'
  }
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
  schemaVersion: 4,
  activeLlmProvider: 'ollama',
  model: 'gemma4:12b',
  ollamaHost: 'http://localhost:11434',
  nvidiaDeploymentMode: 'build',
  nvidiaModel: '',
  nvidiaNimEndpoint: '',
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
  chatWebSearch: true,
  devMode: false,
  forceOnboarding: false,
  discordEnabled: false,
  discordLlmProvider: 'ollama',
  trayResident: false,
  autoLaunch: false,
  comfyBaseUrl: 'http://127.0.0.1:8188',
  comfyInstallPath: '',
  comfyModelSelectionMode: 'auto'
}

export interface LlmExecutionSettingsSnapshot {
  readonly provider: ActiveLlmProvider
  readonly deploymentMode: NvidiaDeploymentMode | null
  readonly endpoint: string
  readonly model: string
}

/**
 * Capture provider/model/endpoint once when an execution starts. Consumers keep this
 * immutable value so later Settings changes only affect the next execution.
 */
export function snapshotLlmSettings(settings: AppSettings): LlmExecutionSettingsSnapshot {
  if (settings.activeLlmProvider === 'ollama') {
    return Object.freeze({
      provider: 'ollama',
      deploymentMode: null,
      endpoint: settings.ollamaHost,
      model: settings.model
    })
  }
  return Object.freeze({
    provider: 'nvidia',
    deploymentMode: settings.nvidiaDeploymentMode,
    endpoint: settings.nvidiaDeploymentMode === 'build'
      ? NVIDIA_BUILD_BASE_URL
      : canonicalizeNvidiaNimEndpoint(settings.nvidiaNimEndpoint),
    model: settings.nvidiaModel
  })
}
