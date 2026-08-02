import type { AppSettings } from '../shared/settings.ts'
import {
  canonicalizeNvidiaBinding,
  sameNvidiaBinding,
  type NvidiaCredentialBinding,
  type NvidiaCredentialBindingInput,
  type NvidiaCredentialStatus,
  type NvidiaExecutionPrepareResult
} from '../shared/nvidia.ts'

export interface NvidiaExecutionPreparationDeps {
  loadSettings: () => AppSettings
  credentialStatus: (binding: NvidiaCredentialBinding) => Promise<NvidiaCredentialStatus>
  readCredential: (binding: NvidiaCredentialBinding) => Promise<string>
  setSidecarCredential: (
    deploymentMode: 'build' | 'nim',
    endpoint: string,
    apiKey: string
  ) => Promise<void>
  bindSidecarNim: (endpoint: string) => Promise<void>
  clearSidecarCredential: () => Promise<void>
  sidecarStatus: () => Promise<Record<string, unknown>>
}

function settingsBinding(settings: AppSettings): NvidiaCredentialBinding | null {
  if (settings.activeLlmProvider !== 'nvidia') return null
  return canonicalizeNvidiaBinding({
    deploymentMode: settings.nvidiaDeploymentMode,
    endpoint: settings.nvidiaDeploymentMode === 'nim' ? settings.nvidiaNimEndpoint : undefined
  })
}

/**
 * Prepare one exact NVIDIA execution target. The API key crosses only this Main-only
 * boundary and is never included in the return value or a Renderer-visible object.
 */
export async function prepareNvidiaExecution(
  requestedInput: NvidiaCredentialBindingInput,
  deps: NvidiaExecutionPreparationDeps
): Promise<NvidiaExecutionPrepareResult> {
  const requested = canonicalizeNvidiaBinding(requestedInput)
  const current = settingsBinding(deps.loadSettings())
  if (!current) throw new Error('현재 선택된 LLM 공급자가 NVIDIA가 아닙니다.')
  if (!sameNvidiaBinding(requested, current)) {
    throw new Error('실행 대상이 현재 NVIDIA 설정과 일치하지 않습니다.')
  }

  const status = await deps.credentialStatus(current)
  let credentialState: NvidiaExecutionPrepareResult['credential']
  if (status.hasStoredCredential && status.matchesCurrentBinding) {
    let apiKey = await deps.readCredential(current)
    try {
      await deps.setSidecarCredential(current.deploymentMode, current.endpoint, apiKey)
    } finally {
      apiKey = ''
    }
    credentialState = 'stored'
  } else if (current.deploymentMode === 'nim') {
    await deps.bindSidecarNim(current.endpoint)
    credentialState = 'not_required'
  } else {
    throw new Error('현재 NVIDIA Build 대상에 맞는 API 키가 없습니다.')
  }

  const after = settingsBinding(deps.loadSettings())
  if (!after || !sameNvidiaBinding(current, after)) {
    await deps.clearSidecarCredential().catch(() => {})
    throw new Error('준비 중 NVIDIA 실행 대상이 변경되어 자격 증명을 폐기했습니다.')
  }

  const sidecar = await deps.sidecarStatus()
  const sidecarBinding = sidecar.binding as Record<string, unknown> | null
  if (
    sidecarBinding?.deploymentMode !== current.deploymentMode ||
    sidecarBinding?.endpoint !== current.endpoint ||
    (credentialState === 'stored' && sidecar.hasCredential !== true) ||
    (credentialState === 'not_required' && sidecar.hasCredential === true)
  ) {
    await deps.clearSidecarCredential().catch(() => {})
    throw new Error('사이드카의 NVIDIA 자격 증명 바인딩을 확인하지 못했습니다.')
  }
  return { ready: true, credential: credentialState }
}
