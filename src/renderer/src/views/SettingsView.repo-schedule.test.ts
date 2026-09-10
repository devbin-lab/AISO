import { describe, expect, it } from 'vitest'
import type { DiscordSchedule } from '../../../shared/discord'
import {
  describeRepoFailure,
  describeRepoProgress,
  describeScheduleDetail,
  describeScheduleKind,
  repoFolderName
} from './SettingsView'

/**
 * 예약 목록이 **저장소 보고를 저장소 보고로** 보여 준다는 계약.
 *
 * 이 종류가 목록 렌더에 빠져 있어서, 등록해 두고도 화면에는 '메시지'로 표시되고 어느
 * 저장소·어느 브랜치를 보는지가 아예 나오지 않았다. 데이터는 이미 응답에 실려 왔다.
 */

const REPO: DiscordSchedule = {
  id: 'job-1',
  kind: 'repo_report',
  channel_name: 'dev-log',
  text: '',
  repeat: 'interval',
  interval_hours: 6,
  repo_path: 'D:\\My_Git\\AISO',
  branch: 'origin/main',
  last_commit: '92b0384343a6',
  next_run: '2026-09-10T18:00'
}

describe('describeScheduleKind', () => {
  it('저장소 보고를 저장소 보고로 부른다', () => {
    expect(describeScheduleKind('repo_report')).toBe('저장소 보고')
  })

  it('기존 종류의 이름은 그대로다', () => {
    expect(describeScheduleKind('briefing')).toBe('브리핑')
    expect(describeScheduleKind('channel_report')).toBe('채널 보고')
    expect(describeScheduleKind('message')).toBe('메시지')
  })
})

describe('repoFolderName', () => {
  it('윈도우 경로에서 폴더 이름만 뽑는다', () => {
    expect(repoFolderName('D:\\My_Git\\AISO')).toBe('AISO')
  })

  it('POSIX 경로도 같은 규칙으로 읽는다', () => {
    expect(repoFolderName('/home/dev/aiso')).toBe('aiso')
  })

  it('끝에 붙은 구분자에 속아 빈 이름을 내지 않는다', () => {
    expect(repoFolderName('D:\\My_Git\\AISO\\')).toBe('AISO')
  })

  it('경로가 없으면 아무것도 주장하지 않는다', () => {
    expect(repoFolderName(undefined)).toBe('')
  })
})

describe('describeScheduleDetail', () => {
  it('저장소와 브랜치를 함께 보여 준다 — main 과 origin/main 을 구분해야 한다', () => {
    expect(describeScheduleDetail(REPO)).toBe('AISO · origin/main')
  })

  it('지시가 있으면 뒤에 덧붙인다', () => {
    expect(describeScheduleDetail({ ...REPO, text: '분류형으로' })).toBe(
      'AISO · origin/main · 분류형으로'
    )
  })

  it('브랜치를 모르면 HEAD 로 읽는다(옛 저장 파일)', () => {
    expect(describeScheduleDetail({ ...REPO, branch: undefined })).toBe('AISO · HEAD')
  })

  it('채널 보고는 기존대로 수집 채널을 보여 준다', () => {
    const detail = describeScheduleDetail({
      ...REPO,
      kind: 'channel_report',
      source_channels: [{ id: '1', name: '일반', last_message_id: '5' }]
    })
    expect(detail).toBe('#일반 → #dev-log')
  })
})

describe('describeRepoProgress', () => {
  it('아직 한 번도 보고하지 않았으면 기준 커밋을 보여 준다', () => {
    expect(describeRepoProgress(REPO)).toBe('아직 보고 없음 · 기준 커밋 92b0384')
  })

  it('보고한 적이 있으면 그 시각과 커밋을 보여 준다 — 침묵이 정상인 기능이라 이 값이 유일한 신호다', () => {
    const text = describeRepoProgress({ ...REPO, last_reported_at: '2026-09-10T12:00' })
    expect(text).toBe('마지막 보고 2026-09-10 12:00 · 92b0384')
  })

  it('커밋을 모르면 커밋을 지어내지 않는다', () => {
    expect(describeRepoProgress({ ...REPO, last_commit: undefined })).toBe('아직 보고 없음')
  })

  it('저장소 보고가 아니면 진행 줄이 없다', () => {
    expect(describeRepoProgress({ ...REPO, kind: 'message' })).toBe('')
  })
})

describe('describeRepoFailure', () => {
  it('마지막 시도가 실패했으면 사유를 보여 준다', () => {
    // 같은 사유는 #aiso 에 한 번만 알린다. 며칠 뒤 화면을 보는 사람에게는 이게 유일한 단서다.
    const text = describeRepoFailure({ ...REPO, last_failure: '보고서 생성이 240초를 넘겨 중단되었습니다.' })
    expect(text).toBe('⚠ 마지막 시도 실패 — 보고서 생성이 240초를 넘겨 중단되었습니다.')
  })

  it('성공해서 비워졌으면 아무 말도 하지 않는다', () => {
    expect(describeRepoFailure({ ...REPO, last_failure: '' })).toBe('')
    expect(describeRepoFailure(REPO)).toBe('')
  })

  it('저장소 보고가 아니면 해당 없음이다', () => {
    expect(describeRepoFailure({ ...REPO, kind: 'message', last_failure: 'x' })).toBe('')
  })
})
