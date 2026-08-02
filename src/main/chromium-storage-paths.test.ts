import assert from 'node:assert/strict'
import test from 'node:test'
import { chromiumStoragePaths } from './chromium-storage-paths.ts'

test('development restarts keep sessionData stable while isolating disk caches by PID', () => {
  const first = chromiumStoragePaths({
    userData: 'C:\\Users\\tester\\AppData\\Roaming\\aiso',
    temp: 'C:\\Users\\tester\\AppData\\Local\\Temp',
    isDev: true,
    pid: 101
  })
  const restarted = chromiumStoragePaths({
    userData: 'C:\\Users\\tester\\AppData\\Roaming\\aiso',
    temp: 'C:\\Users\\tester\\AppData\\Local\\Temp',
    isDev: true,
    pid: 202
  })

  assert.equal(first.sessionData, restarted.sessionData)
  assert.notEqual(first.diskCache, restarted.diskCache)
  assert.match(first.diskCache, /101$/)
  assert.match(restarted.diskCache, /202$/)
})

test('packaged storage uses stable userData paths', () => {
  const paths = chromiumStoragePaths({
    userData: 'C:\\Users\\tester\\AppData\\Roaming\\aiso',
    temp: 'C:\\Temp',
    isDev: false,
    pid: 101
  })

  assert.match(paths.sessionData, /chromium-session$/)
  assert.match(paths.diskCache, /chromium-cache$/)
  assert.equal(paths.sessionData.includes('101'), false)
  assert.equal(paths.diskCache.includes('101'), false)
})

test('development and installed apps do not share a live Chromium profile', () => {
  const common = {
    userData: 'C:\\Users\\tester\\AppData\\Roaming\\aiso',
    temp: 'C:\\Temp',
    pid: 101
  }
  const development = chromiumStoragePaths({ ...common, isDev: true })
  const packaged = chromiumStoragePaths({ ...common, isDev: false })

  assert.notEqual(development.sessionData, packaged.sessionData)
  assert.match(development.sessionData, /chromium-session-dev$/)
  assert.match(packaged.sessionData, /chromium-session$/)
})
