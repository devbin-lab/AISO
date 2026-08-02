export type LlmCapabilityState = 'supported' | 'unsupported' | 'unknown'

export interface LlmModelCapabilities {
  chat: LlmCapabilityState
  stream: LlmCapabilityState
  tools: LlmCapabilityState
}

export interface LlmModelListResult {
  models: string[]
  refreshedAt: string
}
