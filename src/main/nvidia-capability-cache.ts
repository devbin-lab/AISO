import { randomUUID } from 'crypto'
import { dirname } from 'path'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'fs'
import type { LlmModelCapabilities, LlmCapabilityState } from '../shared/llm.ts'
import {
  canonicalizeNvidiaBinding,
  sameNvidiaBinding,
  type NvidiaCapabilitySnapshot,
  type NvidiaCapabilityTargetInput,
  type NvidiaCredentialBinding,
  type NvidiaCredentialBindingInput
} from '../shared/nvidia.ts'

const CACHE_SCHEMA = 1 as const
export const NVIDIA_CAPABILITY_MAX_AGE_MS = 24 * 60 * 60 * 1000

/** Monotonic in-process fence preventing a late probe from restoring invalidated cache data. */
export class NvidiaCapabilityRevision {
  private value = 0
  private mutationDepth = 0

  snapshot(): number {
    return this.value
  }

  invalidate(): void {
    this.value += 1
  }

  beginMutation(): void {
    this.mutationDepth += 1
    this.value += 1
  }

  endMutation(): void {
    if (this.mutationDepth <= 0) throw new Error('NVIDIA capability mutation is not active')
    this.mutationDepth -= 1
    this.value += 1
  }

  isCurrent(snapshot: number): boolean {
    return this.mutationDepth === 0 && snapshot === this.value
  }
}

interface CapabilityCacheFile {
  schemaVersion: typeof CACHE_SCHEMA
  entries: NvidiaCapabilitySnapshot[]
}

export interface CapabilityCacheFileOps {
  exists(path: string): boolean
  mkdir(path: string): void
  read(path: string): string
  rename(from: string, to: string): void
  remove(path: string): void
  writeExclusive(path: string, contents: string): void
}

const nodeFileOps: CapabilityCacheFileOps = {
  exists: existsSync,
  mkdir: (path) => mkdirSync(path, { recursive: true }),
  read: (path) => readFileSync(path, 'utf8'),
  rename: renameSync,
  remove: (path) => rmSync(path, { force: true }),
  writeExclusive: (path, contents) => writeFileSync(path, contents, { encoding: 'utf8', flag: 'wx' })
}

function isCapabilityState(value: unknown): value is LlmCapabilityState {
  return value === 'supported' || value === 'unsupported' || value === 'unknown'
}

function normalizeModel(value: unknown): string {
  if (typeof value !== 'string') throw new Error('NVIDIA 모델명이 필요합니다.')
  const model = value.trim()
  if (!model || model.length > 512) throw new Error('NVIDIA 모델명 형식이 올바르지 않습니다.')
  return model
}

function normalizeCapabilities(value: unknown): LlmModelCapabilities {
  if (!value || typeof value !== 'object') throw new Error('capability cache is invalid')
  const record = value as Record<string, unknown>
  if (
    !isCapabilityState(record.chat) ||
    !isCapabilityState(record.stream) ||
    !isCapabilityState(record.tools)
  ) {
    throw new Error('capability cache is invalid')
  }
  return { chat: record.chat, stream: record.stream, tools: record.tools }
}

function normalizeEntry(value: unknown): NvidiaCapabilitySnapshot {
  if (!value || typeof value !== 'object') throw new Error('capability cache is invalid')
  const record = value as Record<string, unknown>
  if (record.schemaVersion !== CACHE_SCHEMA) throw new Error('capability cache schema is stale')
  const bindingValue = record.binding
  if (!bindingValue || typeof bindingValue !== 'object') throw new Error('capability cache is invalid')
  const bindingRecord = bindingValue as Record<string, unknown>
  const binding = canonicalizeNvidiaBinding({
    deploymentMode: bindingRecord.deploymentMode as 'build' | 'nim',
    endpoint: typeof bindingRecord.endpoint === 'string' ? bindingRecord.endpoint : undefined
  })
  if (binding.endpoint !== bindingRecord.endpoint) throw new Error('capability cache binding is not canonical')
  const checkedAt = typeof record.checkedAt === 'string' ? record.checkedAt : ''
  if (!checkedAt || !Number.isFinite(Date.parse(checkedAt))) throw new Error('capability cache time is invalid')
  return {
    schemaVersion: CACHE_SCHEMA,
    binding,
    model: normalizeModel(record.model),
    capabilities: normalizeCapabilities(record.capabilities),
    checkedAt
  }
}

function parseFile(contents: string): CapabilityCacheFile {
  const raw = JSON.parse(contents) as unknown
  if (!raw || typeof raw !== 'object') throw new Error('capability cache is invalid')
  const record = raw as Record<string, unknown>
  if (record.schemaVersion !== CACHE_SCHEMA) throw new Error('capability cache schema is stale')
  if (!Array.isArray(record.entries)) throw new Error('capability cache is invalid')
  return { schemaVersion: CACHE_SCHEMA, entries: record.entries.map(normalizeEntry) }
}

function targetBinding(target: NvidiaCredentialBindingInput): NvidiaCredentialBinding {
  return canonicalizeNvidiaBinding(target)
}

function sameTarget(
  entry: NvidiaCapabilitySnapshot,
  binding: NvidiaCredentialBinding,
  model: string
): boolean {
  return sameNvidiaBinding(entry.binding, binding) && entry.model === model
}

export class NvidiaCapabilityCache {
  private readonly file: string
  private readonly ops: CapabilityCacheFileOps
  private readonly now: () => number

  constructor(
    file: string,
    ops: CapabilityCacheFileOps = nodeFileOps,
    now: () => number = Date.now
  ) {
    this.file = file
    this.ops = ops
    this.now = now
  }

  private read(): CapabilityCacheFile {
    if (!this.ops.exists(this.file)) return { schemaVersion: CACHE_SCHEMA, entries: [] }
    try {
      return parseFile(this.ops.read(this.file))
    } catch {
      // Unknown, corrupt, or stale schemas are never trusted. A future successful
      // write replaces them with the current metadata-only schema.
      return { schemaVersion: CACHE_SCHEMA, entries: [] }
    }
  }

  private write(entries: NvidiaCapabilitySnapshot[]): void {
    const temporary = `${this.file}.${randomUUID()}.tmp`
    try {
      this.ops.mkdir(dirname(this.file))
      this.ops.writeExclusive(temporary, JSON.stringify({ schemaVersion: CACHE_SCHEMA, entries }, null, 2))
      this.ops.rename(temporary, this.file)
    } finally {
      this.ops.remove(temporary)
    }
  }

  get(target: NvidiaCapabilityTargetInput): NvidiaCapabilitySnapshot | null {
    const binding = targetBinding(target)
    const model = normalizeModel(target.model)
    const entry = this.read().entries.find((candidate) => sameTarget(candidate, binding, model))
    if (!entry) return null
    const checked = Date.parse(entry.checkedAt)
    const age = this.now() - checked
    if (age < 0 || age >= NVIDIA_CAPABILITY_MAX_AGE_MS) return null
    return entry
  }

  put(
    target: NvidiaCapabilityTargetInput,
    capabilities: LlmModelCapabilities
  ): NvidiaCapabilitySnapshot {
    const binding = targetBinding(target)
    const model = normalizeModel(target.model)
    const snapshot: NvidiaCapabilitySnapshot = {
      schemaVersion: CACHE_SCHEMA,
      binding,
      model,
      capabilities: normalizeCapabilities(capabilities),
      checkedAt: new Date(this.now()).toISOString()
    }
    const entries = this.read().entries.filter((entry) => !sameTarget(entry, binding, model))
    entries.push(snapshot)
    this.write(entries)
    return snapshot
  }

  clearTarget(target: NvidiaCapabilityTargetInput): void {
    const binding = targetBinding(target)
    const model = normalizeModel(target.model)
    const current = this.read().entries
    const next = current.filter((entry) => !sameTarget(entry, binding, model))
    if (this.ops.exists(this.file) || next.length !== current.length) this.write(next)
  }

  clearBinding(bindingInput: NvidiaCredentialBindingInput): void {
    const binding = targetBinding(bindingInput)
    const current = this.read().entries
    const next = current.filter((entry) => !sameNvidiaBinding(entry.binding, binding))
    if (this.ops.exists(this.file) || next.length !== current.length) this.write(next)
  }

  removeModelsNotInList(bindingInput: NvidiaCredentialBindingInput, models: string[]): void {
    const binding = targetBinding(bindingInput)
    const available = new Set(models.map(normalizeModel))
    const current = this.read().entries
    const next = current.filter(
      (entry) => !sameNvidiaBinding(entry.binding, binding) || available.has(entry.model)
    )
    if (next.length !== current.length) this.write(next)
  }

  clearAll(): void {
    this.ops.remove(this.file)
  }
}
