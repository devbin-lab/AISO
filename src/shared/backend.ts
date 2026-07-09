export type BackendState = 'starting' | 'ready' | 'error' | 'stopped'

export interface BackendInfo {
  state: BackendState
  port: number | null
  detail?: string
}

export interface HealthInfo {
  ollama: boolean
  models: string[]
  detail?: string
}
