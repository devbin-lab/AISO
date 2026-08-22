import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * 좁은 창에서 일일 보고서 패널이 무너지지 않게 하는 CSS 계약.
 *
 * 증상: 창을 줄여 이력 화면이 1열로 접히면 보고서 칸이 통째로 사라지고, 그 안의
 * 글자만 아래 날짜 머리글("8월 22일 (토) 변경 2건") 위에 겹쳐 찍혔다.
 * 실측(창 941px): 패널 높이 2px vs 내용 73px.
 *
 * 원인은 2열 배치용 장치다. 2열에서는 보고서가 달력과 같은 격자 **행**에 있어
 * `height: 0` + `min-block-size: 100%` 로 행 높이(=달력 높이)를 따라가게 해 두었다.
 * 1열로 접히면 보고서가 자기 행을 차지하므로 그 100% 가 자기 자신의 0 높이를
 * 기준으로 풀려 칸이 0 이 된다.
 *
 * jsdom 은 레이아웃도 컨테이너 쿼리도 계산하지 않아 무너짐 자체를 재현할 수 없다.
 * 그래서 1열 분기에 되돌림 선언이 남아 있는지, 그리고 그 분기가 기본 규칙보다
 * **뒤에** 있는지를 지킨다 — 특이도가 같아 순서가 뒤집히면 그대로 다시 무너진다.
 */

const CSS = readFileSync(resolve('src/renderer/src/assets/app.css'), 'utf8')

/** 기본 `.mydb-report-panel { ... }` 규칙(컨테이너 쿼리 밖)의 시작 위치. */
const BASE_AT = CSS.indexOf('\n.mydb-report-panel {')

/** 1열 분기 — `.mydb-report-panel` 을 되돌리는 @container 블록. */
function narrowBlock(): { body: string; at: number } {
  const marker = /@container \(max-width: 1040px\) \{([\s\S]*?)\n\}/g
  let m: RegExpExecArray | null
  while ((m = marker.exec(CSS)) !== null) {
    if (m[1].includes('.mydb-report-panel')) return { body: m[1], at: m.index }
  }
  throw new Error('.mydb-report-panel 을 되돌리는 1열 @container 블록을 찾지 못했다')
}

describe('좁은 창에서 일일 보고서 패널이 무너지지 않는다', () => {
  it('기본 규칙은 2열용 높이 장치를 그대로 갖고 있다', () => {
    // 이 장치가 있기 때문에 아래 되돌림이 필요하다. 사라지면 이 테스트의 전제가 바뀐다.
    expect(BASE_AT).toBeGreaterThan(-1)
    const open = CSS.indexOf('{', BASE_AT)
    const base = CSS.slice(open + 1, CSS.indexOf('}', open))
    expect(base).toMatch(/height:\s*0/)
    expect(base).toMatch(/min-block-size:\s*100%/)
  })

  it('1열로 접히면 높이를 내용 기준으로 되돌린다', () => {
    const { body } = narrowBlock()
    // 둘 중 하나만 빠져도 칸이 0 으로 무너진다.
    expect(body).toMatch(/height:\s*auto/)
    expect(body).toMatch(/min-block-size:\s*0/)
  })

  it('되돌림은 기본 규칙보다 뒤에 온다 — 특이도가 같아 순서가 곧 승패다', () => {
    // 앞에 두면 나중에 오는 기본 규칙의 height:0 이 이겨서 증상이 그대로 돌아온다.
    expect(narrowBlock().at).toBeGreaterThan(BASE_AT)
  })

  it('높이 제한이 풀린 만큼 긴 보고서는 안에서 스크롤한다', () => {
    // 상한이 없으면 본문이 길 때 화면을 끝없이 끌고 내려간다.
    expect(narrowBlock().body).toMatch(/\.mydb-report-panel__body\s*\{[^}]*max-height:/)
  })
})
