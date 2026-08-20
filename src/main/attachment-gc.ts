/**
 * 첨부 저장소 정리 판정 — 순수 함수. 파일 시스템도 Electron도 건드리지 않는다.
 *
 * 첨부는 userData/attachments/<uuid>/ 에 쌓이는데 지우는 코드가 없었다. 유일한
 * 삭제는 스테이징 실패 시 롤백 하나뿐이고, 대화를 지워도 공장초기화를 해도 남는다.
 * 실측(이 개발 PC): 23MB 2개 폴더가 있는데 conversations 테이블은 0행 — 전부 고아.
 *
 * 고아가 되는 경로가 대화 삭제만이 아니라는 점이 중요하다. 첨부 칩을 ×로 지우거나
 * 첨부만 하고 전송하지 않아도 그 폴더는 어떤 대화에서도 참조되지 않는다(제거 IPC 자체가
 * 없다). 그래서 "대화 삭제 시 그 대화의 첨부를 지운다"는 훅으로는 실측된 고아를 하나도
 * 회수하지 못한다. 참조 카운팅 스윕이어야 한다.
 *
 * 유예 기간이 안전의 핵심이다. 스테이징된 첨부는 전송 전까지 어떤 DB 행에도 없고
 * 렌더러 state에만 있다. 유예 없이 쓸면 "첨부해 두고 잠시 뒤 전송"에서 사용자 파일이
 * 사라진다.
 */

/** 스테이징 후 이 시간 안에는 참조가 없어도 지우지 않는다. */
export const ATTACHMENT_GRACE_MS = 24 * 60 * 60 * 1000

export interface AttachmentDirEntry {
  /** 폴더 이름 = 첨부 id(uuid) */
  id: string
  /** 마지막 변경 시각(ms). 스테이징 시점의 근사값으로 쓴다. */
  modifiedAtMs: number
}

export interface SweepInput {
  entries: readonly AttachmentDirEntry[]
  /** 대화에 실제로 참조된 첨부 id 집합 */
  live: ReadonlySet<string>
  nowMs: number
  graceMs?: number
}

/**
 * 지워도 되는 첨부 폴더 id를 돌려준다.
 *
 * 지우는 조건은 둘 다 만족할 때뿐이다: (1) 어떤 대화에서도 참조되지 않고,
 * (2) 유예 기간을 넘겼다. 둘 중 하나라도 아니면 남긴다 — 이 함수는 의심스러우면
 * 보존하는 쪽으로 기운다. 첨부는 사용자가 고른 원본 파일의 복사본이다.
 */
export function unreferencedAttachmentIds({
  entries,
  live,
  nowMs,
  graceMs = ATTACHMENT_GRACE_MS
}: SweepInput): string[] {
  const cutoff = nowMs - Math.max(0, graceMs)
  return entries
    .filter((entry) => !live.has(entry.id) && entry.modifiedAtMs <= cutoff)
    .map((entry) => entry.id)
}

/** uuid v4 형태만 첨부 폴더로 인정한다 — 저장소에 섞인 다른 것을 지우지 않기 위해서다. */
const ATTACHMENT_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export function looksLikeAttachmentId(name: string): boolean {
  return ATTACHMENT_ID_RE.test(name)
}

/**
 * 대화 data JSON 문자열들에서 참조된 첨부 id를 뽑는다.
 *
 * 채팅은 messages[].attachments, 에이전트는 items[]와 history[] 양쪽에 첨부 id를
 * 담는다. 저장 구조를 따라 파싱하는 대신 uuid 패턴을 훑는 이유는, 한쪽 구조가 바뀌어도
 * 참조를 놓쳐 사용자 파일을 지우는 일이 없도록 하기 위해서다 — 과보존은 안전하지만
 * 과삭제는 복구 불가다.
 */
export function referencedAttachmentIds(dataBlobs: Iterable<string>): Set<string> {
  const found = new Set<string>()
  const pattern = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi
  for (const blob of dataBlobs) {
    if (!blob) continue
    for (const match of blob.matchAll(pattern)) found.add(match[0].toLowerCase())
  }
  return found
}
