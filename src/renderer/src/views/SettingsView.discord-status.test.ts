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

describe('describeDiscordStatus — 붙어 있는 서버', () => {
  it('서버 이름을 밝힌다 — 옮겨 다니며 초대한 뒤 어디에 있는지 확인할 곳이 여기뿐이다', () => {
    const text = describeDiscordStatus({
      running: true,
      user: 'Aiso#1234',
      guild_name: '학기작 개발',
      channel_id: '999'
    })
    expect(text).toContain('서버 학기작 개발')
  })

  it('서버 이름을 모르면 주장하지 않는다', () => {
    const text = describeDiscordStatus({ running: true, user: 'Aiso#1234', channel_id: '999' })
    expect(text).not.toContain('서버')
  })

  it('멈춘 봇은 서버 이름을 말하지 않는다', () => {
    const text = describeDiscordStatus({ running: false, guild_name: '학기작 개발', detail: '중지' })
    expect(text).not.toContain('학기작 개발')
  })

  it('공급자와 서버가 함께 있어도 채널 경고가 묻히지 않는다', () => {
    const text = describeDiscordStatus({
      running: true,
      user: 'Aiso#1234',
      provider: 'nvidia',
      model: 'moonshotai/kimi-k3',
      guild_name: '학기작 개발'
    })
    expect(text).toContain('NVIDIA')
    expect(text).toContain('서버 학기작 개발')
    expect(text).toContain('명령 채널 미생성')
  })
})

describe('describeDiscordStatus — 여러 서버', () => {
  const two = [
    { guild_id: '111', guild_name: 'A팀', channel_id: 'aaa', allowlist: [] },
    { guild_id: '222', guild_name: 'B팀', channel_id: 'bbb', allowlist: [] }
  ]

  it('붙어 있는 서버를 전부 보여 준다', () => {
    const text = describeDiscordStatus({ running: true, user: 'Aiso#1', guilds: two })
    expect(text).toContain('서버 A팀, B팀')
    expect(text).toContain('#aiso 채널 준비됨')
  })

  it('채널이 없는 서버를 따로 표시한다 — 그 서버는 전면 무응답이다', () => {
    const mixed = [two[0]!, { ...two[1]!, channel_id: '' }]
    const text = describeDiscordStatus({ running: true, user: 'Aiso#1', guilds: mixed })
    expect(text).toContain('B팀 (채널 없음)')
    expect(text).toContain('1개 서버에 명령 채널 없음')
    expect(text).not.toContain('#aiso 채널 준비됨')
  })

  it('서버가 하나뿐인 옛 응답도 그대로 읽는다', () => {
    const text = describeDiscordStatus({
      running: true, user: 'Aiso#1', guild_name: '학기작 개발', channel_id: '999'
    })
    expect(text).toContain('서버 학기작 개발')
    expect(text).toContain('#aiso 채널 준비됨')
  })

  it('이름이 없으면 서버 id 로라도 구분해 준다', () => {
    const text = describeDiscordStatus({
      running: true,
      guilds: [{ guild_id: '111', guild_name: '', channel_id: 'aaa', allowlist: [] }]
    })
    expect(text).toContain('서버 111')
  })

  it('멈춘 봇은 서버 목록을 말하지 않는다', () => {
    const text = describeDiscordStatus({ running: false, guilds: two, detail: '중지' })
    expect(text).toBe('중지 · 중지')
  })
})
