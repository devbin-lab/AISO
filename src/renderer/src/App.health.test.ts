import { describe, expect, it } from 'vitest'
import { keepIfSame } from './App'
import type { HealthInfo } from '../../shared/backend'

/**
 * health 폴링이 참조를 흔들지 않는다.
 *
 * App 은 5초마다 /health 를 폴링한다. 매번 새 객체를 만들면 값이 하나도 안
 * 바뀌어도 참조가 달라지고, health 를 의존성에 둔 화면들이 전부 다시 돈다.
 * 홈 대시보드가 그래서 5초마다 통째로 재로드됐다(실측: 60초에 12회, 변화 0회).
 *
 * 아래 세 가지가 동시에 성립해야 한다 — 하나라도 깨지면 증상이 돌아오거나,
 * 반대로 진짜 상태 변화를 놓친다.
 */

const base = (): HealthInfo => ({ ollama: true, models: ['gemma4:12b', 'bge-m3'], detail: undefined })

describe('health 참조 안정화', () => {
  it('내용이 같으면 이전 객체를 그대로 준다', () => {
    const prev = base()
    // 폴링이 만들어 낸 새 객체 — 내용은 동일하다.
    expect(keepIfSame(prev, base())).toBe(prev)
  })

  it('12회 폴링에도 참조가 한 번만 정해진다', () => {
    let current: HealthInfo | null = null
    const refs = new Set<HealthInfo>()
    for (let i = 0; i < 12; i++) {
      current = keepIfSame(current, base())
      refs.add(current)
    }
    // 예전에는 12개였다.
    expect(refs.size).toBe(1)
  })

  it('실제 상태가 바뀌면 새 객체를 준다', () => {
    const prev = base()

    // Ollama 연결이 끊겼다
    expect(keepIfSame(prev, { ollama: false, models: [], detail: '연결 실패' })).not.toBe(prev)
    // 모델이 새로 설치됐다
    expect(keepIfSame(prev, { ollama: true, models: ['gemma4:12b', 'bge-m3', 'llama3'], detail: undefined })).not.toBe(prev)
    // 같은 개수라도 다른 모델이면 다른 상태다
    expect(keepIfSame(prev, { ollama: true, models: ['gemma4:12b', 'llama3'], detail: undefined })).not.toBe(prev)
    // detail(오류 사유)만 달라져도 사용자에게 보이는 정보가 바뀐다
    expect(keepIfSame(prev, { ollama: true, models: ['gemma4:12b', 'bge-m3'], detail: '느림' })).not.toBe(prev)
  })

  it('첫 폴링(prev 가 null)은 항상 새 값을 받는다', () => {
    const next = base()
    expect(keepIfSame(null, next)).toBe(next)
  })
})
