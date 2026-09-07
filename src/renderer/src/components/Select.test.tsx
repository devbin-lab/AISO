import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import Select, { matchesQuery } from './Select'

/**
 * 긴 목록에서 고르는 계약.
 *
 * NVIDIA 모델 목록은 수백 개라 손으로 굴려 찾는 것이 사실상 불가능했다. 여기서
 * 고정하는 것은 "검색으로 바로 좁혀지고, 좁힌 것을 키보드로 고를 수 있다"이다.
 */

const MANY = [
  'meta/llama-3.3-70b-instruct',
  'meta/llama-3.1-8b-instruct',
  'nvidia/llama-3.1-nemotron-70b-instruct',
  'mistralai/mistral-large-2-instruct',
  'google/gemma-2-27b-it',
  'microsoft/phi-3-medium-4k-instruct',
  'qwen/qwen2.5-coder-32b-instruct',
  'deepseek-ai/deepseek-r1',
  'writer/palmyra-creative-122b'
]
const FEW = ['alpha', 'beta', 'gamma']

function openMenu(): void {
  fireEvent.click(screen.getByRole('button', { name: /선택|instruct|alpha/ }))
}

describe('Select 검색', () => {
  it('항목이 많으면 검색칸이 뜨고, 적으면 뜨지 않는다', () => {
    const { unmount } = render(<Select value="" options={MANY} onChange={() => {}} placeholder="모델 선택" />)
    openMenu()
    expect(screen.getByLabelText('목록 검색')).toBeTruthy()
    unmount()

    render(<Select value="" options={FEW} onChange={() => {}} placeholder="모델 선택" />)
    fireEvent.click(screen.getByRole('button', { name: '모델 선택' }))
    expect(screen.queryByLabelText('목록 검색')).toBeNull()
  })

  it('검색어로 목록을 좁힌다 — 굴리지 않고 바로 찾는다', () => {
    render(<Select value="" options={MANY} onChange={() => {}} placeholder="모델 선택" />)
    openMenu()
    expect(screen.getAllByRole('option').length).toBe(MANY.length)

    fireEvent.change(screen.getByLabelText('목록 검색'), { target: { value: 'nemotron' } })
    const shown = screen.getAllByRole('option')
    expect(shown.length).toBe(1)
    expect(shown[0]?.textContent).toBe('nvidia/llama-3.1-nemotron-70b-instruct')
  })

  it('떨어져 있는 조각도 순서만 맞으면 걸린다 — 사람이 기억하는 방식', () => {
    render(<Select value="" options={MANY} onChange={() => {}} placeholder="모델 선택" />)
    openMenu()
    fireEvent.change(screen.getByLabelText('목록 검색'), { target: { value: 'llama 70b' } })
    const shown = screen.getAllByRole('option').map((node) => node.textContent)
    expect(shown).toEqual([
      'meta/llama-3.3-70b-instruct',
      'nvidia/llama-3.1-nemotron-70b-instruct'
    ])
  })

  it('좁힌 결과를 엔터로 고른다 — 첫 항목이 켜져 있다', () => {
    const onChange = vi.fn()
    render(<Select value="" options={MANY} onChange={onChange} placeholder="모델 선택" />)
    openMenu()
    const search = screen.getByLabelText('목록 검색')
    fireEvent.change(search, { target: { value: 'qwen' } })
    fireEvent.keyDown(search, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('qwen/qwen2.5-coder-32b-instruct')
  })

  it('맞는 항목이 없으면 그렇게 말한다 — 빈 목록과 구분한다', () => {
    render(<Select value="" options={MANY} onChange={() => {}} placeholder="모델 선택" />)
    openMenu()
    fireEvent.change(screen.getByLabelText('목록 검색'), { target: { value: '없는모델' } })
    expect(screen.getByText('검색 결과 없음')).toBeTruthy()
  })

  it('검색어를 지우면 전체가 돌아온다 — 목록을 잃지 않는다', () => {
    render(<Select value="" options={MANY} onChange={() => {}} placeholder="모델 선택" />)
    openMenu()
    const search = screen.getByLabelText('목록 검색')
    fireEvent.change(search, { target: { value: 'qwen' } })
    fireEvent.change(search, { target: { value: '' } })
    expect(screen.getAllByRole('option').length).toBe(MANY.length)
  })

  it('다시 열면 지난 검색어가 남지 않는다', () => {
    render(<Select value="" options={MANY} onChange={() => {}} placeholder="모델 선택" />)
    openMenu()
    fireEvent.change(screen.getByLabelText('목록 검색'), { target: { value: 'qwen' } })
    fireEvent.keyDown(screen.getByLabelText('목록 검색'), { key: 'Escape' })
    openMenu()
    expect(screen.getAllByRole('option').length).toBe(MANY.length)
  })

  it('검색칸이 없는 짧은 목록도 화살표와 엔터로 고를 수 있다 — 회귀 방지', () => {
    const onChange = vi.fn()
    render(<Select value="" options={FEW} onChange={onChange} placeholder="모델 선택" />)
    const trigger = screen.getByRole('button', { name: '모델 선택' })
    fireEvent.click(trigger)
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    fireEvent.keyDown(trigger, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('beta')
  })
})

describe('matchesQuery', () => {
  it('대소문자를 가리지 않는다', () => {
    expect(matchesQuery('meta/Llama-3.3-70B-Instruct', 'LLAMA')).toBe(true)
  })

  it('조각이 전부 들어 있어야 맞는 것이다', () => {
    expect(matchesQuery('meta/llama-3.3-70b-instruct', 'llama 70b')).toBe(true)
    expect(matchesQuery('meta/llama-3.3-70b-instruct', 'llama 405b')).toBe(false)
  })

  it('빈 검색어는 모두 통과시킨다', () => {
    expect(matchesQuery('anything', '   ')).toBe(true)
  })
})
