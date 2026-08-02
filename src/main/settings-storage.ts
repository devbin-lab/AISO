import { dirname } from 'path'
import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync
} from 'fs'
import { randomUUID } from 'crypto'
import {
  DEFAULT_SETTINGS,
  type AppSettings,
  type SettingsRecoveryStatus
} from '../shared/settings.ts'
import { canonicalizeNvidiaNimEndpoint } from '../shared/nvidia.ts'

export interface SettingsFileOps {
  exists(path: string): boolean
  mkdir(path: string): void
  read(path: string): string
  rename(from: string, to: string): void
  remove(path: string): void
  writeExclusive(path: string, contents: string): void
}

const nodeFileOps: SettingsFileOps = {
  exists: existsSync,
  mkdir: (path) => mkdirSync(path, { recursive: true }),
  read: (path) => readFileSync(path, 'utf-8'),
  rename: renameSync,
  remove: (path) => rmSync(path, { force: true }),
  writeExclusive: (path, contents) =>
    writeFileSync(path, contents, { encoding: 'utf-8', flag: 'wx' })
}

export const APP_SETTING_KEYS = new Set<keyof AppSettings>([
  'schemaVersion',
  'activeLlmProvider',
  'model',
  'ollamaHost',
  'nvidiaDeploymentMode',
  'nvidiaModel',
  'nvidiaNimEndpoint',
  'reasoningEffort',
  'tempPreset',
  'tempCustom',
  'contextLength',
  'theme',
  'workspace',
  'embeddingModel',
  'ragEnabled',
  'ragMaxFiles',
  'ragTopK',
  'keepAlive',
  'chatWebSearch',
  'devMode',
  'forceOnboarding',
  'discordEnabled',
  'discordLlmProvider',
  'trayResident',
  'autoLaunch',
  'comfyBaseUrl',
  'comfyInstallPath',
  'comfyModelSelectionMode'
])

export interface SettingsLoadResult {
  settings: AppSettings
  recovery: SettingsRecoveryStatus
  writeBlocked: boolean
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('설정의 최상위 값은 객체여야 합니다.')
  }
  return value as Record<string, unknown>
}

function textValue(raw: Record<string, unknown>, key: keyof AppSettings, fallback: string): string {
  const value = raw[key]
  if (value === undefined) return fallback
  if (typeof value !== 'string') throw new Error(`${String(key)} 값은 문자열이어야 합니다.`)
  return value
}

function boolValue(raw: Record<string, unknown>, key: keyof AppSettings, fallback: boolean): boolean {
  const value = raw[key]
  if (value === undefined) return fallback
  if (typeof value !== 'boolean') throw new Error(`${String(key)} 값은 참/거짓이어야 합니다.`)
  return value
}

function numberValue(raw: Record<string, unknown>, key: keyof AppSettings, fallback: number): number {
  const value = raw[key]
  if (value === undefined) return fallback
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`${String(key)} 값은 유효한 숫자여야 합니다.`)
  }
  return value
}

function enumValue<T extends string>(
  raw: Record<string, unknown>,
  key: keyof AppSettings,
  allowed: readonly T[],
  fallback: T
): T {
  const value = raw[key]
  if (value === undefined) return fallback
  if (typeof value !== 'string' || !allowed.includes(value as T)) {
    throw new Error(`${String(key)} 값이 지원 범위를 벗어났습니다.`)
  }
  return value as T
}

/** Convert a validated v3/v4 record into the single v4 settings contract. */
export function normalizeSettingsRecord(rawValue: unknown, legacy: boolean): AppSettings {
  const raw = record(rawValue)
  for (const key of Object.keys(raw)) {
    if (!APP_SETTING_KEYS.has(key as keyof AppSettings) && !(legacy && key === 'version')) {
      throw new Error(`설정 스키마에 없는 항목입니다: ${key}`)
    }
  }
  const deploymentMode = enumValue(
    raw,
    'nvidiaDeploymentMode',
    ['build', 'nim'] as const,
    DEFAULT_SETTINGS.nvidiaDeploymentMode
  )
  const activeLlmProvider = legacy
    ? 'ollama'
    : enumValue(raw, 'activeLlmProvider', ['ollama', 'nvidia'] as const, 'ollama')
  const suppliedNimEndpoint = textValue(
    raw,
    'nvidiaNimEndpoint',
    DEFAULT_SETTINGS.nvidiaNimEndpoint
  ).trim()
  const nvidiaNimEndpoint = suppliedNimEndpoint
    ? canonicalizeNvidiaNimEndpoint(suppliedNimEndpoint)
    : ''

  if (!legacy && activeLlmProvider === 'nvidia' && deploymentMode === 'nim' && !nvidiaNimEndpoint) {
    throw new Error('사용자 NIM을 활성화하려면 엔드포인트가 필요합니다.')
  }

  return {
    schemaVersion: 4,
    activeLlmProvider,
    model: textValue(raw, 'model', DEFAULT_SETTINGS.model),
    ollamaHost: textValue(raw, 'ollamaHost', DEFAULT_SETTINGS.ollamaHost),
    nvidiaDeploymentMode: deploymentMode,
    nvidiaModel: textValue(raw, 'nvidiaModel', DEFAULT_SETTINGS.nvidiaModel),
    nvidiaNimEndpoint,
    reasoningEffort: enumValue(raw, 'reasoningEffort', ['low', 'medium', 'high'] as const, DEFAULT_SETTINGS.reasoningEffort),
    tempPreset: enumValue(raw, 'tempPreset', ['auto', 'organize', 'balanced', 'custom'] as const, DEFAULT_SETTINGS.tempPreset),
    tempCustom: numberValue(raw, 'tempCustom', DEFAULT_SETTINGS.tempCustom),
    contextLength: numberValue(raw, 'contextLength', DEFAULT_SETTINGS.contextLength),
    theme: enumValue(raw, 'theme', ['dark', 'light', 'system'] as const, DEFAULT_SETTINGS.theme),
    workspace: textValue(raw, 'workspace', DEFAULT_SETTINGS.workspace),
    embeddingModel: textValue(raw, 'embeddingModel', DEFAULT_SETTINGS.embeddingModel),
    ragEnabled: boolValue(raw, 'ragEnabled', DEFAULT_SETTINGS.ragEnabled),
    ragMaxFiles: numberValue(raw, 'ragMaxFiles', DEFAULT_SETTINGS.ragMaxFiles),
    ragTopK: numberValue(raw, 'ragTopK', DEFAULT_SETTINGS.ragTopK),
    keepAlive: textValue(raw, 'keepAlive', DEFAULT_SETTINGS.keepAlive),
    chatWebSearch: boolValue(raw, 'chatWebSearch', DEFAULT_SETTINGS.chatWebSearch),
    devMode: boolValue(raw, 'devMode', DEFAULT_SETTINGS.devMode),
    forceOnboarding: boolValue(raw, 'forceOnboarding', DEFAULT_SETTINGS.forceOnboarding),
    discordEnabled: boolValue(raw, 'discordEnabled', DEFAULT_SETTINGS.discordEnabled),
    discordLlmProvider: legacy
      ? 'ollama'
      : enumValue(raw, 'discordLlmProvider', ['ollama', 'nvidia'] as const, 'ollama'),
    trayResident: boolValue(raw, 'trayResident', DEFAULT_SETTINGS.trayResident),
    autoLaunch: boolValue(raw, 'autoLaunch', DEFAULT_SETTINGS.autoLaunch),
    comfyBaseUrl: textValue(raw, 'comfyBaseUrl', DEFAULT_SETTINGS.comfyBaseUrl),
    comfyInstallPath: textValue(raw, 'comfyInstallPath', DEFAULT_SETTINGS.comfyInstallPath),
    comfyModelSelectionMode: enumValue(raw, 'comfyModelSelectionMode', ['auto', 'manual'] as const, DEFAULT_SETTINGS.comfyModelSelectionMode)
  }
}

export function applySettingsPatch(current: AppSettings, patchValue: unknown): AppSettings {
  const patch = record(patchValue)
  for (const key of Object.keys(patch)) {
    if (!APP_SETTING_KEYS.has(key as keyof AppSettings)) {
      throw new Error(`허용되지 않은 설정 항목입니다: ${key}`)
    }
  }
  if (patch.schemaVersion !== undefined && patch.schemaVersion !== 4) {
    throw new Error('설정 스키마 버전은 변경할 수 없습니다.')
  }
  return normalizeSettingsRecord({ ...current, ...patch, schemaVersion: 4 }, false)
}

export function atomicWriteSettings(
  file: string,
  settings: AppSettings,
  ops: SettingsFileOps = nodeFileOps
): void {
  const temporary = `${file}.${randomUUID()}.tmp`
  try {
    ops.mkdir(dirname(file))
    ops.writeExclusive(temporary, JSON.stringify(settings, null, 2))
    ops.rename(temporary, file)
  } finally {
    ops.remove(temporary)
  }
}

function quarantine(
  file: string,
  category: 'future' | 'corrupt',
  ops: SettingsFileOps
): { backup?: string; blocked: boolean } {
  const backup = `${file}.${category}-${Date.now()}-${randomUUID()}.json`
  try {
    ops.rename(file, backup)
    return { backup, blocked: false }
  } catch {
    return { blocked: true }
  }
}

export function loadSettingsFile(
  file: string,
  ops: SettingsFileOps = nodeFileOps
): SettingsLoadResult {
  const none: SettingsRecoveryStatus = { kind: 'none' }
  if (!ops.exists(file)) return { settings: { ...DEFAULT_SETTINGS }, recovery: none, writeBlocked: false }

  let raw: unknown
  try {
    raw = JSON.parse(ops.read(file))
  } catch {
    const result = quarantine(file, 'corrupt', ops)
    return {
      settings: { ...DEFAULT_SETTINGS },
      recovery: {
        kind: result.blocked ? 'blocked' : 'quarantined',
        message: result.blocked
          ? '손상된 설정 파일을 격리하지 못해 설정 저장을 차단했습니다.'
          : '손상된 설정 파일을 격리하고 안전한 기본값으로 복구했습니다.',
        backupPath: result.backup
      },
      writeBlocked: result.blocked
    }
  }

  try {
    const obj = record(raw)
    const schema = obj.schemaVersion
    const legacyVersion = obj.version
    if (typeof schema === 'number' && schema > 4) {
      const result = quarantine(file, 'future', ops)
      return {
        settings: { ...DEFAULT_SETTINGS },
        recovery: {
          kind: result.blocked ? 'blocked' : 'quarantined',
          message: result.blocked
            ? '더 새로운 설정 파일을 격리하지 못해 설정 저장을 차단했습니다.'
            : '더 새로운 버전의 설정 파일을 격리했습니다. 기존 파일은 보존됩니다.',
          backupPath: result.backup
        },
        writeBlocked: result.blocked
      }
    }

    const legacy = schema === 3 || (
      schema === undefined && (legacyVersion === undefined || legacyVersion === '0.3.1')
    )
    if (!legacy && schema !== 4) throw new Error('지원하지 않는 설정 스키마입니다.')

    const settings = normalizeSettingsRecord(obj, legacy)
    if (!legacy) return { settings, recovery: none, writeBlocked: false }

    try {
      atomicWriteSettings(file, settings, ops)
      return {
        settings,
        recovery: { kind: 'migrated', message: 'v3 설정을 v4 형식으로 안전하게 변환했습니다.' },
        writeBlocked: false
      }
    } catch {
      return {
        settings,
        recovery: {
          kind: 'blocked',
          message: 'v3 설정 변환본을 원자적으로 저장하지 못했습니다. 원본을 보존하고 설정 저장을 차단했습니다.'
        },
        writeBlocked: true
      }
    }
  } catch {
    const result = quarantine(file, 'corrupt', ops)
    return {
      settings: { ...DEFAULT_SETTINGS },
      recovery: {
        kind: result.blocked ? 'blocked' : 'quarantined',
        message: result.blocked
          ? '유효하지 않은 설정 파일을 격리하지 못해 설정 저장을 차단했습니다.'
          : '유효하지 않은 설정 파일을 격리하고 안전한 기본값으로 복구했습니다.',
        backupPath: result.backup
      },
      writeBlocked: result.blocked
    }
  }
}

export function removeSettingsFile(file: string, ops: SettingsFileOps = nodeFileOps): void {
  ops.remove(file)
}
