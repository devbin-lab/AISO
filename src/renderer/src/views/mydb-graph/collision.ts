/**
 * 노드가 서로 파고들지 않게 하는 **위치 제약**.
 *
 * 기존 Barnes-Hut 반발력만으로는 겹침이 막히지 않는다. 그 힘은 1/r² 로 멀어질수록
 * 약해지는 데다 alpha 로 감쇠하므로, 배치가 식고 나면 사실상 0 이 된다. 겹친 채로
 * 멈춘 화면이 나오는 이유가 이것이다.
 *
 * 그래서 힘이 아니라 제약으로 푼다. 매 프레임 겹친 쌍을 찾아 **좌표를 직접** 밀어
 * 떼어 놓는다. alpha 와 무관하므로 다 식은 뒤에도 겹침이 남지 않는다.
 *
 * 그리는 반지름보다 큰 원을 쓰는 것이 핵심이다(사용자 표현으로 "보이지 않는 큰 원").
 * 라벨이 붙는 노드는 원보다 넓은 자리를 차지하고, 여유가 있어야 군집이 동그랗게
 * 뭉친다.
 */

export interface CollisionBody {
  x: number
  y: number
  radius: number
}

export interface CollisionOptions {
  /** 그리는 반지름에 더해 확보할 여유. 이게 '보이지 않는 큰 원'의 두께다. */
  padding?: number
  /** 반복 횟수. 한 번으로는 3개 이상이 뭉친 자리가 안 풀린다. */
  iterations?: number
  /** 한 번에 밀어내는 비율(0~1). 1 이면 즉시 분리하지만 튀어 보인다. */
  strength?: number
  /** 사용자가 끌고 있는 노드는 밀리면 안 된다. */
  isPinned?: (body: CollisionBody, index: number) => boolean
}

/**
 * 겹친 쌍을 떼어 놓고, 실제로 밀어낸 횟수를 돌려준다.
 *
 * 균일 격자로 이웃만 본다. n²로 모든 쌍을 보면 노드가 늘어날수록 프레임이 무너진다.
 * 순서는 입력 순서로 고정한다 — 배치가 실행할 때마다 달라지면 안 된다.
 */
export function resolveCollisions<T extends CollisionBody>(
  bodies: T[],
  options: CollisionOptions = {}
): number {
  const { padding = 0, iterations = 2, strength = 0.5, isPinned } = options
  if (bodies.length < 2) return 0

  let maxRadius = 0
  for (const body of bodies) maxRadius = Math.max(maxRadius, body.radius + padding)
  // 셀이 가장 큰 원의 지름이면, 겹칠 수 있는 상대는 인접 9칸 안에만 있다.
  const cellSize = Math.max(1, maxRadius * 2)

  let corrections = 0
  const buckets = new Map<number, number[]>()
  const keyOf = (x: number, y: number): number => {
    // 좌표를 셀 격자로 접는다. 32비트 안에서 겹치지 않게 큰 소수를 곱해 섞는다.
    const cx = Math.floor(x / cellSize)
    const cy = Math.floor(y / cellSize)
    return cx * 73856093 ^ cy * 19349663
  }

  for (let pass = 0; pass < iterations; pass += 1) {
    buckets.clear()
    for (let i = 0; i < bodies.length; i += 1) {
      const body = bodies[i]!
      const key = keyOf(body.x, body.y)
      const bucket = buckets.get(key)
      if (bucket) bucket.push(i)
      else buckets.set(key, [i])
    }

    for (let i = 0; i < bodies.length; i += 1) {
      const a = bodies[i]!
      const ar = a.radius + padding
      const cx = Math.floor(a.x / cellSize)
      const cy = Math.floor(a.y / cellSize)
      for (let ox = -1; ox <= 1; ox += 1) {
        for (let oy = -1; oy <= 1; oy += 1) {
          const bucket = buckets.get((cx + ox) * 73856093 ^ (cy + oy) * 19349663)
          if (!bucket) continue
          for (const j of bucket) {
            // 각 쌍을 한 번만 본다.
            if (j <= i) continue
            const b = bodies[j]!
            const need = ar + b.radius + padding
            let dx = b.x - a.x
            let dy = b.y - a.y
            let distance = Math.hypot(dx, dy)
            if (distance >= need) continue
            if (distance < 1e-6) {
              // 완전히 겹친 두 노드는 방향이 없다. 인덱스로 정해진 방향을 준다 —
              // 난수를 쓰면 같은 입력이 실행마다 다른 배치를 만든다.
              const angle = ((i * 31 + j * 17) % 360) * (Math.PI / 180)
              dx = Math.cos(angle)
              dy = Math.sin(angle)
              distance = 1e-6
            }
            const overlap = (need - distance) * strength
            const ux = dx / distance
            const uy = dy / distance
            const aPinned = isPinned ? isPinned(a, i) : false
            const bPinned = isPinned ? isPinned(b, j) : false
            if (aPinned && bPinned) continue
            // 한쪽이 고정이면 나머지 한쪽이 전부 물러난다.
            const aShare = aPinned ? 0 : bPinned ? 1 : 0.5
            const bShare = bPinned ? 0 : aPinned ? 1 : 0.5
            a.x -= ux * overlap * aShare
            a.y -= uy * overlap * aShare
            b.x += ux * overlap * bShare
            b.y += uy * overlap * bShare
            corrections += 1
          }
        }
      }
    }
  }
  return corrections
}
