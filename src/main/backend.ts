import { spawn, execFileSync, type ChildProcess } from 'child_process'
import { createServer } from 'net'
import { randomBytes } from 'crypto'
import { app } from 'electron'
import { join } from 'path'
import { existsSync } from 'fs'
import { ensureSkillsDir } from './skills'
import type { BackendInfo } from '../shared/backend'
import type { LlmModelCapabilities, LlmCapabilityState } from '../shared/llm'

/**
 * FastAPI 사이드카 수명주기 관리.
 * 앱 시작 시 uvicorn을 동적 포트로 스폰하고, /health 폴링으로 준비를 확인한다.
 */

// 사이드카 세션 인증 토큰 — 앱 실행마다 새로 생성. 환경변수로 백엔드에 넘기고,
// preload를 통해서만 렌더러에 노출한다. 렌더러 fetch가 X-Aiso-Token 헤더로 실어야
// 백엔드 인증을 통과하므로, 포트를 스캔한 악성 웹페이지의 cross-origin 호출을 막는다.
const AUTH_TOKEN = randomBytes(32).toString('hex')
let credentialChannelToken = ''

/** 렌더러(preload)에 노출할 사이드카 인증 토큰. */
export function backendToken(): string {
  return AUTH_TOKEN
}

let proc: ChildProcess | null = null
let info: BackendInfo = { state: 'stopped', port: null }
const listeners = new Set<(i: BackendInfo) => void>()

// 크래시 자동 복구 — 트레이 상주로 장시간 무인 가동 중 사이드카가 죽으면 봇·예약도 함께 죽는다.
// 비정상 종료(의도된 stopBackend가 아닌) 시 지수 백오프로 재기동하고, 'ready'가 되면 카운터를 리셋한다.
// 재기동 성공 시 onBackendChange('ready')가 디스코드 설정을 자동 재적용해 봇이 되살아난다.
const MAX_CRASH_RESTARTS = 5
let crashRestarts = 0
let lastOllamaHost = ''
let restartTimer: ReturnType<typeof setTimeout> | null = null
let lateReadinessProbe: Promise<void> | null = null

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

function credentialChannelHeaders(nonce: string): Record<string, string> {
  if (!credentialChannelToken) throw new Error('NVIDIA 자격 증명 채널이 준비되지 않았습니다.')
  return {
    'Content-Type': 'application/json',
    'X-Aiso-Credential-Token': credentialChannelToken,
    'X-Aiso-Credential-Nonce': nonce
  }
}

async function credentialChannelRequest(
  operation: 'set' | 'bind' | 'clear' | 'status',
  body?: Record<string, unknown>
): Promise<Record<string, unknown>> {
  if (info.state !== 'ready' || info.port === null) throw new Error('사이드카가 준비되지 않았습니다.')
  const nonce = randomBytes(24).toString('hex')
  const response = await fetch(`http://127.0.0.1:${info.port}/internal/credentials/${operation}`, {
    method: 'POST',
    redirect: 'error',
    signal: AbortSignal.timeout(3_000),
    headers: credentialChannelHeaders(nonce),
    body: JSON.stringify(body ?? {})
  })
  if (!response.ok) throw new Error('사이드카 자격 증명 메모리 작업에 실패했습니다.')
  return await response.json() as Record<string, unknown>
}

/** Main-only dormant Gate 3 primitive. The API key is never exposed to Renderer. */
export async function setBackendNvidiaCredential(
  deploymentMode: 'build' | 'nim',
  endpoint: string,
  apiKey: string
): Promise<void> {
  await credentialChannelRequest('set', { deploymentMode, endpoint, apiKey })
}

export async function bindBackendNvidiaCredential(
  deploymentMode: 'nim',
  endpoint: string
): Promise<void> {
  await credentialChannelRequest('bind', { deploymentMode, endpoint })
}

export async function clearBackendNvidiaCredential(): Promise<void> {
  if (!credentialChannelToken || info.state !== 'ready') return
  await credentialChannelRequest('clear')
}

export async function backendNvidiaCredentialStatus(): Promise<Record<string, unknown>> {
  return credentialChannelRequest('status')
}

async function nvidiaRuntimeRequest(
  path: '/nvidia/models' | '/nvidia/capabilities/probe',
  body: Record<string, unknown>
): Promise<Record<string, unknown>> {
  if (info.state !== 'ready' || info.port === null) throw new Error('사이드카가 준비되지 않았습니다.')
  const response = await fetch(`http://127.0.0.1:${info.port}${path}`, {
    method: 'POST',
    redirect: 'error',
    signal: AbortSignal.timeout(70_000),
    headers: {
      'Content-Type': 'application/json',
      'X-Aiso-Token': AUTH_TOKEN
    },
    body: JSON.stringify(body)
  })
  if (!response.ok) {
    throw new Error(`NVIDIA 조회에 실패했습니다 (HTTP ${response.status}).`)
  }
  const payload = await response.json() as unknown
  if (!payload || typeof payload !== 'object') throw new Error('NVIDIA 조회 응답 형식이 올바르지 않습니다.')
  return payload as Record<string, unknown>
}

export async function fetchBackendNvidiaModels(
  deploymentMode: 'build' | 'nim',
  endpoint: string
): Promise<string[]> {
  const result = await nvidiaRuntimeRequest('/nvidia/models', {
    deployment_mode: deploymentMode,
    endpoint
  })
  if (!Array.isArray(result.models) || result.models.some((model) => typeof model !== 'string' || !model.trim())) {
    throw new Error('NVIDIA 모델 목록 응답 형식이 올바르지 않습니다.')
  }
  return [...new Set(result.models.map((model) => String(model).trim()))]
}

function isCapabilityState(value: unknown): value is LlmCapabilityState {
  return value === 'supported' || value === 'unsupported' || value === 'unknown'
}

export async function probeBackendNvidiaCapabilities(
  deploymentMode: 'build' | 'nim',
  endpoint: string,
  model: string
): Promise<LlmModelCapabilities> {
  const result = await nvidiaRuntimeRequest('/nvidia/capabilities/probe', {
    deployment_mode: deploymentMode,
    endpoint,
    model
  })
  const capabilities = result.capabilities
  if (!capabilities || typeof capabilities !== 'object') {
    throw new Error('NVIDIA capability 응답 형식이 올바르지 않습니다.')
  }
  const record = capabilities as Record<string, unknown>
  if (
    !isCapabilityState(record.chat) ||
    !isCapabilityState(record.stream) ||
    !isCapabilityState(record.tools)
  ) {
    throw new Error('NVIDIA capability 응답 형식이 올바르지 않습니다.')
  }
  return { chat: record.chat, stream: record.stream, tools: record.tools }
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

async function isHealthy(port: number): Promise<boolean> {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/health`, {
      headers: { 'X-Aiso-Token': AUTH_TOKEN }
    })
    return response.ok
  } catch {
    return false
  }
}

/** A slow Python/Ollama initialization must be able to recover from the initial timeout. */
function continueLateReadinessProbe(expected: ChildProcess, port: number, initialDetail: string): void {
  if (lateReadinessProbe) return
  lateReadinessProbe = (async () => {
    const deadline = Date.now() + 90_000
    while (Date.now() < deadline && proc === expected && info.state !== 'stopped') {
      if (await isHealthy(port)) {
        crashRestarts = 0
        set({ state: 'ready', detail: undefined })
        console.log('[backend] 지연 준비 완료')
        return
      }
      await new Promise((resolve) => setTimeout(resolve, 2_000))
    }
    if (proc === expected && info.state !== 'stopped' && expected.pid) {
      set({ state: 'error', detail: `${initialDetail} · 재시작을 시도합니다.` })
      if (process.platform === 'win32') {
        try {
          execFileSync('taskkill', ['/PID', String(expected.pid), '/T', '/F'], { stdio: 'ignore' })
        } catch {
          /* already stopped */
        }
      } else {
        try {
          expected.kill()
        } catch {
          /* already stopped */
        }
      }
    }
  })().finally(() => {
    lateReadinessProbe = null
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
  lastOllamaHost = ollamaHost // 크래시 재기동 시 재사용
  if (restartTimer) {
    clearTimeout(restartTimer)
    restartTimer = null
  }
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
  // 스킬 저장소 — 사이드카(create_skill/run_skill)와 설정탭이 공유하는 앱 영속 폴더.
  const skillsDirPath = ensureSkillsDir()

  let stderrTail = ''
  credentialChannelToken = randomBytes(32).toString('hex')
  proc = spawn(py, args, {
    cwd: dir,
    env: {
      ...process.env,
      AISO_OLLAMA_HOST: ollamaHost,
      AISO_TOOLS_DIR: toolsDir,
      AISO_SKILLS_DIR: skillsDirPath,
      AISO_AUTH_TOKEN: AUTH_TOKEN,
      AISO_CREDENTIAL_CHANNEL_TOKEN: credentialChannelToken
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
    // state==='stopped'는 의도된 종료(stopBackend/앱 종료) → 재기동 안 함.
    if (info.state !== 'stopped') {
      set({ state: 'error', detail: stderrTail || `프로세스 종료 (code ${code})` })
      if (crashRestarts < MAX_CRASH_RESTARTS) {
        crashRestarts += 1
        const backoff = Math.min(30_000, 1000 * 2 ** (crashRestarts - 1)) // 1s→2s→4s…최대 30s
        console.log(`[backend] 비정상 종료 — ${backoff}ms 후 재기동 (${crashRestarts}/${MAX_CRASH_RESTARTS})`)
        restartTimer = setTimeout(() => {
          restartTimer = null
          if (info.state !== 'stopped') void startBackend(lastOllamaHost)
        }, backoff)
      } else {
        console.error('[backend] 재기동 상한 도달 — 자동 복구 중단')
      }
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
      if (await isHealthy(port)) {
        crashRestarts = 0 // 정상 준비 → 크래시 카운터 리셋
        set({ state: 'ready' })
        console.log('[backend] 준비 완료')
        return
      }
    } catch {
      /* 아직 안 뜸 */
    }
    await new Promise((r) => setTimeout(r, 400))
  }
  const detail = `준비 시간 초과${stderrTail ? ` · ${stderrTail}` : ''}`
  set({ state: 'error', detail })
  if (proc) continueLateReadinessProbe(proc, port, detail)
}

export function stopBackend(): void {
  credentialChannelToken = ''
  if (restartTimer) {
    clearTimeout(restartTimer)
    restartTimer = null
  }
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
