export type ReasoningEffort = 'low' | 'medium' | 'high'
export type ThemeMode = 'dark' | 'light' | 'system'
/** 작업 유형별 온도 프리셋 — 정리/분류는 낮게(일관), 창작은 높게(다양) */
export type TempPreset = 'organize' | 'balanced' | 'creative'

export interface AppSettings {
  /** Ollama 모델 이름 */
  model: string
  /** Ollama 호스트 (FastAPI 사이드카가 이 주소로 통신) */
  ollamaHost: string
  /** 추론(think) 강도 — think 지원 모델(gemma4·gpt-oss 등)에 적용 */
  reasoningEffort: ReasoningEffort
  /** 현재 활성 온도 프리셋 — 이 프리셋의 값이 실제 생성 temperature로 쓰인다 */
  tempPreset: TempPreset
  /** 정리·분류·추출 작업 온도 (낮게=일관·정확) */
  tempOrganize: number
  /** 코딩·일반 작업 온도 (균형) */
  tempBalanced: number
  /** 창작·아이디어 작업 온도 (높게=다양·창의) */
  tempCreative: number
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
  /** 자동 주입/검색할 관련 조각 수 */
  ragTopK: number
  /** 모델 상주 유지(keep_alive) — 유휴 시 언로드로 인한 콜드 재로드(~5.8s) 방지.
   *  Ollama 형식: "30m"·"-1"(항상)·"0"(즉시 언로드). 상주는 VRAM을 계속 점유. */
  keepAlive: string
}

/** 온도 프리셋 메타 — id ↔ 저장 필드 ↔ UI 라벨/설명 (설정 화면·대화창 드롭다운 공용) */
export const TEMP_PRESET_META: {
  id: TempPreset
  field: 'tempOrganize' | 'tempBalanced' | 'tempCreative'
  label: string
  hint: string
}[] = [
  { id: 'organize', field: 'tempOrganize', label: '정리·분류', hint: '파일 정리·데이터 추출 — 낮을수록 일관·정확' },
  { id: 'balanced', field: 'tempBalanced', label: '코딩·일반', hint: '코딩·일반 작업 — 균형' },
  { id: 'creative', field: 'tempCreative', label: '창작', hint: '아이디어·글쓰기 — 높을수록 다양·창의' }
]

/** 활성 프리셋의 실제 온도값을 뽑는다 (생성 요청의 temperature로 사용). */
export function resolveTemperature(s: AppSettings): number {
  const field = TEMP_PRESET_META.find((p) => p.id === s.tempPreset)?.field ?? 'tempBalanced'
  const v = s[field]
  return typeof v === 'number' ? v : 0.5
}

export const DEFAULT_SETTINGS: AppSettings = {
  model: 'gemma4:12b',
  ollamaHost: 'http://localhost:11434',
  reasoningEffort: 'medium',
  tempPreset: 'balanced',
  tempOrganize: 0.2,
  tempBalanced: 0.5,
  tempCreative: 0.7,
  contextLength: 16384,
  theme: 'dark',
  workspace: '',
  embeddingModel: 'bge-m3',
  ragEnabled: true,
  ragTopK: 5,
  keepAlive: '30m'
}
