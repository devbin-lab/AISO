import { describe, expect, it } from 'vitest'
import type { DiscordStatus } from '../../../shared/discord'
import { describeDiscordStatus } from './SettingsView'

/**
 * 봇 상태 한 줄이 **지금 무엇으로 답하고 있는지**를 밝힌다는 계약.
 *
 * 예전에는 실행 여부·계정·채널만 보여 줘서, 저장된 설정과 실제로 돌고 있는 값이 어긋나도
 * 확인할 길이 없었다("로컬로 도는 건지 NVIDIA로 도는 건지 의심이 든다").
 */

const RUNNING: DiscordStatus = {
  running: true,
  user: 'Aiso#1234',
  channel_id: '999'
}

describe('describeDiscordStatus', () => {
  it('실행 중이면 공급자와 모델을 함께 밝힌다', () => {
    const text = describeDiscordStatus({
      ...RUNNING,
      provider: 'nvidia',
      model: 'moonshotai/kimi-k3'
    })
    expect(text).toContain('NVIDIA')
    expect(text).toContain('moonshotai/kimi-k3')
    expect(text).toContain('#aiso 채널 준비됨')
  })

  it('로컬 공급자는 Ollama 로 읽힌다', () => {
    const text = describeDiscordStatus({ ...RUNNING, provider: 'ollama', model: 'gemma4:12b' })
    expect(text).toContain('Ollama')
    expect(text).toContain('gemma4:12b')
    expect(text).not.toContain('NVIDIA')
  })

  it('공급자를 모르면 아무것도 주장하지 않는다 — 추측이 의심을 키운다', () => {
    const text = describeDiscordStatus(RUNNING)
    expect(text).not.toContain('NVIDIA')
    expect(text).not.toContain('Ollama')
    expect(text).toContain('연결됨')
  })

  it('모델 없이 공급자만 있어도 공급자는 보여 준다', () => {
    expect(describeDiscordStatus({ ...RUNNING, provider: 'nvidia' })).toContain('NVIDIA')
  })

  it('멈춘 봇은 공급자를 말하지 않고 사유를 말한다', () => {
    const text = describeDiscordStatus({
      running: false,
      provider: 'nvidia',
      model: 'moonshotai/kimi-k3',
      last_error: '로그인 실패'
    })
    expect(text).toBe('중지 · 로그인 실패')
  })

  it('명령 채널이 없으면 경고가 공급자 표시에 묻히지 않는다', () => {
    const text = describeDiscordStatus({
      running: true,
      user: 'Aiso#1234',
      provider: 'nvidia',
      model: 'moonshotai/kimi-k3'
    })
    expect(text).toContain('명령 채널 미생성')
    expect(text).toContain('NVIDIA')
  })

  it('상태를 아직 못 받았으면 미확인이다', () => {
    expect(describeDiscordStatus(null)).toBe('미확인')
  })
})
