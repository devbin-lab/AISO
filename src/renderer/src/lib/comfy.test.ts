import { describe, expect, it } from 'vitest'
import { looksLikeImageGenerationRequest } from './comfy'

describe('image follow-up provenance', () => {
  const completionProse =
    '이미지 생성을 완료했습니다. 결과 카드에서 확인하세요. 실제 프롬프트: 1girl, fictional character'
  const visualFeedback =
    '표정이 너무 어두워. 내가 아는 에이메스는 더 밝은 아이돌 같은 표정이야.'

  it('does not turn visual feedback into generation from unverified completion prose', () => {
    expect(looksLikeImageGenerationRequest(visualFeedback, completionProse, false)).toBe(false)
  })

  it('allows visual correction only after a verified image-result event', () => {
    expect(looksLikeImageGenerationRequest(visualFeedback, completionProse, true)).toBe(true)
  })
})
