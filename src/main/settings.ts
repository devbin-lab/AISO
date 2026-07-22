import { app } from 'electron'
import { dirname, join } from 'path'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'fs'
import { randomUUID } from 'crypto'
import { AppSettings, ComfyModelSelectionMode, DEFAULT_SETTINGS } from '../shared/settings'
import { appDataFrozen } from './appdata-guard'

let settingsRecoveryBlocked = false

/**
 * Persisted settings are user-editable JSON, so do not let an older or manually
 * edited value turn a manual image request into an undefined selection mode.
 */
function normalizeComfyModelSelectionMode(value: unknown): ComfyModelSelectionMode {
  return value === 'manual' ? 'manual' : 'auto'
}

function settingsFile(): string {
  return join(app.getPath('userData'), 'settings.json')
}

function quarantineCorruptSettings(file: string, reason: unknown): void {
  const backup = `${file}.corrupt-${Date.now()}.json`
  try {
    renameSync(file, backup)
    console.error(`[settings] 손상된 설정을 격리했습니다: ${backup}`, reason)
  } catch (error) {
    // Do not silently overwrite an unreadable file on the next save.
    settingsRecoveryBlocked = true
    console.error('[settings] 손상된 설정 격리 실패:', error, reason)
  }
}

export function loadSettings(): AppSettings {
  const file = settingsFile()
  if (existsSync(file)) {
    try {
      const raw = JSON.parse(readFileSync(file, 'utf-8')) as Partial<AppSettings>
      // 기본값과 병합 → 새 설정 항목이 추가돼도 안전
      return {
        ...DEFAULT_SETTINGS,
        ...raw,
        comfyModelSelectionMode: normalizeComfyModelSelectionMode(raw.comfyModelSelectionMode)
      }
    } catch (err) {
      quarantineCorruptSettings(file, err)
    }
  }
  return { ...DEFAULT_SETTINGS }
}

export function saveSettings(patch: Partial<AppSettings>): AppSettings {
  if (settingsRecoveryBlocked) {
    throw new Error('손상된 설정 파일을 안전하게 격리하지 못해 저장을 중단했습니다.')
  }
  const current = loadSettings()
  const next: AppSettings = {
    ...current,
    ...patch,
    comfyModelSelectionMode: normalizeComfyModelSelectionMode(
      patch.comfyModelSelectionMode ?? current.comfyModelSelectionMode
    )
  }
  // 공장 초기화 뒤 지연된 저장이 삭제한 설정을 되살리면 안 된다. 호출자에게도 실패를
  // 알려 Renderer가 실제로 저장되지 않은 값을 성공처럼 표시하지 않게 한다.
  if (appDataFrozen()) throw new Error('앱 데이터 초기화 중에는 설정을 저장할 수 없습니다.')

  const file = settingsFile()
  const temporary = `${file}.${randomUUID()}.tmp`
  try {
    mkdirSync(dirname(file), { recursive: true })
    // 완성된 JSON을 같은 디렉터리의 임시 파일에 쓴 뒤 교체한다. 전원 종료/디스크 오류가
    // 있어도 기존 settings.json이 반쯤 잘린 상태로 남지 않는다.
    writeFileSync(temporary, JSON.stringify(next, null, 2), { encoding: 'utf-8', flag: 'wx' })
    renameSync(temporary, file)
  } catch (err) {
    console.error('[settings] 저장 실패:', err)
    throw new Error(`설정을 저장하지 못했습니다: ${err instanceof Error ? err.message : String(err)}`)
  } finally {
    rmSync(temporary, { force: true })
  }
  return next
}

/** 설정 파일을 지워 기본값으로 되돌린다(공장초기화). 다음 loadSettings가 DEFAULT_SETTINGS 반환. */
export function resetSettings(): void {
  try {
    rmSync(settingsFile(), { force: true })
    settingsRecoveryBlocked = false
  } catch (err) {
    console.error('[settings] 초기화 실패:', err)
  }
}
