import assert from 'node:assert/strict'
import test from 'node:test'
import { discordTokenPath, hasUsableDiscordToken } from './discord-token-path.ts'

test('development and installed app Discord tokens use separate stable files', () => {
  const userData = 'C:\\Users\\tester\\AppData\\Roaming\\aiso'
  const development = discordTokenPath(userData, false)
  const packaged = discordTokenPath(userData, true)

  assert.notEqual(development, packaged)
  assert.match(development, /discord\.token\.dev\.enc$/)
  assert.match(packaged, /discord\.token\.enc$/)
})

test('only a decrypted non-empty Discord token is usable', () => {
  assert.equal(hasUsableDiscordToken(''), false)
  assert.equal(hasUsableDiscordToken('   '), false)
  assert.equal(hasUsableDiscordToken('bot-token'), true)
})
