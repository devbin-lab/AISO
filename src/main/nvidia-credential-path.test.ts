import assert from 'node:assert/strict'
import test from 'node:test'
import { nvidiaCredentialPath } from './nvidia-credential-path.ts'

test('development and installed app credentials use separate stable files', () => {
  const userData = 'C:\\Users\\tester\\AppData\\Roaming\\aiso'
  const development = nvidiaCredentialPath(userData, false)
  const packaged = nvidiaCredentialPath(userData, true)

  assert.notEqual(development, packaged)
  assert.match(development, /nvidia-credential\.dev\.json$/)
  assert.match(packaged, /nvidia-credential\.json$/)
})
