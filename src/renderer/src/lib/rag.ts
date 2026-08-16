import type { AppSettings } from '../../../shared/settings'
import { authHeaders } from './backend'

export interface RagStatus {
  indexed: boolean
  count: number
  files?: number
  embed_model?: string
  dim?: number
  detail?: string
}

export type RagIndexEvent =
  | { type: 'progress'; done: number; total: number; file: string }
  | {
      type: 'done'
      count: number
      files: number
      reused: number
      embed_model: string
      dim: number
      truncated?: boolean
      total_found?: number
      total_found_exact?: boolean
      file_limit_reached?: boolean
    }
  | { type: 'error'; error: string }

/** 작업 폴더 색인 상태 조회. */
export async function ragStatus(port: number, workspace: string): Promise<RagStatus> {
  const res = await fetch(
    `http://127.0.0.1:${port}/rag/status?workspace=${encodeURIComponent(workspace)}`,
    { headers: authHeaders() }
  )
  if (!res.ok) return { indexed: false, count: 0 }
  return (await res.json()) as RagStatus
}

/** 작업 폴더를 색인한다. 진행 이벤트를 콜백으로 흘린다. */
export async function ragIndex(
  port: number,
  settings: AppSettings,
  workspace: string,
  onEvent: (e: RagIndexEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const res = await fetch(`http://127.0.0.1:${port}/rag/index`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      workspace,
      embed_model: settings.embeddingModel,
      ollama_host: settings.ollamaHost,
      max_files: settings.ragMaxFiles
    }),
    signal
  })
  if (!res.ok || !res.body) throw new Error(`색인 백엔드 오류 (HTTP ${res.status})`)

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
        onEvent(JSON.parse(line) as RagIndexEvent)
      } catch {
        /* 불완전 라인 무시 */
      }
    }
  }
}
