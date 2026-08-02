import { app } from 'electron'
import { join } from 'path'
import { appDataFrozen } from './appdata-guard'
import type { AppSettings, SettingsRecoveryStatus } from '../shared/settings'
import {
  applySettingsPatch,
  atomicWriteSettings,
  loadSettingsFile,
  removeSettingsFile
} from './settings-storage'

let settingsRecoveryBlocked = false
let recoveryStatus: SettingsRecoveryStatus = { kind: 'none' }

function settingsFile(): string {
  return join(app.getPath('userData'), 'settings.json')
}

export function loadSettings(): AppSettings {
  const result = loadSettingsFile(settingsFile())
  settingsRecoveryBlocked = result.writeBlocked
  if (result.recovery.kind !== 'none') recoveryStatus = result.recovery
  return result.settings
}

export function getSettingsRecoveryStatus(): SettingsRecoveryStatus {
  return { ...recoveryStatus }
}

export function saveSettings(patch: Partial<AppSettings>): AppSettings {
  if (settingsRecoveryBlocked) {
    throw new Error('설정 파일을 안전하게 복구할 수 없어 저장이 차단되었습니다.')
  }
  if (appDataFrozen()) throw new Error('데이터 초기화 중에는 설정을 저장할 수 없습니다.')
  const next = applySettingsPatch(loadSettings(), patch)
  atomicWriteSettings(settingsFile(), next)
  return next
}

export function resetSettings(): void {
  removeSettingsFile(settingsFile())
  settingsRecoveryBlocked = false
  recoveryStatus = { kind: 'none' }
}
