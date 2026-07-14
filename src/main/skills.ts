import { app } from 'electron'
import { join, relative, isAbsolute } from 'path'
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync } from 'fs'
import type { SkillMeta } from '../shared/skill'

/**
 * 에이전트가 만든 스킬의 앱 영속 저장소.
 *
 * 파이썬 사이드카(create_skill/run_skill)와 이 main 프로세스(설정탭 목록·삭제)가
 * 같은 폴더를 공유한다: <userData>/skills/<이름>/{main.py, skill.json}.
 * main은 백엔드 스폰 시 이 경로를 AISO_SKILLS_DIR로 넘겨 양쪽이 일치하게 한다(backend.ts).
 */
export function skillsDir(): string {
  return join(app.getPath('userData'), 'skills')
}

/** 스킬 폴더가 없으면 만들고 경로를 돌려준다(백엔드 스폰 전 호출). */
export function ensureSkillsDir(): string {
  const d = skillsDir()
  try {
    mkdirSync(d, { recursive: true })
  } catch {
    /* 권한 등 실패 시에도 앱은 떠야 하므로 무시 */
  }
  return d
}

/** 이름이 스킬 폴더 바로 아래를 벗어나지 않도록 방어(렌더러/IPC 유입 대비). */
function safeFolder(name: string): string {
  // 경로 구분자·상위참조·드라이브/스트림 지정자(:)를 모두 거부 — 파이썬 쪽 이름 검증과 일치.
  if (typeof name !== 'string' || !name || /[\\/:]/.test(name) || name.includes('..')) {
    throw new Error('잘못된 스킬 이름')
  }
  const base = skillsDir()
  const folder = join(base, name)
  const rel = relative(base, folder)
  if (rel === '' || rel.startsWith('..') || isAbsolute(rel)) throw new Error('잘못된 스킬 경로')
  return folder
}

/** 만들어진 스킬 목록(최신순). skill.json의 설명·생성시각을 읽고, 없으면 폴더 mtime로 폴백. */
export function listSkills(): SkillMeta[] {
  const base = skillsDir()
  if (!existsSync(base)) return []
  const out: SkillMeta[] = []
  let entries: string[] = []
  try {
    entries = readdirSync(base)
  } catch {
    return []
  }
  for (const name of entries) {
    const folder = join(base, name)
    try {
      if (!statSync(folder).isDirectory()) continue
      if (!existsSync(join(folder, 'main.py'))) continue // 스킬 폴더 형태만
      let description = ''
      let created = statSync(folder).mtimeMs
      const manifest = join(folder, 'skill.json')
      if (existsSync(manifest)) {
        const m = JSON.parse(readFileSync(manifest, 'utf-8')) as {
          description?: unknown
          created?: unknown
        }
        if (typeof m.description === 'string') description = m.description
        if (typeof m.created === 'string') {
          const t = Date.parse(m.created)
          if (Number.isFinite(t)) created = t
        }
      }
      out.push({ name, description, created })
    } catch {
      /* 손상된 폴더는 건너뛴다 */
    }
  }
  out.sort((a, b) => b.created - a.created)
  return out
}

/** 스킬 폴더를 통째로 삭제한다(설정탭 삭제 버튼 — 확인 다이얼로그는 렌더러가 담당). */
export function deleteSkill(name: string): void {
  rmSync(safeFolder(name), { recursive: true, force: true })
}
