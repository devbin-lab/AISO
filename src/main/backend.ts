import { spawn, execFileSync, type ChildProcess } from 'child_process'
import { createServer } from 'net'
import { randomBytes } from 'crypto'
import { app } from 'electron'
import { join } from 'path'
import { existsSync } from 'fs'
import type { BackendInfo } from '../shared/backend'

/**
 * FastAPI 사이드카 수명주기 관리.
 * 앱 시작 시 uvicorn을 동적 포트로 스폰하고, /health 폴링으로 준비를 확인한다.
 */

// 사이드카 세션 인증 토큰 — 앱 실행마다 새로 생성. 환경변수로 백엔드에 넘기고,
// preload를 통해서만 렌더러에 노출한다. 렌더러 fetch가 X-Aiso-Token 헤더로 실어야
// 백엔드 인증을 통과하므로, 포트를 스캔한 악성 웹페이지의 cross-origin 호출을 막는다.
const AUTH_TOKEN = randomBytes(32).toString('hex')

/** 렌더러(preload)에 노출할 사이드카 인증 토큰. */
export function backendToken(): string {
  return AUTH_TOKEN
}

let proc: ChildProcess | null = null
let info: BackendInfo = { state: 'stopped', port: null }
const listeners = new Set<(i: BackendInfo) => void>()

function set(patch: Partial<BackendInfo>): void {
  info = { ...info, ...patch }
  listeners.forEach((f) => f(info))
}

export function onBackendChange(f: (i: BackendInfo) => void): () => void {
  listeners.add(f)
  return () => listeners.delete(f)
}

export function backendInfo(): BackendInfo {
  return info
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const s = createServer()
    s.listen(0, '127.0.0.1', () => {
      const addr = s.address()
      if (addr && typeof addr === 'object') {
        const p = addr.port
        s.close(() => resolve(p))
      } else {
        s.close(() => reject(new Error('포트 확보 실패')))
      }
    })
    s.on('error', reject)
  })
}

function resolvePython(codeDir: string): string {
  // 우선순위: 환경변수 > (패키징)번들 런타임 > 프로젝트 venv > 시스템 python
  if (process.env['AISO_PYTHON']) return process.env['AISO_PYTHON']
  // 패키징: 앱에 번들된 임베디드 Python 런타임(resources/pyruntime/python.exe).
  // 사용자가 Python을 따로 설치하지 않아도 백엔드가 뜬다.
  if (app.isPackaged) {
    return join(process.resourcesPath, 'pyruntime', 'python.exe')
  }
  // 개발: 프로젝트 venv > 시스템 python
  const venv = join(codeDir, '.venv', 'Scripts', 'python.exe')
  if (existsSync(venv)) return venv
  return 'python'
}

export async function startBackend(ollamaHost: string): Promise<void> {
  if (proc) return
  // 앱 코드(main.py 등)는 extraResources로 복사된다: 패키징=resources/python, 개발=<repo>/python.
  const dir = app.isPackaged ? join(process.resourcesPath, 'python') : join(app.getAppPath(), 'python')
  const py = resolvePython(dir)
  const port = await freePort()
  set({ state: 'starting', port, detail: undefined })
  console.log(`[backend] 시작: ${py} → 127.0.0.1:${port}`)

  const args = ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', String(port), '--log-level', 'warning']
  // dev: 파이썬 파일 변경 시 사이드카 자동 리로드 (앱 재시작 불필요).
  // uvicorn --reload는 *.py만 감시하고 .venv는 dev 중 불변이라 exclude 없이도 안전.
  if (!app.isPackaged) {
    args.push('--reload')
  }

  // 번들 툴체인(w64devkit 등) 위치: dev는 앱 루트, 패키징은 resources 밑.
  const toolsDir = app.isPackaged ? process.resourcesPath : app.getAppPath()

  let stderrTail = ''
  proc = spawn(py, args, {
    cwd: dir,
    env: {
      ...process.env,
      AISO_OLLAMA_HOST: ollamaHost,
      AISO_TOOLS_DIR: toolsDir,
      AISO_AUTH_TOKEN: AUTH_TOKEN
    },
    windowsHide: true
  })
  proc.stderr?.on('data', (d: Buffer) => {
    stderrTail = (stderrTail + d.toString()).slice(-600)
  })
  proc.stdout?.on('data', (d: Buffer) => console.log('[backend]', d.toString().trim()))
  proc.on('exit', (code) => {
    console.log(`[backend] 종료 code=${code}`)
    proc = null
    if (info.state !== 'stopped') {
      set({ state: 'error', detail: stderrTail || `프로세스 종료 (code ${code})` })
    }
  })
  proc.on('error', (err) => {
    proc = null
    set({ state: 'error', detail: `실행 실패: ${err.message} — Python 설치/venv 확인 필요` })
  })

  // 준비 폴링 (최대 25초)
  const deadline = Date.now() + 25_000
  while (Date.now() < deadline) {
    if (!proc) return // 이미 종료됨 (exit 핸들러가 error 처리)
    try {
      // /health도 토큰 인증 대상이므로 준비 폴링에도 토큰을 실어야 한다.
      const r = await fetch(`http://127.0.0.1:${port}/health`, {
        headers: { 'X-Aiso-Token': AUTH_TOKEN }
      })
      if (r.ok) {
        set({ state: 'ready' })
        console.log('[backend] 준비 완료')
        return
      }
    } catch {
      /* 아직 안 뜸 */
    }
    await new Promise((r) => setTimeout(r, 400))
  }
  set({ state: 'error', detail: `준비 시간 초과${stderrTail ? ` · ${stderrTail}` : ''}` })
}

export function stopBackend(): void {
  if (proc && proc.pid) {
    set({ state: 'stopped' })
    const pid = proc.pid
    proc = null
    // Windows + uvicorn --reload: 리로더가 워커 자식을 스폰하므로 트리 전체를 종료해야
    // orphan(포트 점유)이 남지 않는다.
    if (process.platform === 'win32') {
      try {
        execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' })
      } catch {
        /* 이미 종료됨 */
      }
    } else {
      try {
        process.kill(pid)
      } catch {
        /* 이미 종료됨 */
      }
    }
    console.log('[backend] 중지')
  }
}
