/**
 * 연결 상태 점검 — 진단 센터와 홈 대시보드가 **같은 판정**을 쓰도록 한 곳에 둔다.
 *
 * 원래는 DiagnosticCenter 안에 인라인으로 있었다. 홈에도 같은 표가 필요해졌는데,
 * 복사하면 두 화면이 서로 다른 답을 내는 순간이 반드시 온다(한쪽만 고치게 된다).
 * 그래서 판정 로직만 빼서 공유하고, 표시 방식은 각 화면이 정한다.
 */

import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import type { AppSettings } from '../../../shared/settings'
import { authHeaders } from './backend'

export type CheckState = 'ok' | 'warning' | 'error' | 'info'

export interface Check {
  id: string
  label: string
  state: CheckState
  detail: string
}

/**
 * 'info' 는 "쓰지 않거나 아직 안 붙었다" 는 뜻이다 — 오류도 경고도 아니다.
 * 디스코드를 안 쓰는 사용자에게 '확인 필요' 를 띄우면 쓰지도 않는 기능으로 잔소리가 된다.
 *
 * 다만 예전 라벨 '정보' 는 상태가 아니라 **분류 이름**이라 아무것도 알려주지 않았다.
 * 진단 센터는 아래 설명줄이 있어 견뎠지만 홈 카드는 상태 단어만 보여준다.
 * 두 'info' 생산자(ComfyUI·디스코드)가 모두 "연결 안 됨" 을 뜻하므로 그렇게 적는다.
 */
export function stateLabel(state: CheckState): string {
  return state === 'ok' ? '정상' : state === 'warning' ? '확인 필요' : state === 'error' ? '오류' : '미연결'
}

export function backendUrl(backend: BackendInfo, path: string): string | null {
  return backend.state === 'ready' && backend.port != null ? `http://127.0.0.1:${backend.port}${path}` : null
}

/** 상태 묶음을 한 줄로 요약한다 — 나쁜 소식이 먼저다. */
export function summarizeChecks(checks: Check[]): string {
  const failed = checks.filter((check) => check.state === 'error').length
  const warnings = checks.filter((check) => check.state === 'warning').length
  return failed ? `오류 ${failed}` : warnings ? `확인 필요 ${warnings}` : '핵심 연결 정상'
}

/**
 * 백엔드·LLM·ComfyUI·디스코드 연결을 실제로 확인한다.
 *
 * 네트워크를 타는 점검이 섞여 있으므로 호출자가 호출 시점을 정한다 — 주기적으로
 * 부르면 설정 화면을 열어 둔 것만으로 외부 요청이 반복된다.
 */
export async function collectConnectionChecks(
  backend: BackendInfo,
  health: HealthInfo | null,
  settings: AppSettings
): Promise<Check[]> {
  const next: Check[] = []
  next.push({
    id: 'backend',
    label: 'Aiso 백엔드',
    state: backend.state === 'ready' ? 'ok' : backend.state === 'starting' ? 'warning' : 'error',
    detail: backend.state === 'ready'
      ? `로컬 사이드카가 포트 ${backend.port}에서 준비되었습니다.`
      : backend.detail || '백엔드를 준비하고 있습니다.'
  })

  if (settings.activeLlmProvider === 'ollama') {
    next.push({
      id: 'llm',
      label: '로컬 LLM (Ollama)',
      state: health?.ollama ? 'ok' : 'warning',
      detail: health?.ollama
        ? `${health.models.length}개 모델을 확인했습니다.`
        : health?.detail || 'Ollama 연결 또는 모델 목록을 확인하지 못했습니다.'
    })
  } else {
    try {
      const credential = await window.api.nvidia.credential.status({
        deploymentMode: settings.nvidiaDeploymentMode,
        endpoint: settings.nvidiaDeploymentMode === 'nim' ? settings.nvidiaNimEndpoint : undefined
      })
      next.push({
        id: 'llm',
        label: 'NVIDIA LLM 연결',
        state: credential.usableForCurrentBinding ? 'ok' : 'warning',
        detail: credential.usableForCurrentBinding
          ? `API 키와 ${settings.nvidiaModel || '선택 모델'} 구성이 준비되었습니다.`
          : '현재 배포 대상에 사용할 수 있는 API 키 또는 모델 설정을 확인하세요.'
      })
    } catch {
      next.push({
        id: 'llm',
        label: 'NVIDIA LLM 연결',
        state: 'warning',
        detail: '보안 저장소의 NVIDIA 키 상태를 확인하지 못했습니다.'
      })
    }
  }

  if (backend.state === 'ready' && backend.port != null && settings.comfyBaseUrl) {
    try {
      const response = await fetch(
        `${backendUrl(backend, '/comfy/health')}?base_url=${encodeURIComponent(settings.comfyBaseUrl)}`,
        { headers: authHeaders() }
      )
      // response.ok 는 **사이드카가 답했다**는 뜻일 뿐이다. ComfyUI 가 아예 없어도
      // /comfy/health 는 200 에 { online: false } 를 담아 돌려준다. 그래서 예전에는
      // ComfyUI 를 설치조차 하지 않은 컴퓨터에서 '정상'으로 떴다. 본문을 읽어야 한다.
      const health = response.ok
        ? (await response.json().catch(() => null)) as { online?: boolean; detail?: string } | null
        : null
      const online = health?.online === true
      next.push({
        id: 'comfy',
        label: 'ComfyUI 연결',
        state: online ? 'ok' : 'warning',
        detail: online
          ? '설정한 ComfyUI 서버에 응답이 있습니다.'
          : health?.detail?.trim()
            || (response.ok
              ? 'ComfyUI 서버가 응답하지 않습니다. ComfyUI를 실행했는지 확인하세요.'
              : 'ComfyUI 서버 응답을 확인하지 못했습니다.')
      })
    } catch {
      next.push({
        id: 'comfy',
        label: 'ComfyUI 연결',
        state: 'warning',
        detail: 'ComfyUI 연결을 확인하지 못했습니다.'
      })
    }
  } else {
    next.push({
      id: 'comfy',
      label: 'ComfyUI 연결',
      state: 'info',
      detail: 'ComfyUI를 연결하지 않았거나 백엔드를 준비 중입니다.'
    })
  }

  try {
    const discord = await window.api.discord.status()
    next.push({
      id: 'discord',
      label: '디스코드 봇',
      state: discord.running ? 'ok' : 'info',
      detail: discord.running
        ? '봇이 Discord에 연결되어 있습니다.'
        : '연결하지 않았거나 현재 중지되어 있습니다.'
    })
  } catch {
    next.push({
      id: 'discord',
      label: '디스코드 봇',
      state: 'warning',
      detail: '디스코드 연결 상태를 확인하지 못했습니다.'
    })
  }
  return next
}
