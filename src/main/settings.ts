import { app } from 'electron'
import { join } from 'path'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { AppSettings, DEFAULT_SETTINGS } from '../shared/settings'
import { appDataFrozen } from './appdata-guard'

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
  if (appDataFrozen()) return next // 공장초기화 직후 지연 저장 차단(파일 되살림 방지)
  try {
    const file = settingsFile()
    mkdirSync(join(file, '..'), { recursive: true })
    writeFileSync(file, JSON.stringify(next, null, 2), 'utf-8')
  } catch (err) {
    console.error('[settings] 저장 실패:', err)
  }
  return next
}

/** 설정 파일을 지워 기본값으로 되돌린다(공장초기화). 다음 loadSettings가 DEFAULT_SETTINGS 반환. */
export function resetSettings(): void {
  try {
    rmSync(settingsFile(), { force: true })
  } catch (err) {
    console.error('[settings] 초기화 실패:', err)
  }
}
