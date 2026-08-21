import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * 버튼 글자가 버튼 밖으로 새지 않게 하는 CSS 계약.
 *
 * 증상: 창을 줄이면 설정 화면의 "AI 언로드" · "전체 삭제" 버튼 글자가 두 줄이 되어
 * 버튼 아래위로 삐져나왔다. `.btn` 은 height 가 30px 로 고정인데 `.row` 는 가로
 * flex 라서, 폭이 좁아지면 버튼도 함께 눌리고 한글이 아무 데서나 줄바꿈됐다.
 * 실측(폭 656px): 글자 2줄, 내용 높이 41px vs 상자 30px → 11px 넘침.
 *
 * jsdom 은 레이아웃을 계산하지 않아 넘침 자체를 재현할 수 없다. 그래서 넘침을
 * 막아 주는 선언이 `.btn` 에 남아 있는지를 지킨다 — 셋 중 하나라도 빠지면 증상이
 * 그대로 돌아온다.
 */

// vitest 는 프로젝트 루트에서 돈다(vitest.config.ts). import.meta.url 이 file 스킴이
// 아닐 수 있으므로 루트 기준 경로로 읽는다.
const CSS = readFileSync(resolve('src/renderer/src/assets/app.css'), 'utf8')

function ruleBody(selector: string): string {
  const start = CSS.indexOf(`\n${selector} {`)
  if (start < 0) throw new Error(`${selector} 규칙을 찾지 못했다`)
  const open = CSS.indexOf('{', start)
  const close = CSS.indexOf('}', open)
  return CSS.slice(open + 1, close)
}

describe('버튼 글자가 버튼 안에 머문다', () => {
  const btn = ruleBody('.btn')

  it('.btn 은 글자를 줄바꿈하지 않는다', () => {
    // 이것이 빠지면 좁은 폭에서 "AI 언로/드" 처럼 잘린다.
    expect(btn).toMatch(/white-space:\s*nowrap/)
  })

  it('.btn 은 flex 행에서 눌리지 않는다', () => {
    // .row 는 가로 flex 다. shrink 를 막지 않으면 nowrap 만으로는 가로로 넘친다.
    expect(btn).toMatch(/flex-shrink:\s*0/)
  })

  it('.btn 은 남는 공간에서도 글자를 가운데 둔다', () => {
    // 버튼이 늘어난 자리(라벨이 짧을 때)에서 글자가 왼쪽에 붙지 않게 한다.
    expect(btn).toMatch(/display:\s*inline-flex/)
    expect(btn).toMatch(/align-items:\s*center/)
    expect(btn).toMatch(/justify-content:\s*center/)
  })

  it('높이가 고정이라는 전제는 그대로다', () => {
    // 높이가 auto 로 바뀌면 위 세 선언의 이유가 사라진다. 그때 이 테스트를 다시 판단하라.
    expect(btn).toMatch(/height:\s*30px/)
  })

  it('savebar 에 같은 처방이 중복되지 않는다', () => {
    // 두 곳에 나뉘어 있으면 한쪽만 고치게 되고, 그때 다른 쪽이 조용히 깨진다.
    expect(CSS).not.toMatch(/\.settings-savebar \.btn \{[^}]*white-space/)
  })
})
