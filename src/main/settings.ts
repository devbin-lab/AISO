import { app } from 'electron'
import { join } from 'path'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs'
import { AppSettings, DEFAULT_SETTINGS } from '../shared/settings'

function settingsFile(): string {
  return join(app.getPath('userData'), 'settings.json')
}

export function loadSettings(): AppSettings {
  try {
    const file = settingsFile()
    if (existsSync(file)) {
      const raw = JSON.parse(readFileSync(file, 'utf-8')) as Partial<AppSettings>
      // 기본값과 병합 → 새 설정 항목이 추가돼도 안전
      return { ...DEFAULT_SETTINGS, ...raw }
    }
  } catch (err) {
    console.error('[settings] 불러오기 실패:', err)
  }
  return { ...DEFAULT_SETTINGS }
}

export function saveSettings(patch: Partial<AppSettings>): AppSettings {
  const next: AppSettings = { ...loadSettings(), ...patch }
  try {
    const file = settingsFile()
    mkdirSync(join(file, '..'), { recursive: true })
    writeFileSync(file, JSON.stringify(next, null, 2), 'utf-8')
  } catch (err) {
    console.error('[settings] 저장 실패:', err)
  }
  return next
}
