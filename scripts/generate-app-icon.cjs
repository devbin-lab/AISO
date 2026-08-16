/* Generate the Windows PNG/ICO assets from build/icon.svg.
 * Run with: npm run build:icon
 * Windows ships Microsoft Edge, whose SVG renderer produces the same crisp
 * raster result used by the Electron packager.
 */
const { execFileSync } = require('child_process')
const { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } = require('fs')
const { tmpdir } = require('os')
const { join } = require('path')

const root = join(__dirname, '..')
const input = join(root, 'build', 'icon.svg')
const pngOutput = join(root, 'build', 'icon.png')
const icoOutput = join(root, 'build', 'icon.ico')
const readmeLogoOutput = join(root, 'docs', 'logo.png')
const edge = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'
].find(existsSync)

if (!edge) throw new Error('Microsoft Edge를 찾지 못했습니다. build/icon.png와 build/icon.ico를 생성할 수 없습니다.')

const iconUrl = `file:///${input.replaceAll('\\', '/')}`
const edgeProfile = mkdtempSync(join(tmpdir(), 'aiso-icon-edge-'))
try {
  execFileSync(edge, [
    '--headless', '--disable-gpu', '--hide-scrollbars', '--no-first-run', '--force-device-scale-factor=1',
    `--user-data-dir=${edgeProfile}`, '--window-size=512,512', `--screenshot=${pngOutput}`, iconUrl
  ], { stdio: 'inherit' })
} finally {
  rmSync(edgeProfile, { recursive: true, force: true, maxRetries: 3 })
}

const png = existsSync(pngOutput) ? readFileSync(pngOutput) : Buffer.alloc(0)
if (png.length < 100 || png.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a') {
  throw new Error('SVG 아이콘을 PNG로 렌더링하지 못했습니다.')
}

// README must use the same current visual identity as the app, installer, and
// taskbar.  Keeping this generated copy here prevents the documentation logo
// from silently remaining on an older design.
writeFileSync(readmeLogoOutput, png)

// A PNG-backed ICO is supported by modern Windows and preserves the sharp
// 512px source when Windows scales the app, taskbar, installer, or shortcut.
const header = Buffer.alloc(22)
  header.writeUInt16LE(0, 0)
  header.writeUInt16LE(1, 2)
header.writeUInt16LE(1, 4)
header.writeUInt8(0, 6)
header.writeUInt8(0, 7)
header.writeUInt8(0, 8)
header.writeUInt8(0, 9)
header.writeUInt16LE(1, 10)
header.writeUInt16LE(32, 12)
header.writeUInt32LE(png.length, 14)
header.writeUInt32LE(22, 18)
writeFileSync(icoOutput, Buffer.concat([header, png]))
