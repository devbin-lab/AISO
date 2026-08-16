import { app, safeStorage } from 'electron'
import { join } from 'path'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { loadSettings } from './settings'
import { backendInfo, backendToken } from './backend'
import { canonicalizeNvidiaBinding } from '../shared/nvidia'
import { discordTokenPath, hasUsableDiscordToken } from './discord-token-path'
import { listComfyModelProfiles } from './comfy-models'

/**
 * 디스코드 봇(MVP-1: 기본 채팅) — Electron 메인 측 배선.
 *
 * 봇 토큰은 설정 JSON(평문)에 두지 않고 safeStorage(앱 바운드 암호화)로 별도 파일에 보관한다.
 * 앱이 켜져 있을 때만 동작하는 MVP라 safeStorage로 충분하다(앱-오프 러너가 없어 교차복호 불필요).
 * 실제 봇 구동은 사이드카(discordbot.py)가 하고, 토큰은 인증된 루프백 POST로 1회 전달돼
 * 사이드카 메모리에만 상주한다(env·디스크에 재기록하지 않음).
 */
function tokenFile(): string {
  return discordTokenPath(app.getPath('userData'), app.isPackaged)
}

/** 봇 토큰을 암호화 저장. 빈 값이면 파일을 지운다. */
export function saveDiscordToken(token: string): void {
  const f = tokenFile()
  const t = (token || '').trim()
  if (!t) {
    try {
      rmSync(f, { force: true })
    } catch {
      /* 없으면 무시 */
    }
    return
  }
  try {
    mkdirSync(join(f, '..'), { recursive: true })
    const enc = safeStorage.isEncryptionAvailable()
      ? safeStorage.encryptString(t)
      : Buffer.from(t, 'utf-8') // 암호화 불가 환경 폴백 — NTFS 사용자 ACL에 의존
    writeFileSync(f, enc)
  } catch (e) {
    console.error('[discord] 토큰 저장 실패:', e)
  }
}

function loadDiscordToken(): string {
  const f = tokenFile()
  if (!existsSync(f)) return ''
  try {
    const buf = readFileSync(f)
    return safeStorage.isEncryptionAvailable() ? safeStorage.decryptString(buf) : buf.toString('utf-8')
  } catch {
    return ''
  }
}

export function hasDiscordToken(): boolean {
  return hasUsableDiscordToken(loadDiscordToken())
}

/** 공장초기화용 — 암호화 봇 토큰과 동적 상태(state.json)·예약(schedules.json)을 모두 삭제한다.
 *  '처음 설치 상태로 되돌린다'는 약속대로 가장 민감한 비밀(봇 토큰)과 PII(허용 ID·서버/채널)를 남기지 않는다. */
export function clearDiscordData(): void {
  saveDiscordToken('') // discord.token.enc 삭제
  try {
    rmSync(join(app.getPath('userData'), 'discord'), { recursive: true, force: true })
  } catch {
    /* 없으면 무시 */
  }
}

export interface DiscordApplyResult {
  ok: boolean
  detail?: string
}

export interface NvidiaDiscordRuntime {
  provider: 'nvidia'
  deploymentMode: 'build' | 'nim'
  endpoint: string
  model: string
  grantId?: string
}

const DISCORD_CONFIG_TIMEOUT_MS = 5_000

/** Force the sidecar bot off without requiring any NVIDIA runtime or bearer. */
export async function disableDiscordConfig(): Promise<DiscordApplyResult> {
  const info = backendInfo()
  if (info.state !== 'ready' || !info.port) {
    return { ok: false, detail: 'Discord 봇 중지 상태를 확인하지 못했습니다.' }
  }
  const s = loadSettings()
  try {
    const response = await postDiscordConfig(info.port, {
      enabled: false,
      token: '',
      data_dir: join(app.getPath('userData'), 'discord'),
      provider: 'ollama',
      deployment_mode: null,
      endpoint: s.ollamaHost,
      model: s.model,
      context_length: s.contextLength,
      keep_alive: s.keepAlive,
      ollama_host: s.ollamaHost,
      comfy_base_url: '',
      comfy_profiles: [],
      allow_attachment_images: false
    })
    return response.ok
      ? { ok: true }
      : { ok: false, detail: '사이드카에서 Discord 봇 중지를 확인하지 못했습니다.' }
  } catch {
    return { ok: false, detail: 'Discord 봇 중지 요청이 완료되지 않았습니다.' }
  }
}

/** 현재 설정 + 저장된 토큰을 사이드카에 적용(봇 재시작/중지). 앱 준비 후·설정 변경 시 호출. */
export async function applyDiscordConfig(
  nvidiaRuntime?: NvidiaDiscordRuntime
): Promise<DiscordApplyResult> {
  const info = backendInfo()
  if (info.state !== 'ready' || !info.port) return { ok: false, detail: '백엔드가 아직 준비되지 않았습니다.' }
  const s = loadSettings()
  const comfyProfiles = listComfyModelProfiles().profiles
  let providerConfig: Record<string, unknown>
  if (s.discordLlmProvider === 'nvidia') {
    const binding = canonicalizeNvidiaBinding({
      deploymentMode: s.nvidiaDeploymentMode,
      endpoint: s.nvidiaDeploymentMode === 'nim' ? s.nvidiaNimEndpoint : undefined
    })
    const exact = nvidiaRuntime &&
      nvidiaRuntime.provider === 'nvidia' &&
      nvidiaRuntime.deploymentMode === binding.deploymentMode &&
      nvidiaRuntime.endpoint === binding.endpoint &&
      nvidiaRuntime.model === s.nvidiaModel.trim()
    if (!exact) {
      const disabled = await disableDiscordConfig()
      return disabled.ok
        ? { ok: false, detail: 'Discord NVIDIA 실행 신뢰를 확인하지 못해 봇을 중지했습니다.' }
        : disabled
    }
    providerConfig = {
      provider: 'nvidia',
      deployment_mode: binding.deploymentMode,
      endpoint: binding.endpoint,
      model: nvidiaRuntime.model,
      nvidia_runtime_grant: nvidiaRuntime.grantId ?? ''
    }
  } else {
    providerConfig = {
      provider: 'ollama',
      deployment_mode: null,
      endpoint: s.ollamaHost,
      model: s.model
    }
  }
  const token = loadDiscordToken()
  if (s.discordEnabled && !hasUsableDiscordToken(token)) {
    return {
      ok: false,
      detail:
        '현재 실행 환경에서 사용할 수 있는 Discord 봇 토큰이 없습니다. 봇 토큰을 다시 입력한 뒤 연결/적용을 눌러 주세요.'
    }
  }
  // 소유자·채널·허용목록은 봇이 런타임에 자동 판별/관리한다. 동적 상태는 이 폴더에 영속.
  const dataDir = join(app.getPath('userData'), 'discord')
  try {
    mkdirSync(dataDir, { recursive: true })
  } catch {
    /* 권한 등 실패 무시 */
  }
  try {
    const r = await postDiscordConfig(info.port, {
      enabled: s.discordEnabled,
      token,
      data_dir: dataDir,
      ...providerConfig,
      context_length: s.contextLength,
      keep_alive: s.keepAlive,
      ollama_host: s.ollamaHost,
      comfy_base_url: s.comfyBaseUrl,
      comfy_profiles: comfyProfiles,
      allow_attachment_images: s.discordLlmProvider === 'ollama' &&
        s.model.toLocaleLowerCase('en-US').includes('gemma4')
    })
    if (!r.ok) return { ok: false, detail: `사이드카 오류 HTTP ${r.status}` }
    return { ok: true }
  } catch (e) {
    return { ok: false, detail: `연결 실패: ${String(e)}` }
  }
}

async function postDiscordConfig(port: number, body: Record<string, unknown>): Promise<Response> {
  return fetch(`http://127.0.0.1:${port}/discord/config`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Aiso-Token': backendToken() },
    body: JSON.stringify(body),
    redirect: 'error',
    signal: AbortSignal.timeout(DISCORD_CONFIG_TIMEOUT_MS)
  })
}

export async function discordStatus(): Promise<unknown> {
  const info = backendInfo()
  if (info.state !== 'ready' || !info.port) return { running: false, detail: '백엔드 준비 안 됨' }
  try {
    const r = await fetch(`http://127.0.0.1:${info.port}/discord/status`, {
      headers: { 'X-Aiso-Token': backendToken() }
    })
    return await r.json()
  } catch {
    return { running: false }
  }
}

/** 등록된 예약 목록 조회 — 설정 탭이 표시. */
export async function discordSchedules(): Promise<unknown> {
  const info = backendInfo()
  if (info.state !== 'ready' || !info.port) return { jobs: [] }
  try {
    const r = await fetch(`http://127.0.0.1:${info.port}/discord/schedules`, {
      headers: { 'X-Aiso-Token': backendToken() }
    })
    return await r.json()
  } catch {
    return { jobs: [] }
  }
}

/** 예약 삭제(설정 탭 목록의 삭제 버튼). */
export async function discordScheduleRemove(id: string): Promise<{ ok: boolean }> {
  const info = backendInfo()
  if (info.state !== 'ready' || !info.port) return { ok: false }
  try {
    const r = await fetch(`http://127.0.0.1:${info.port}/discord/schedules/remove`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Aiso-Token': backendToken() },
      body: JSON.stringify({ id })
    })
    return (await r.json()) as { ok: boolean }
  } catch {
    return { ok: false }
  }
}
