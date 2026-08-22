/**
 * 자정 재시도 시각 계산.
 *
 * 주기 검사(기본 6시간)만으로는 날이 바뀐 뒤 최대 6시간 동안 어제 보고서가
 * 없다. 자정 직후 한 번 더 돌려 그 창을 없앤다.
 *
 * index.ts 에서 떼어 둔 이유는 테스트뿐이다 — 그쪽은 Electron 모듈을 끌어와
 * 단위 테스트로 부를 수 없다. 계산을 여기 두면 화면 코드와 테스트가 **같은**
 * 함수를 쓰므로, 한쪽만 바뀌어 조용히 어긋나는 일이 없다.
 */

/** 다음 자정 30초 뒤까지 남은 밀리초. 0 이면 타이머가 폭주하므로 최소 1초. */
export function nextMidnightWaitMs(now: Date = new Date()): number {
  const nextMidnight = new Date(now)
  nextMidnight.setHours(24, 0, 30, 0) // 자정 30초 뒤 — 경계에서 어제/오늘이 흔들리지 않게
  return Math.max(1000, nextMidnight.getTime() - now.getTime())
}
