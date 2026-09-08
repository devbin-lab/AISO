import { randomUUID } from 'crypto'
import { dirname } from 'path'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'fs'
import {
  canonicalizeNvidiaBinding,
  sameNvidiaBinding,
  type NvidiaCapabilitySnapshot,
  type NvidiaCredentialBinding
} from '../shared/nvidia.ts'

const CONSENT_SCHEMA = 1 as const

/**
 * 사용자가 "이 대상으로 Discord 대화를 보내도 좋다"고 승인한 사실.
 *
 * 예전에는 이 승인이 프로세스 메모리에만 있어서, 앱을 껐다 켤 때마다 Discord 기능이
 * 죽은 채로 시작하고 승인 버튼을 다시 눌러야 살아났다. 그런데 재시작은 **무엇이 어디로
 * 가는지를 바꾸지 않는다**. 지켜야 하는 성질은 "이 프로세스에서 승인했다"가 아니라
 * "지금 이 대상에 대해 승인했다"이다. 그래서 승인을 대상에 묶어 저장한다.
 */
export interface NvidiaDiscordConsentRecord {
  schemaVersion: typeof CONSENT_SCHEMA
  deploymentMode: 'build' | 'nim'
  endpoint: string
  model: string
  /**
   * 승인 당시 근거가 된 기능 검사 시각.
   *
   * 기능 검사 결과가 바뀌거나 다시 검사되면 이 값이 달라져 저장된 승인이 더는 맞지 않는다.
   * 키를 바꾸거나 지우면 기능 검사 캐시가 통째로 비므로 승인도 자동으로 무효가 된다.
   */
  capabilityCheckedAt: string
  consentedAt: string
}

export interface NvidiaDiscordConsentTarget extends NvidiaCredentialBinding {
  model: string
}

export interface ConsentFileOps {
  exists(path: string): boolean
  read(path: string): string
  writeExclusive(path: string, contents: string): void
  rename(from: string, to: string): void
  remove(path: string): void
  mkdir(path: string): void
}

const nodeOps: ConsentFileOps = {
  exists: (path) => existsSync(path),
  read: (path) => readFileSync(path, 'utf-8'),
  writeExclusive: (path, contents) => writeFileSync(path, contents, { encoding: 'utf-8', flag: 'wx' }),
  rename: (from, to) => renameSync(from, to),
  remove: (path) => rmSync(path, { force: true }),
  mkdir: (path) => mkdirSync(path, { recursive: true })
}

function parseRecord(raw: string): NvidiaDiscordConsentRecord | null {
  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    return null
  }
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  if (record.schemaVersion !== CONSENT_SCHEMA) return null
  const mode = record.deploymentMode
  if (mode !== 'build' && mode !== 'nim') return null
  const endpoint = record.endpoint
  const model = record.model
  const checkedAt = record.capabilityCheckedAt
  const consentedAt = record.consentedAt
  if (
    typeof endpoint !== 'string' || !endpoint ||
    typeof model !== 'string' || !model ||
    typeof checkedAt !== 'string' || !checkedAt ||
    typeof consentedAt !== 'string' || !consentedAt
  ) {
    return null
  }
  return {
    schemaVersion: CONSENT_SCHEMA,
    deploymentMode: mode,
    endpoint,
    model,
    capabilityCheckedAt: checkedAt,
    consentedAt
  }
}

/**
 * 저장된 승인이 **지금 이 대상과 이 기능 검사 근거에** 그대로 들어맞는가.
 *
 * 하나라도 어긋나면 거짓이다. 모델·배포 대상·엔드포인트가 바뀌었거나, 기능 검사를 다시
 * 했거나(시각이 달라짐), 키 교체·삭제로 검사 캐시가 비었으면 승인은 더는 그 상황을
 * 가리키지 않는다. 그때는 사용자에게 다시 물어야 한다.
 *
 * 기능 검사 캐시 자체가 24시간이 지나면 사라지므로, 이 판정은 그 신선도를 그대로 물려받는다.
 */
export function consentMatchesTarget(
  record: NvidiaDiscordConsentRecord | null,
  target: NvidiaDiscordConsentTarget | null,
  capability: NvidiaCapabilitySnapshot | null
): boolean {
  if (!record || !target || !capability) return false
  if (capability.capabilities.tools !== 'supported') return false
  if (record.model !== target.model) return false
  if (record.capabilityCheckedAt !== capability.checkedAt) return false
  return sameNvidiaBinding(
    canonicalizeNvidiaBinding({ deploymentMode: record.deploymentMode, endpoint: record.endpoint }),
    target
  )
}

/** 저장된 승인. 대상에 묶여 있고, 어긋나면 스스로 무효가 된다. */
export class NvidiaDiscordConsentStore {
  private readonly file: string
  private readonly ops: ConsentFileOps

  constructor(file: string, ops: ConsentFileOps = nodeOps) {
    this.file = file
    this.ops = ops
  }

  read(): NvidiaDiscordConsentRecord | null {
    if (!this.ops.exists(this.file)) return null
    try {
      return parseRecord(this.ops.read(this.file))
    } catch {
      return null
    }
  }

  record(target: NvidiaDiscordConsentTarget, capability: NvidiaCapabilitySnapshot): void {
    const binding = canonicalizeNvidiaBinding(target)
    const entry: NvidiaDiscordConsentRecord = {
      schemaVersion: CONSENT_SCHEMA,
      deploymentMode: binding.deploymentMode,
      endpoint: binding.endpoint,
      model: target.model,
      capabilityCheckedAt: capability.checkedAt,
      consentedAt: new Date().toISOString()
    }
    const temporary = `${this.file}.${randomUUID()}.tmp`
    try {
      this.ops.mkdir(dirname(this.file))
      this.ops.writeExclusive(temporary, JSON.stringify(entry, null, 2))
      this.ops.rename(temporary, this.file)
    } finally {
      this.ops.remove(temporary)
    }
  }

  clear(): void {
    this.ops.remove(this.file)
  }
}
