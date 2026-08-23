import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DEFAULT_SETTINGS } from '../../../shared/settings'
import type { BackendInfo } from '../../../shared/backend'
import { collectConnectionChecks } from './diagnostics'

/**
 * ComfyUI 연결 판정.
 *
 * 증상: ComfyUI 를 **설치조차 하지 않은** 컴퓨터에서 홈 카드와 진단 센터가 모두
 * '정상'으로 표시했다. 처음 세팅하는 사람에게 가장 나쁜 종류의 거짓말이다 —
 * 준비가 됐다고 믿고 이미지 생성을 시도하게 된다.
 *
 * 원인: 판정이 `response.ok` 만 봤다. 그건 **사이드카가 답했다**는 뜻일 뿐이고,
 * 사이드카의 /comfy/health 는 ComfyUI 가 죽어 있어도 200 에 { online: false } 를
 * 담아 돌려준다(python/comfy_client.py 의 _offline). 그래서 늘 참이었다.
 */

const READY: BackendInfo = { state: 'ready', port: 49155 }

function comfyCheckOf(checks: Awaited<ReturnType<typeof collectConnectionChecks>>) {
  return checks.find((check) => check.id === 'comfy')
}

/** ComfyUI 외의 점검은 이 테스트의 관심사가 아니므로 조용히 실패시킨다. */
function installFetch(comfyBody: unknown, options: { ok?: boolean } = {}): void {
  vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
    const url = String(input)
    if (url.includes('/comfy/health')) {
      return {
        ok: options.ok ?? true,
        json: async () => comfyBody
      } as unknown as Response
    }
    throw new Error('unrelated check')
  }))
}

beforeEach(() => {
  vi.stubGlobal('window', {
    ...(globalThis as { window?: unknown }).window as object,
    api: {
      nvidia: { hasCredential: async () => false },
      discord: { status: async () => ({ connected: false }) }
    }
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ComfyUI 연결 판정', () => {
  const settings = { ...DEFAULT_SETTINGS, comfyBaseUrl: 'http://127.0.0.1:8188' }

  it('설치되지 않아 online:false 면 정상이 아니다', async () => {
    // 사이드카는 200 으로 답한다. 그것만 보면 안 된다.
    installFetch({ online: false, detail: 'ComfyUI 서버에 연결할 수 없습니다.' })
    const comfy = comfyCheckOf(await collectConnectionChecks(READY, null, settings))
    expect(comfy?.state).not.toBe('ok')
    expect(comfy?.state).toBe('warning')
  })

  it('online:false 일 때 사이드카가 준 사유를 그대로 보여 준다', async () => {
    installFetch({ online: false, detail: 'ComfyUI 서버에 연결할 수 없습니다.' })
    const comfy = comfyCheckOf(await collectConnectionChecks(READY, null, settings))
    expect(comfy?.detail).toContain('연결할 수 없습니다')
  })

  it('사유가 비어 있어도 무엇을 해야 하는지 알려 준다', async () => {
    installFetch({ online: false, detail: '' })
    const comfy = comfyCheckOf(await collectConnectionChecks(READY, null, settings))
    expect(comfy?.state).toBe('warning')
    expect(comfy?.detail).toContain('ComfyUI를 실행했는지')
  })

  it('실제로 살아 있을 때만 정상이다', async () => {
    installFetch({ online: true, version: '0.3.0', devices: [] })
    const comfy = comfyCheckOf(await collectConnectionChecks(READY, null, settings))
    expect(comfy?.state).toBe('ok')
  })

  it('본문이 JSON 이 아니어도 정상으로 넘기지 않는다', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
      if (String(input).includes('/comfy/health')) {
        return { ok: true, json: async () => { throw new Error('not json') } } as unknown as Response
      }
      throw new Error('unrelated check')
    }))
    const comfy = comfyCheckOf(await collectConnectionChecks(READY, null, settings))
    expect(comfy?.state).toBe('warning')
  })

  it('사이드카 자체가 오류를 내면 확인 필요다', async () => {
    installFetch({ detail: 'bad request' }, { ok: false })
    const comfy = comfyCheckOf(await collectConnectionChecks(READY, null, settings))
    expect(comfy?.state).toBe('warning')
  })

  it('ComfyUI 주소를 설정하지 않았으면 미연결이지 정상이 아니다', async () => {
    installFetch({ online: true })
    const comfy = comfyCheckOf(
      await collectConnectionChecks(READY, null, { ...DEFAULT_SETTINGS, comfyBaseUrl: '' })
    )
    expect(comfy?.state).toBe('info')
  })
})
