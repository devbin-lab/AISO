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

// Windows picks the closest image inside an ICO for a taskbar, shortcut, or
// Explorer surface. A single 512px PNG makes the shell downscale fine strokes
// too aggressively at 16-48px, so package the usual small sizes as well.
const icoSizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
const icoWorkDir = mkdtempSync(join(tmpdir(), 'aiso-icon-ico-'))
let icoImages
try {
  icoImages = icoSizes.map((size) => {
    const output = join(icoWorkDir, `icon-${size}.png`)
    const source = pngOutput.replaceAll("'", "''")
    const target = output.replaceAll("'", "''")
    const resize = [
      '$ErrorActionPreference = "Stop"',
      'Add-Type -AssemblyName System.Drawing',
      `$source = [System.Drawing.Image]::FromFile('${source}')`,
      `$bitmap = New-Object System.Drawing.Bitmap(${size}, ${size})`,
      '$graphics = [System.Drawing.Graphics]::FromImage($bitmap)',
      '$graphics.Clear([System.Drawing.Color]::White)',
      '$graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic',
      '$graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality',
      '$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality',
      `$graphics.DrawImage($source, 0, 0, ${size}, ${size})`,
      `$bitmap.Save('${target}', [System.Drawing.Imaging.ImageFormat]::Png)`,
      '$graphics.Dispose(); $bitmap.Dispose(); $source.Dispose()'
    ].join('; ')
    execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', resize], { stdio: 'inherit' })
    return { size, bytes: readFileSync(output) }
  })
} finally {
  rmSync(icoWorkDir, { recursive: true, force: true, maxRetries: 3 })
}

const directory = Buffer.alloc(6 + (16 * icoImages.length))
directory.writeUInt16LE(0, 0)
directory.writeUInt16LE(1, 2)
directory.writeUInt16LE(icoImages.length, 4)
let offset = directory.length
for (const [index, image] of icoImages.entries()) {
  const entry = 6 + (index * 16)
  directory.writeUInt8(image.size === 256 ? 0 : image.size, entry)
  directory.writeUInt8(image.size === 256 ? 0 : image.size, entry + 1)
  directory.writeUInt8(0, entry + 2)
  directory.writeUInt8(0, entry + 3)
  directory.writeUInt16LE(1, entry + 4)
  directory.writeUInt16LE(32, entry + 6)
  directory.writeUInt32LE(image.bytes.length, entry + 8)
  directory.writeUInt32LE(offset, entry + 12)
  offset += image.bytes.length
}
writeFileSync(icoOutput, Buffer.concat([directory, ...icoImages.map((image) => image.bytes)]))
