import assert from 'node:assert/strict'
import test from 'node:test'
import type { AppSettings } from '../shared/settings.ts'
import { sanitizeSettingsPatch } from './settings-patch.ts'

/**
 * 설정 저장이 한 필드 때문에 통째로 막히지 않는다는 계약.
 *
 * 설정 화면은 폼 **전체**를 한 덩어리로 보낸다. 예전에는 `discordLlmProvider` 가
 * 'nvidia' 로 올라오면 저장 경로가 예외를 던졌고, 저장된 값이 'ollama' 인 채로 폼이
 * 'nvidia' 를 들고 있으면 그 뒤로 API 키도 모델도 봇 켜기도 아무것도 저장되지 않았다.
 */

test('전체 폼 저장은 Discord 공급자 필드가 섞여 있어도 나머지를 전부 저장한다', () => {
  const form = {
    discordLlmProvider: 'nvidia',
    nvidiaModel: 'meta/llama-3.3-70b-instruct',
    discordEnabled: true,
    workspace: 'D:/work'
  } as Partial<AppSettings>

  const result = sanitizeSettingsPatch(form)

  assert.equal(result.ignoredDiscordProvider, 'nvidia')
  assert.equal('discordLlmProvider' in result.patch, false, '공급자 전환은 전용 동의 흐름의 몫이다')
  assert.deepEqual(result.patch, {
    nvidiaModel: 'meta/llama-3.3-70b-instruct',
    discordEnabled: true,
    workspace: 'D:/work'
  })
})

test('원본 패치는 건드리지 않는다 — 호출부가 같은 객체를 다시 쓸 수 있다', () => {
  const form = { discordLlmProvider: 'nvidia', nvidiaModel: 'a/b' } as Partial<AppSettings>
  sanitizeSettingsPatch(form)
  assert.equal(form.discordLlmProvider, 'nvidia')
})

test('공급자 필드가 없는 패치는 그대로 통과한다', () => {
  const form = { nvidiaModel: 'a/b' } as Partial<AppSettings>
  const result = sanitizeSettingsPatch(form)
  assert.equal(result.ignoredDiscordProvider, null)
  assert.deepEqual(result.patch, form)
})

test('무시는 승격이 아니다 — 일반 저장으로 NVIDIA 가 켜지지 않는다', () => {
  const stored = { discordLlmProvider: 'ollama' } as AppSettings
  const result = sanitizeSettingsPatch({ discordLlmProvider: 'nvidia' } as Partial<AppSettings>)
  const merged = { ...stored, ...result.patch }
  assert.equal(merged.discordLlmProvider, 'ollama')
})

test('ollama 로 되돌리는 요청도 일반 저장에서는 무시한다 — 되돌리기도 전용 흐름이 정리한다', () => {
  const result = sanitizeSettingsPatch({ discordLlmProvider: 'ollama' } as Partial<AppSettings>)
  assert.equal(result.ignoredDiscordProvider, 'ollama')
  assert.equal('discordLlmProvider' in result.patch, false)
})

test('빈 패치도 안전하다', () => {
  const result = sanitizeSettingsPatch({})
  assert.deepEqual(result.patch, {})
  assert.equal(result.ignoredDiscordProvider, null)
})
