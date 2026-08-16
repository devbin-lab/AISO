import type { MyDbBridge } from '../../../shared/mydb'

export function getMyDbBridge(): MyDbBridge {
  const bridge = window.api?.myDb
  if (!bridge) {
    throw new Error('My DB 저장소를 준비하는 중입니다. Aiso를 다시 시작해 주세요.')
  }
  return bridge
}
