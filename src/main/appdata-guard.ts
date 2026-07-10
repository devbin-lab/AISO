// 공장초기화 직후 짧은 구간 동안, 진행 중이던 스트림의 지연 저장(conv:save·usage:record 등)이
// 방금 삭제한 userData 파일을 되살리지 못하도록 쓰기를 일시 잠근다.
// (렌더러 리로드가 끝나고 옛 스트림의 꼬리 저장이 지나갈 시간을 확보한다.)
let frozenUntil = 0

/** 지정 시간(ms) 동안 앱 데이터 쓰기를 잠근다(기본 4초). */
export function freezeAppDataWrites(ms = 4000): void {
  frozenUntil = Date.now() + ms
}

/** 지금 앱 데이터 쓰기가 잠겨 있는지. */
export function appDataFrozen(): boolean {
  return Date.now() < frozenUntil
}
