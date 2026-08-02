import type { LlmModelCapabilities, LlmModelListResult } from './llm.ts'

export const NVIDIA_BUILD_BASE_URL = 'https://integrate.api.nvidia.com/v1' as const

export type NvidiaDeploymentMode = 'build' | 'nim'

export interface NvidiaCredentialBindingInput {
  deploymentMode: NvidiaDeploymentMode
  /** User NIM only. Build always uses NVIDIA_BUILD_BASE_URL. */
  endpoint?: string
}

export interface NvidiaCredentialBinding {
  deploymentMode: NvidiaDeploymentMode
  endpoint: string
}

export interface NvidiaCredentialSaveRequest extends NvidiaCredentialBindingInput {
  apiKey: string
}

export interface NvidiaCredentialStatus {
  encryptionAvailable: boolean
  hasStoredCredential: boolean
  matchesCurrentBinding: boolean
  detail?: string
}

export interface NvidiaExecutionPrepareResult {
  ready: true
  credential: 'stored' | 'not_required'
}

export interface NvidiaCapabilityTargetInput extends NvidiaCredentialBindingInput {
  model: string
}

export interface NvidiaCapabilitySnapshot {
  schemaVersion: 1
  binding: NvidiaCredentialBinding
  model: string
  capabilities: LlmModelCapabilities
  checkedAt: string
}

export type NvidiaModelListResult = LlmModelListResult

function isLoopbackHostname(hostname: string): boolean {
  const host = hostname.toLowerCase()
  if (host === 'localhost' || host.endsWith('.localhost')) return true
  if (host === '[::1]') return true
  return /^127(?:\.[0-9]{1,3}){3}$/.test(host)
}

/**
 * Canonicalize a user-hosted NIM base URL without performing DNS or network I/O.
 * Loopback development endpoints may use HTTP; every other endpoint must use HTTPS.
 */
export function canonicalizeNvidiaNimEndpoint(input: string): string {
  const candidate = String(input ?? '').trim()
  if (!candidate) throw new Error('사용자 NIM 주소를 입력해 주세요.')
  if (candidate.includes('?') || candidate.includes('#')) {
    throw new Error('사용자 NIM 주소에는 query 또는 fragment를 사용할 수 없습니다.')
  }

  let parsed: URL
  try {
    parsed = new URL(candidate)
  } catch {
    throw new Error('사용자 NIM 주소 형식이 올바르지 않습니다.')
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('사용자 NIM 주소는 HTTP 또는 HTTPS URL이어야 합니다.')
  }
  if (!parsed.hostname || parsed.username || parsed.password) {
    throw new Error('사용자 정보가 포함된 NIM 주소는 사용할 수 없습니다.')
  }
  if (parsed.search || parsed.hash) {
    throw new Error('사용자 NIM 주소에는 query 또는 fragment를 사용할 수 없습니다.')
  }
  if (parsed.protocol === 'http:' && !isLoopbackHostname(parsed.hostname)) {
    throw new Error('로컬호스트가 아닌 사용자 NIM은 HTTPS만 사용할 수 있습니다.')
  }

  // URL performs host casing, IPv4 and default-port normalization. Keep an optional
  // API base path, but store one stable representation without trailing slashes.
  const path = parsed.pathname.replace(/\/+$/, '')
  return `${parsed.origin}${path}`
}

export function canonicalizeNvidiaBinding(
  input: NvidiaCredentialBindingInput
): NvidiaCredentialBinding {
  if (input.deploymentMode === 'build') {
    if (input.endpoint) {
      let supplied: string
      try {
        supplied = canonicalizeNvidiaNimEndpoint(input.endpoint)
      } catch {
        throw new Error('NVIDIA Build 주소는 변경할 수 없습니다.')
      }
      if (supplied !== NVIDIA_BUILD_BASE_URL) {
        throw new Error('NVIDIA Build 주소는 변경할 수 없습니다.')
      }
    }
    return { deploymentMode: 'build', endpoint: NVIDIA_BUILD_BASE_URL }
  }
  if (input.deploymentMode !== 'nim') {
    throw new Error('지원하지 않는 NVIDIA 배포 방식입니다.')
  }
  return {
    deploymentMode: 'nim',
    endpoint: canonicalizeNvidiaNimEndpoint(input.endpoint ?? '')
  }
}

export function sameNvidiaBinding(
  left: NvidiaCredentialBinding,
  right: NvidiaCredentialBinding
): boolean {
  return left.deploymentMode === right.deploymentMode && left.endpoint === right.endpoint
}
