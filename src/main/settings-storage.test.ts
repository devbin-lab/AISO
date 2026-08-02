import assert from 'node:assert/strict'
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { DEFAULT_SETTINGS, snapshotLlmSettings } from '../shared/settings.ts'
import {
  applySettingsPatch,
  atomicWriteSettings,
  loadSettingsFile,
  type SettingsFileOps
} from './settings-storage.ts'

function temporarySettings(t: test.TestContext): { dir: string; file: string } {
  const dir = mkdtempSync(join(tmpdir(), 'aiso-settings-v4-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  return { dir, file: join(dir, 'settings.json') }
}

const realOps: SettingsFileOps = {
  exists: existsSync,
  mkdir: (path) => mkdirSync(path, { recursive: true }),
  read: (path) => readFileSync(path, 'utf8'),
  rename: renameSync,
  remove: (path) => rmSync(path, { force: true }),
  writeExclusive: (path, contents) => writeFileSync(path, contents, { encoding: 'utf8', flag: 'wx' })
}

test('new install starts with schema 4 and Ollama defaults', (t) => {
  const { file } = temporarySettings(t)
  const result = loadSettingsFile(file)
  assert.equal(result.settings.schemaVersion, 4)
  assert.equal(result.settings.activeLlmProvider, 'ollama')
  assert.equal(result.settings.model, DEFAULT_SETTINGS.model)
  assert.equal(result.recovery.kind, 'none')
})

for (const marker of ['none', 'version', 'schema'] as const) {
  test(`explicit v3 migration preserves legacy values (${marker})`, (t) => {
    const { file } = temporarySettings(t)
    const raw: Record<string, unknown> = {
      model: 'legacy-model',
      ollamaHost: 'http://127.0.0.1:22434',
      workspace: 'D:/work',
      ragEnabled: false,
      tempCustom: 0.25
    }
    if (marker === 'version') raw.version = '0.3.1'
    if (marker === 'schema') raw.schemaVersion = 3
    writeFileSync(file, JSON.stringify(raw))

    const result = loadSettingsFile(file)
    assert.equal(result.recovery.kind, 'migrated')
    assert.equal(result.settings.schemaVersion, 4)
    assert.equal(result.settings.activeLlmProvider, 'ollama')
    assert.equal(result.settings.model, 'legacy-model')
    assert.equal(result.settings.ollamaHost, 'http://127.0.0.1:22434')
    assert.equal(result.settings.workspace, 'D:/work')
    assert.equal(result.settings.ragEnabled, false)
    assert.equal(result.settings.tempCustom, 0.25)
    assert.deepEqual(JSON.parse(readFileSync(file, 'utf8')), result.settings)
  })
}

test('future schema is quarantined and never overwritten', (t) => {
  const { dir, file } = temporarySettings(t)
  const original = JSON.stringify({ schemaVersion: 99, futureValue: 'keep-me' })
  writeFileSync(file, original)
  const result = loadSettingsFile(file)
  assert.equal(result.recovery.kind, 'quarantined')
  assert.equal(existsSync(file), false)
  const backup = readdirSync(dir).find((name) => name.includes('.future-'))
  assert.ok(backup)
  assert.equal(readFileSync(join(dir, backup), 'utf8'), original)
})

test('corrupt settings are quarantined and reported', (t) => {
  const { dir, file } = temporarySettings(t)
  writeFileSync(file, '{not-json')
  const result = loadSettingsFile(file)
  assert.equal(result.recovery.kind, 'quarantined')
  assert.equal(result.settings.activeLlmProvider, 'ollama')
  assert.ok(readdirSync(dir).some((name) => name.includes('.corrupt-')))
})

test('failed atomic migration keeps the complete v3 source and blocks writes', (t) => {
  const { file } = temporarySettings(t)
  const original = JSON.stringify({ version: '0.3.1', model: 'old' })
  writeFileSync(file, original)
  const failingOps: SettingsFileOps = {
    ...realOps,
    rename: (from, to) => {
      if (from.includes('.tmp') && to === file) throw new Error('injected rename failure')
      renameSync(from, to)
    }
  }
  const result = loadSettingsFile(file, failingOps)
  assert.equal(result.recovery.kind, 'blocked')
  assert.equal(result.writeBlocked, true)
  assert.equal(readFileSync(file, 'utf8'), original)
})

test('failed atomic save preserves the previous v4 file', (t) => {
  const { file } = temporarySettings(t)
  const previous = JSON.stringify(DEFAULT_SETTINGS)
  writeFileSync(file, previous)
  const failingOps: SettingsFileOps = {
    ...realOps,
    rename: () => { throw new Error('injected rename failure') }
  }
  assert.throws(() => atomicWriteSettings(file, { ...DEFAULT_SETTINGS, model: 'new' }, failingOps))
  assert.equal(readFileSync(file, 'utf8'), previous)
})

test('renderer patches cannot smuggle credentials into normal settings', () => {
  assert.throws(
    () => applySettingsPatch(DEFAULT_SETTINGS, { nvidiaApiKey: 'CANARY-SECRET' }),
    /허용되지 않은 설정/
  )
})

test('a schema 4 file with an unknown plaintext credential field is quarantined', (t) => {
  const { dir, file } = temporarySettings(t)
  writeFileSync(file, JSON.stringify({ ...DEFAULT_SETTINGS, nvidiaApiKey: 'unexpected-secret' }))
  const result = loadSettingsFile(file)
  assert.equal(result.recovery.kind, 'quarantined')
  assert.equal(existsSync(file), false)
  assert.ok(readdirSync(dir).some((name) => name.includes('.corrupt-')))
})

test('execution settings are frozen snapshots', () => {
  const mutable = { ...DEFAULT_SETTINGS }
  const snapshot = snapshotLlmSettings(mutable)
  mutable.model = 'changed-later'
  assert.equal(snapshot.model, DEFAULT_SETTINGS.model)
  assert.equal(Object.isFrozen(snapshot), true)
})
