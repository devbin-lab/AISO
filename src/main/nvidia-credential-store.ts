import { dirname } from 'path'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'fs'
import { randomUUID } from 'crypto'
import {
  canonicalizeNvidiaBinding,
  sameNvidiaBinding,
  type NvidiaCredentialBinding,
  type NvidiaCredentialBindingInput,
  type NvidiaCredentialStatus
} from '../shared/nvidia.ts'

interface EncryptedCredentialPayload {
  schemaVersion: 1
  binding: NvidiaCredentialBinding
  apiKey: string
}

interface CredentialEnvelope {
  schemaVersion: 1
  binding: NvidiaCredentialBinding
  ciphertext: string
}

export interface AsyncSafeCrypto {
  isAvailable(): Promise<boolean>
  backend(): string
  encrypt(plaintext: string): Promise<Buffer>
  decrypt(ciphertext: Buffer): Promise<{ result: string; shouldReEncrypt: boolean }>
}

export interface CredentialFileOps {
  exists(path: string): boolean
  mkdir(path: string): void
  read(path: string): string
  rename(from: string, to: string): void
  remove(path: string): void
  writeExclusive(path: string, contents: string): void
}

const nodeFileOps: CredentialFileOps = {
  exists: existsSync,
  mkdir: (path) => mkdirSync(path, { recursive: true }),
  read: (path) => readFileSync(path, 'utf-8'),
  rename: renameSync,
  remove: (path) => rmSync(path, { force: true }),
  writeExclusive: (path, contents) =>
    writeFileSync(path, contents, { encoding: 'utf-8', flag: 'wx' })
}

function parseEnvelope(contents: string): CredentialEnvelope {
  let raw: unknown
  try {
    raw = JSON.parse(contents)
  } catch {
    throw new Error('저장된 NVIDIA 자격 증명 파일이 손상되었습니다.')
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('저장된 NVIDIA 자격 증명 파일이 손상되었습니다.')
  }
  const candidate = raw as Record<string, unknown>
  if (candidate.schemaVersion !== 1 || typeof candidate.ciphertext !== 'string') {
    throw new Error('지원하지 않는 NVIDIA 자격 증명 형식입니다.')
  }
  const bindingValue = candidate.binding
  if (!bindingValue || typeof bindingValue !== 'object' || Array.isArray(bindingValue)) {
    throw new Error('저장된 NVIDIA 자격 증명 바인딩이 손상되었습니다.')
  }
  const bindingRecord = bindingValue as Record<string, unknown>
  const binding = canonicalizeNvidiaBinding({
    deploymentMode: bindingRecord.deploymentMode as 'build' | 'nim',
    endpoint: typeof bindingRecord.endpoint === 'string' ? bindingRecord.endpoint : undefined
  })
  return { schemaVersion: 1, binding, ciphertext: candidate.ciphertext }
}

async function availability(crypto: AsyncSafeCrypto): Promise<{ ok: boolean; detail?: string }> {
  try {
    if (crypto.backend() === 'basic_text') {
      return { ok: false, detail: '운영체제 보안 저장소가 기본 텍스트 모드여서 API 키 저장을 거부했습니다.' }
    }
    if (!await crypto.isAvailable()) {
      return { ok: false, detail: '운영체제 보안 저장소를 현재 사용할 수 없습니다.' }
    }
    return { ok: true }
  } catch {
    return { ok: false, detail: '운영체제 보안 저장소 상태를 확인할 수 없습니다.' }
  }
}

export class NvidiaCredentialStore {
  private readonly file: string
  private readonly crypto: AsyncSafeCrypto
  private readonly ops: CredentialFileOps

  constructor(
    file: string,
    crypto: AsyncSafeCrypto,
    ops: CredentialFileOps = nodeFileOps
  ) {
    this.file = file
    this.crypto = crypto
    this.ops = ops
  }

  private atomicWrite(envelope: CredentialEnvelope): void {
    const temporary = `${this.file}.${randomUUID()}.tmp`
    try {
      this.ops.mkdir(dirname(this.file))
      this.ops.writeExclusive(temporary, JSON.stringify(envelope, null, 2))
      this.ops.rename(temporary, this.file)
    } finally {
      this.ops.remove(temporary)
    }
  }

  async status(bindingInput?: NvidiaCredentialBindingInput): Promise<NvidiaCredentialStatus> {
    const state = await availability(this.crypto)
    if (!this.ops.exists(this.file)) {
      return {
        encryptionAvailable: state.ok,
        hasStoredCredential: false,
        matchesCurrentBinding: false,
        detail: state.detail
      }
    }
    try {
      const envelope = parseEnvelope(this.ops.read(this.file))
      const current = bindingInput ? canonicalizeNvidiaBinding(bindingInput) : undefined
      return {
        encryptionAvailable: state.ok,
        hasStoredCredential: true,
        matchesCurrentBinding: current ? sameNvidiaBinding(envelope.binding, current) : false,
        detail: state.detail
      }
    } catch {
      return {
        encryptionAvailable: state.ok,
        hasStoredCredential: false,
        matchesCurrentBinding: false,
        detail: '저장된 NVIDIA 자격 증명 파일을 읽을 수 없습니다.'
      }
    }
  }

  async save(bindingInput: NvidiaCredentialBindingInput, apiKeyValue: unknown): Promise<void> {
    const state = await availability(this.crypto)
    if (!state.ok) throw new Error(state.detail)
    if (typeof apiKeyValue !== 'string' || !apiKeyValue.trim()) {
      throw new Error('NVIDIA API 키를 입력해 주세요.')
    }
    const apiKey = apiKeyValue.trim()
    const binding = canonicalizeNvidiaBinding(bindingInput)
    const payload: EncryptedCredentialPayload = { schemaVersion: 1, binding, apiKey }
    let ciphertext: Buffer
    try {
      ciphertext = await this.crypto.encrypt(JSON.stringify(payload))
    } catch {
      throw new Error('운영체제 보안 저장소가 일시적으로 API 키를 암호화하지 못했습니다.')
    }
    this.atomicWrite({ schemaVersion: 1, binding, ciphertext: ciphertext.toString('base64') })
  }

  async readForTransfer(bindingInput: NvidiaCredentialBindingInput): Promise<string> {
    const state = await availability(this.crypto)
    if (!state.ok) throw new Error(state.detail)
    const binding = canonicalizeNvidiaBinding(bindingInput)
    if (!this.ops.exists(this.file)) throw new Error('저장된 NVIDIA API 키가 없습니다.')
    const envelope = parseEnvelope(this.ops.read(this.file))
    if (!sameNvidiaBinding(envelope.binding, binding)) {
      throw new Error('저장된 NVIDIA API 키가 현재 배포 대상과 일치하지 않습니다.')
    }

    let decrypted: { result: string; shouldReEncrypt: boolean }
    try {
      decrypted = await this.crypto.decrypt(Buffer.from(envelope.ciphertext, 'base64'))
    } catch {
      throw new Error('운영체제 보안 저장소가 일시적으로 API 키를 해독하지 못했습니다.')
    }
    let payload: EncryptedCredentialPayload
    try {
      payload = JSON.parse(decrypted.result) as EncryptedCredentialPayload
    } catch {
      throw new Error('해독한 NVIDIA 자격 증명 형식이 올바르지 않습니다.')
    }
    const payloadBinding = canonicalizeNvidiaBinding(payload.binding)
    if (
      payload.schemaVersion !== 1 || typeof payload.apiKey !== 'string' || !payload.apiKey ||
      !sameNvidiaBinding(payloadBinding, envelope.binding) ||
      !sameNvidiaBinding(payloadBinding, binding)
    ) {
      throw new Error('NVIDIA 자격 증명 바인딩 검증에 실패했습니다.')
    }

    if (decrypted.shouldReEncrypt) {
      let rotated: Buffer
      try {
        rotated = await this.crypto.encrypt(decrypted.result)
      } catch {
        throw new Error('NVIDIA API 키 암호화 갱신에 실패했습니다.')
      }
      this.atomicWrite({ schemaVersion: 1, binding, ciphertext: rotated.toString('base64') })
    }
    return payload.apiKey
  }

  delete(): void {
    this.ops.remove(this.file)
  }
}
