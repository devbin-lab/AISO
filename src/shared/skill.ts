// 스킬(에이전트가 만든 재사용 자동화 도구) — main(파일시스템) ↔ renderer(설정탭) 공용 타입.
// 실제 저장은 파이썬 사이드카가 앱 userData/skills/<이름>/{main.py,skill.json}에 한다.
// main 프로세스는 그 폴더를 읽어 목록·삭제만 제공한다.
export interface SkillMeta {
  name: string
  description: string
  created: number // epoch ms (skill.json의 created ISO를 파싱; 없으면 폴더 mtime)
}
