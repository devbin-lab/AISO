/** Ollama 모델 목록·다운로드(pull) 유틸 — 온보딩/에이전트에서 공용. */
import { authHeaders } from './backend'

/** 태그(:latest 등)를 무시하고 모델 설치 여부 판정.
 *  설정값은 'bge-m3'(태그 없음)인데 Ollama 목록은 'bge-m3:latest'라 단순 includes로는 안 잡힌다. */
export function modelInstalled(models: string[], name: string): boolean {
  if (!name) return false
  const norm = (m: string): string => (m.includes(':') ? m : `${m}:latest`)
  const target = norm(name)
  return models.some((m) => norm(m) === target)
}

export interface UnloadResult {
  ok: boolean
  unloaded: string[]
  detail?: string
}

/** 로드된 Ollama 모델을 즉시 VRAM에서 내린다(긴급 GPU 확보). 내려간 모델 이름을 돌려준다. */
export async function unloadModels(
  port: number,
  ollamaHost: string | undefined
): Promise<UnloadResult> {
  const res = await fetch(`http://127.0.0.1:${port}/ollama/unload`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ ollama_host: ollamaHost })
  })
  if (!res.ok) throw new Error(`언로드 백엔드 오류 (HTTP ${res.status})`)
  return (await res.json()) as UnloadResult
}

export type PullEvent =
  | { type: 'progress'; status: string; total?: number; completed?: number }
  | { type: 'done'; model: string }
  | { type: 'error'; error: string }

/** Ollama 모델을 내려받으며 진행 이벤트를 콜백으로 흘린다(NDJSON 스트림). */
export async function pullModel(
  port: number,
  model: string,
  ollamaHost: string | undefined,
  onEvent: (e: PullEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const res = await fetch(`http://127.0.0.1:${port}/ollama/pull`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ model, ollama_host: ollamaHost }),
    signal
  })
  if (!res.ok || !res.body) throw new Error(`다운로드 백엔드 오류 (HTTP ${res.status})`)

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx).trim()
      buf = buf.slice(idx + 1)
      if (!line) continue
      try {
        onEvent(JSON.parse(line) as PullEvent)
      } catch {
        /* 불완전 라인 무시 */
      }
    }
  }
}
