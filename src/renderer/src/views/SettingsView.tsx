import { useEffect, useMemo, useRef, useState } from 'react'
import {
  type AppSettings,
  type ComfyModelSelectionMode,
  type ReasoningEffort,
  type ThemeMode,
  type TempPreset,
  TEMP_MODE_OPTIONS,
  RAG_MAX_FILES_OPTIONS
} from '../../../shared/settings'
import {
  NVIDIA_BUILD_BASE_URL,
  canonicalizeNvidiaNimEndpoint,
  type NvidiaCredentialBindingInput,
  type NvidiaCredentialStatus
} from '../../../shared/nvidia'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import type { UpdateStatus } from '../../../shared/update'
import type { SkillMeta } from '../../../shared/skill'
import type { DiscordStatus, DiscordSchedule } from '../../../shared/discord'
import Select from '../components/Select'
import Segmented from '../components/Segmented'
import Dropdown from '../components/Dropdown'
import { confirmDialog } from '../components/ConfirmDialog'
import ComfyModelManager from '../components/ComfyModelManager'
import AgentToolCatalog from '../components/AgentToolCatalog'
import { unloadModels } from '../lib/ollama'

const ONOFF = [
  { v: true, label: '켜짐' },
  { v: false, label: '꺼짐' }
]

interface Props {
  settings: AppSettings
  backend: BackendInfo
  health: HealthInfo | null
  onSave: (patch: Partial<AppSettings>) => Promise<boolean>
  /** 지금 이 화면이 실제로 보이는 탭인지 — 개발자 모드 단축키 감지 범위를 설정 탭에 한정한다.
   *  (뷰는 항상 마운트 상태로 유지되므로 이 플래그 없이는 다른 탭에서도 단축키가 먹힌다.) */
  active: boolean
}

const DEV_UNLOCK_TAPS = 10 // 이 횟수만큼 눌러야 개발자 모드 활성화
const DEV_UNLOCK_WINDOW_MS = 3000 // 이 시간 안에 연타해야 카운트(느리게 누르면 리셋)

const EFFORTS: { v: ReasoningEffort; label: string }[] = [
  { v: 'low', label: 'Low' },
  { v: 'medium', label: 'Medium' },
  { v: 'high', label: 'High' }
]

const THEMES: { v: ThemeMode; label: string }[] = [
  { v: 'dark', label: '다크' },
  { v: 'light', label: '라이트' },
  { v: 'system', label: '시스템' }
]

// 모델 상주 유지(keep_alive) — Ollama 형식 문자열
const KEEP_ALIVE: { v: string; label: string }[] = [
  { v: '5m', label: '5분' },
  { v: '30m', label: '30분' },
  { v: '-1', label: '항상' },
  { v: '0', label: '끔' }
]

// 세부조정 없이 6단계로만 고른다 (4K ~ 128K)
const TOKEN_STEPS = [4096, 8192, 16384, 32768, 65536, 131072]
const TOKEN_LABELS = ['4K', '8K', '16K', '32K', '64K', '128K']

/** 저장된 값이 단계 목록에 없으면(과거 기본값 등) 가장 가까운 단계로 스냅한다. */
function nearestTokenIndex(v: number): number {
  let idx = 0
  let best = Infinity
  TOKEN_STEPS.forEach((s, i) => {
    const d = Math.abs(s - v)
    if (d < best) {
      best = d
      idx = i
    }
  })
  return idx
}

/** 슬라이더 배경 — 채움(오렌지)/미채움(연한 회색)을 값 비율(0~100)로 그린다. */
function rangeFill(pct: number): React.CSSProperties {
  const p = Math.max(0, Math.min(100, pct))
  return { background: `linear-gradient(to right, var(--accent) ${p}%, var(--track) ${p}%)` }
}

type SectionId =
  | 'llm'
  | 'tools'
  | 'comfy'
  | 'appearance'
  | 'skills'
  | 'discord'
  | 'updates'
  | 'developer'

function SettingsView({ settings, backend, health, onSave, active }: Props): React.JSX.Element {
  const [form, setForm] = useState<AppSettings>(settings)
  const [saved, setSaved] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [dirtyFields, setDirtyFields] = useState<Set<keyof AppSettings>>(() => new Set())
  const [manualModel, setManualModel] = useState(false)
  const [activeSection, setActiveSection] = useState<SectionId>('llm')
  const [nvidiaApiKey, setNvidiaApiKey] = useState('')
  const [credentialStatus, setCredentialStatus] = useState<NvidiaCredentialStatus | null>(null)
  const [credentialBusy, setCredentialBusy] = useState(false)
  const [credentialMessage, setCredentialMessage] = useState('')
  const [settingsRecoveryMessage, setSettingsRecoveryMessage] = useState('')

  const modelOptions = health?.models ?? []
  const useDropdown = modelOptions.length > 0 && !manualModel

  // 외부 설정이 로드/변경되면 동기화하되, 사용자가 아직 저장하지 않은 필드는 보존한다.
  // 예: 개발자 모드 토글 저장이 ComfyUI 주소 입력 초안을 지우면 안 된다.
  useEffect(() => {
    setForm((current) => {
      const merged = { ...settings }
      dirtyFields.forEach((field) => {
        ;(merged as Record<string, unknown>)[field] = current[field]
      })
      return merged
    })
  }, [settings, dirtyFields])

  const dirty = useMemo(
    () => (Object.keys(form) as (keyof AppSettings)[]).some((k) => form[k] !== settings[k]),
    [form, settings]
  )
  const comfyConnectionDirty = form.comfyBaseUrl !== settings.comfyBaseUrl ||
    form.comfyInstallPath !== settings.comfyInstallPath
  const comfyInstallPathDirty = form.comfyInstallPath !== settings.comfyInstallPath

  const set = <K extends keyof AppSettings,>(k: K, v: AppSettings[K]): void => {
    setForm((f) => ({ ...f, [k]: v }))
    setDirtyFields((fields) => new Set(fields).add(k))
    setSaved(false)
    setSaveError('')
  }

  const currentCredentialBinding = (): NvidiaCredentialBindingInput => ({
    deploymentMode: form.nvidiaDeploymentMode,
    endpoint: form.nvidiaDeploymentMode === 'nim' ? form.nvidiaNimEndpoint : undefined
  })

  const refreshCredentialStatus = async (): Promise<void> => {
    try {
      if (form.nvidiaDeploymentMode === 'nim') {
        canonicalizeNvidiaNimEndpoint(form.nvidiaNimEndpoint)
      }
      const status = await window.api.nvidia.credential.status(currentCredentialBinding())
      setCredentialStatus(status)
      if (status.detail) setCredentialMessage(status.detail)
    } catch (error) {
      setCredentialStatus(null)
      setCredentialMessage(error instanceof Error ? error.message : String(error))
    }
  }

  useEffect(() => {
    if (!active) return
    void window.api.settings.recoveryStatus().then((status) => {
      setSettingsRecoveryMessage(status.kind === 'none' ? '' : status.message ?? '')
    }).catch(() => {})
    if (form.activeLlmProvider === 'nvidia') void refreshCredentialStatus()
    // Status contains metadata only; this never decrypts or transfers the API key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, form.activeLlmProvider, form.nvidiaDeploymentMode, form.nvidiaNimEndpoint])

  const saveCredential = async (): Promise<void> => {
    setCredentialBusy(true)
    setCredentialMessage('')
    try {
      const binding = currentCredentialBinding()
      if (credentialStatus?.hasStoredCredential) {
        await window.api.nvidia.credential.replace(binding, nvidiaApiKey)
      } else {
        await window.api.nvidia.credential.save(binding, nvidiaApiKey)
      }
      setNvidiaApiKey('')
      setCredentialMessage('API 키를 운영체제 보안 저장소에 암호화해 저장했습니다.')
      await refreshCredentialStatus()
    } catch (error) {
      setCredentialMessage(error instanceof Error ? error.message : String(error))
    } finally {
      setCredentialBusy(false)
    }
  }

  const removeCredential = async (): Promise<void> => {
    setCredentialBusy(true)
    setCredentialMessage('')
    try {
      await window.api.nvidia.credential.delete()
      setNvidiaApiKey('')
      setCredentialMessage('저장된 NVIDIA API 키를 삭제했습니다.')
      await refreshCredentialStatus()
    } catch (error) {
      setCredentialMessage(error instanceof Error ? error.message : String(error))
    } finally {
      setCredentialBusy(false)
    }
  }

  const submit = async (): Promise<void> => {
    const ok = await onSave(form)
    if (!ok) {
      setSaveError('설정을 저장하지 못했습니다. 권한·디스크 상태를 확인한 뒤 다시 시도해 주세요.')
      return
    }
    setSaveError('')
    setDirtyFields(new Set())
    setSaved(true)
    window.setTimeout(() => setSaved(false), 1800)
  }

  const pickComfyInstall = async (): Promise<void> => {
    try {
      const selected = await window.api.comfy.pickInstall()
      if (selected) set('comfyInstallPath', selected)
    } catch {
      // 대화상자 취소나 IPC 오류는 현재 입력값을 보존한다.
    }
  }

  // ── 자동 업데이트 (GitHub 릴리스) ──
  const [appVersion, setAppVersion] = useState('')
  const [upd, setUpd] = useState<UpdateStatus>({ state: 'idle' })

  // ── 긴급 GPU 확보: 로드된 AI 모델을 즉시 VRAM에서 내린다 ──
  const [dropping, setDropping] = useState(false)
  const [dropMsg, setDropMsg] = useState('')
  const dropAI = async (): Promise<void> => {
    setDropping(true)
    setDropMsg('')
    try {
      const info = await window.api.backend.info()
      if (info.state !== 'ready' || !info.port) {
        setDropMsg('백엔드가 준비되지 않았습니다.')
        return
      }
      const r = await unloadModels(info.port, form.ollamaHost)
      if (!r.ok) setDropMsg(`실패: ${r.detail ?? '알 수 없는 오류'}`)
      else if (r.unloaded.length === 0) setDropMsg('내릴 모델이 없습니다(이미 비어 있음).')
      else setDropMsg(`${r.unloaded.length}개 모델을 내렸습니다. GPU를 비웠습니다.`)
    } catch (e) {
      setDropMsg(`오류: ${String(e)}`)
    } finally {
      setDropping(false)
      window.setTimeout(() => setDropMsg(''), 6000)
    }
  }
  useEffect(() => {
    window.api.updates.version().then(setAppVersion).catch(() => {})
    return window.api.updates.onStatus(setUpd)
  }, [])
  const checkUpdate = (): void => {
    setUpd({ state: 'checking' })
    // dev-disabled·error는 이벤트가 안 올 수 있어 반환값으로 즉시 반영
    void window.api.updates.check().then((s) => {
      if (s.state === 'dev-disabled' || s.state === 'error') setUpd(s)
    })
  }
  // ── 개발자 모드: Ctrl+Shift+F1 10회 연타로 활성화(설정 탭에서만 감지) ──
  const [devToast, setDevToast] = useState<string | null>(null)
  const devModeRef = useRef(settings.devMode) // 키 핸들러 클로저가 최신 값을 보게(리스너 재등록 없이)
  const tapsRef = useRef<number[]>([])
  useEffect(() => {
    devModeRef.current = settings.devMode
  }, [settings.devMode])
  useEffect(() => {
    if (!active) return // 설정 탭이 실제로 보일 때만 감지 — 뷰가 항상 마운트라 필요
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.repeat || e.key !== 'F1' || !e.ctrlKey || !e.shiftKey) return
      e.preventDefault()
      if (devModeRef.current) return // 이미 켜져 있으면 무시
      const now = Date.now()
      const taps = tapsRef.current.filter((t) => now - t < DEV_UNLOCK_WINDOW_MS)
      taps.push(now)
      tapsRef.current = taps
      if (taps.length >= DEV_UNLOCK_TAPS) {
        tapsRef.current = []
        devModeRef.current = true // 라운드트립 끝나기 전 재연타로 다시 트리거되는 것 방지
        void onSave({ devMode: true }).then((saved) => {
          if (!saved) {
            devModeRef.current = false
            setSaveError('개발자 모드 설정을 저장하지 못했습니다.')
            return
          }
          setDevToast('🔓 개발자 모드가 활성화되었습니다')
          window.setTimeout(() => setDevToast(null), 2500)
        })
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [active, onSave])

  const disableDevMode = (): void => {
    setActiveSection('llm') // '개발자' 섹션이 사라지므로 다른 섹션으로 이동
    void onSave({ devMode: false, forceOnboarding: false }).then((saved) => {
      if (!saved) setSaveError('개발자 모드 설정을 저장하지 못했습니다.')
    })
  }

  // ── 스킬 관리 (에이전트가 만든 자동화 도구) ──
  const [skills, setSkills] = useState<SkillMeta[]>([])
  const loadSkills = (): void => {
    window.api.skills
      .list()
      .then(setSkills)
      .catch(() => {})
  }
  // 마운트 시 1회 + 설정 탭이 실제로 보일 때마다 새로고침(에이전트가 방금 만든 스킬 반영)
  useEffect(() => {
    if (active) loadSkills()
  }, [active])
  const removeSkill = async (name: string): Promise<void> => {
    const ok = await confirmDialog({
      title: '스킬 삭제',
      message: `'${name}' 스킬을 삭제합니다. 되돌릴 수 없습니다.\n계속할까요?`,
      confirmLabel: '삭제',
      danger: true
    })
    if (!ok) return
    await window.api.skills.remove(name)
    loadSkills()
  }
  const fmtSkillDate = (ms: number): string => {
    try {
      return new Date(ms).toLocaleDateString('ko-KR', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
      })
    } catch {
      return ''
    }
  }

  // ── 디스코드 봇 (MVP: 기본 채팅) — 소유자·채널·허용목록은 봇이 자동 처리 ──
  const [discordToken, setDiscordToken] = useState('')
  const [discordHasToken, setDiscordHasToken] = useState(false)
  const [discordApplying, setDiscordApplying] = useState(false)
  const [discordStat, setDiscordStat] = useState<DiscordStatus | null>(null)
  const [schedules, setSchedules] = useState<DiscordSchedule[]>([])
  const refreshDiscord = (): void => {
    window.api.discord.hasToken().then(setDiscordHasToken).catch(() => {})
    window.api.discord.status().then(setDiscordStat).catch(() => {})
    window.api.discord.schedules().then((r) => setSchedules(r.jobs ?? [])).catch(() => {})
  }
  const removeSchedule = async (id: string): Promise<void> => {
    await window.api.discord.scheduleRemove(id)
    refreshDiscord()
  }
  useEffect(() => {
    if (active) refreshDiscord()
  }, [active])
  const applyDiscord = async (): Promise<void> => {
    setDiscordApplying(true)
    try {
      if (!await onSave(form)) {
        setSaveError('설정을 저장하지 못해 Discord 연결을 적용하지 않았습니다.')
        return
      }
      setDirtyFields(new Set())
      if (discordToken.trim()) {
        await window.api.discord.saveToken(discordToken.trim())
        setDiscordToken('')
      }
      const r = await window.api.discord.apply()
      if (!r.ok) {
        // 적용 자체가 실패하면 그 사유를 표시하고, status 폴링이 이 메시지를 덮지 않도록 재조회를 건너뛴다.
        setDiscordStat({ running: false, last_error: r.detail ?? '알 수 없음' })
        return
      }
      // 봇 로그인 + 채널 생성까지 시간이 걸리므로 여러 번 뒤늦게 재조회
      refreshDiscord()
      window.setTimeout(refreshDiscord, 1500)
      window.setTimeout(refreshDiscord, 4000)
    } finally {
      setDiscordApplying(false)
    }
  }
  const discordStatusText = ((): string => {
    const s = discordStat
    if (!s) return '미확인'
    if (s.running) {
      const chan = s.channel_id
        ? '#aiso 채널 준비됨'
        : '⚠ 명령 채널 미생성 — 연결/적용을 다시 눌러 재시도하세요'
      const err = s.last_error ? ` · ${s.last_error}` : ''
      return `연결됨${s.user ? ` (${s.user})` : ''} · ${chan}${err}`
    }
    // 중지 상태의 사유: last_error(봇 오류) 또는 detail(백엔드 미준비 등)을 함께 노출한다.
    const reason = s.last_error || s.detail
    return reason ? `중지 · ${reason}` : '중지됨'
  })()

  // ── 디스코드 봇 만들기 안내 (설정탭 내 단계별 가이드) ──
  const [showDcGuide, setShowDcGuide] = useState(false)
  const [dcCopied, setDcCopied] = useState(false)
  // 봇 토큰의 첫 세그먼트는 base64url(Application ID)이라, 토큰만으로 App ID를 얻어 초대 링크를 만든다.
  // (별도 Application ID 입력 불필요. 이미 연결됐다면 봇 상태의 app_id로도 보완.)
  const appIdFromToken = (token: string): string => {
    const first = (token || '').trim().split('.')[0]
    if (!first) return ''
    try {
      const decoded = atob(first.replace(/-/g, '+').replace(/_/g, '/'))
      return /^\d{15,25}$/.test(decoded) ? decoded : ''
    } catch {
      return ''
    }
  }
  const dcAppId = appIdFromToken(discordToken) || discordStat?.app_id || ''
  // 봇 권한: 채널 보기(1024) + 메시지 보내기(2048) + 채널 관리(16) + 메시지 기록 읽기(65536) = 68624
  // integration_type=0(GUILD_INSTALL) — '서버에 추가' 흐름을 강제해 서버 선택 드롭다운이 뜨게 한다
  // (없으면 최근 Discord가 '내 앱에 추가(사용자 설치)' 화면으로 열려 서버 선택이 안 나옴).
  const dcInviteUrl = /^\d{15,25}$/.test(dcAppId)
    ? `https://discord.com/oauth2/authorize?client_id=${dcAppId}&permissions=68624&integration_type=0&scope=bot+applications.commands`
    : ''
  const copyDcInvite = (): void => {
    if (!dcInviteUrl) return
    void navigator.clipboard.writeText(dcInviteUrl).then(() => {
      setDcCopied(true)
      window.setTimeout(() => setDcCopied(false), 1500)
    })
  }

  const factoryReset = async (): Promise<void> => {
    const ok = await confirmDialog({
      title: '공장초기화',
      message:
        '설정·대화·토큰 사용량 기록이 모두 삭제되고 앱이 처음 설치 상태로 돌아갑니다.\n(작업 폴더의 파일과 RAG 색인은 그대로 유지됩니다.)\n계속할까요?',
      confirmLabel: '초기화',
      danger: true
    })
    if (!ok) return
    await window.api.factoryReset()
    window.location.reload()
  }

  const updBusy = upd.state === 'checking' || upd.state === 'downloading'
  const updHint = ((): string => {
    switch (upd.state) {
      case 'checking':
        return '릴리스 확인 중…'
      case 'up-to-date':
        return '최신 버전입니다.'
      case 'available':
        return `새 버전 v${upd.version} 이(가) 있습니다.`
      case 'downloading':
        return `다운로드 중… ${upd.percent ?? 0}%`
      case 'downloaded':
        return `v${upd.version} 다운로드 완료 — 재시작하면 설치됩니다.`
      case 'dev-disabled':
        return '개발 모드에선 확인할 수 없습니다 (설치본에서만 동작).'
      case 'error':
        return `오류: ${upd.message ?? '알 수 없음'}`
      default:
        return 'GitHub 릴리스에서 새 버전을 확인합니다.'
    }
  })()

  const navItems: { id: SectionId; label: string }[] = [
    { id: 'llm', label: 'LLM' },
    { id: 'comfy', label: 'ComfyUI' },
    { id: 'discord', label: '디스코드' },
    { id: 'appearance', label: '화면' },
    { id: 'tools', label: '도구' },
    { id: 'skills', label: '스킬' },
    { id: 'updates', label: '업데이트' },
    ...(form.devMode ? [{ id: 'developer' as SectionId, label: '개발자' }] : [])
  ]

  return (
    <div className="view view--settings">
      <header className="view__head">
        <h1>설정</h1>
        <p className="view__desc">AI 엔진과 화면 설정</p>
      </header>

      <div className="settings-layout">
        <nav className="settings-nav">
          {navItems.map((it) => (
            <button
              key={it.id}
              type="button"
              className={`settings-nav__item ${activeSection === it.id ? 'settings-nav__item--on' : ''}`}
              onClick={() => setActiveSection(it.id)}
            >
              {it.label}
            </button>
          ))}
        </nav>

        <div className="settings-content">
        {activeSection === 'llm' && (
        <section>
          <div className="group__title">엔진</div>
          <div className="group">
            {settingsRecoveryMessage && (
              <div className="row">
                <div>
                  <div className="row__label">설정 복구 알림</div>
                  <div className="row__hint">{settingsRecoveryMessage}</div>
                </div>
              </div>
            )}
            <div className="row">
              <div>
                <div className="row__label">LLM 공급자</div>
                <div className="row__hint">기존 로컬 실행은 Ollama를 사용합니다. NVIDIA 연결은 v4 개발 기능입니다.</div>
              </div>
              <Segmented
                value={form.activeLlmProvider}
                options={[
                  { v: 'ollama' as const, label: 'Ollama' },
                  { v: 'nvidia' as const, label: 'NVIDIA' }
                ]}
                onChange={(v) => set('activeLlmProvider', v)}
              />
            </div>
            {form.activeLlmProvider === 'ollama' ? (
              <>
            <div className="row">
              <div>
                <div className="row__label">모델</div>
                <div className="row__hint">
                  {modelOptions.length > 0
                    ? '설치된 모델 중에서 선택합니다.'
                    : 'Ollama를 연결하면 설치된 모델이 여기에 표시됩니다.'}
                </div>
              </div>
              <div className="row__control">
                {useDropdown ? (
                  <Select
                    value={form.model}
                    options={modelOptions}
                    onChange={(v) => set('model', v)}
                  />
                ) : (
                  <input
                    className="input"
                    value={form.model}
                    onChange={(e) => set('model', e.target.value)}
                    placeholder="gemma4:12b"
                  />
                )}
                {modelOptions.length > 0 && (
                  <button
                    type="button"
                    className="linklike"
                    onClick={() => setManualModel((m) => !m)}
                  >
                    {useDropdown ? '직접 입력' : '목록에서 선택'}
                  </button>
                )}
              </div>
            </div>
            <div className="row">
              <div>
                <div className="row__label">Ollama 호스트</div>
                <div className="row__hint">Ollama 서버 주소이며, 보통 기본값을 그대로 사용합니다.</div>
              </div>
              <input
                className="input"
                value={form.ollamaHost}
                onChange={(e) => set('ollamaHost', e.target.value)}
                placeholder="http://localhost:11434"
              />
            </div>
              </>
            ) : (
              <>
                <div className="row">
                  <div>
                    <div className="row__label">NVIDIA 배포 방식</div>
                    <div className="row__hint">NVIDIA Build 또는 실험적인 사용자 NIM을 선택합니다.</div>
                  </div>
                  <Segmented
                    value={form.nvidiaDeploymentMode}
                    options={[
                      { v: 'build' as const, label: 'NVIDIA Build' },
                      { v: 'nim' as const, label: '사용자 NIM' }
                    ]}
                    onChange={(v) => set('nvidiaDeploymentMode', v)}
                  />
                </div>
                <div className="row">
                  <div>
                    <div className="row__label">NVIDIA 모델</div>
                    <div className="row__hint">모델 자동 조회는 다음 개발 단계에서 연결됩니다.</div>
                  </div>
                  <input
                    className="input"
                    value={form.nvidiaModel}
                    onChange={(e) => set('nvidiaModel', e.target.value)}
                    placeholder="예: nvidia/llama-3.1-nemotron-ultra-253b-v1"
                  />
                </div>
                <div className="row">
                  <div>
                    <div className="row__label">엔드포인트</div>
                    <div className="row__hint">
                      {form.nvidiaDeploymentMode === 'build'
                        ? 'NVIDIA Build 주소는 보안을 위해 고정되며 리디렉션을 허용하지 않습니다.'
                        : '로컬 HTTP 또는 외부 HTTPS만 허용합니다. 사용자 정보·query·fragment는 거부됩니다.'}
                    </div>
                  </div>
                  <input
                    className="input"
                    readOnly={form.nvidiaDeploymentMode === 'build'}
                    value={form.nvidiaDeploymentMode === 'build' ? NVIDIA_BUILD_BASE_URL : form.nvidiaNimEndpoint}
                    onChange={(e) => set('nvidiaNimEndpoint', e.target.value)}
                    placeholder="https://nim.example.com/v1"
                  />
                </div>
                <div className="row">
                  <div>
                    <div className="row__label">NVIDIA API 키</div>
                    <div className="row__hint">
                      {credentialStatus?.hasStoredCredential
                        ? credentialStatus.matchesCurrentBinding
                          ? '현재 배포 대상에 맞는 키가 암호화되어 있습니다.'
                          : '저장된 키가 다른 배포 대상에 묶여 있습니다. 새 키로 교체하세요.'
                        : '키는 일반 설정에 저장되지 않으며 Renderer로 다시 반환되지 않습니다.'}
                    </div>
                  </div>
                  <div className="row__control">
                    <input
                      className="input"
                      type="password"
                      autoComplete="off"
                      value={nvidiaApiKey}
                      onChange={(e) => setNvidiaApiKey(e.target.value)}
                      placeholder={credentialStatus?.hasStoredCredential ? '새 키로 교체' : 'API 키 입력'}
                    />
                    <div>
                      <button
                        type="button"
                        className="btn"
                        disabled={credentialBusy || !nvidiaApiKey.trim() || credentialStatus?.encryptionAvailable === false}
                        onClick={() => void saveCredential()}
                      >
                        {credentialStatus?.hasStoredCredential ? '키 교체' : '키 저장'}
                      </button>{' '}
                      <button
                        type="button"
                        className="btn btn--stop"
                        disabled={credentialBusy || !credentialStatus?.hasStoredCredential}
                        onClick={() => void removeCredential()}
                      >
                        키 삭제
                      </button>
                    </div>
                  </div>
                </div>
                {credentialMessage && <div className="row__hint" role="status">{credentialMessage}</div>}
                <div className="row">
                  <div>
                    <div className="row__label">v4 Gate 2 제한</div>
                    <div className="row__hint">
                      현재 단계는 설정·키 보관만 준비하며 NVIDIA 요청과 모델 목록 조회는 실행하지 않습니다.
                      이후 NVIDIA Build 실행 시 승인한 프롬프트·문맥이 NVIDIA 클라우드로 전송되고,
                      사용자 NIM은 해당 서버 운영자에게 전송될 수 있습니다.
                    </div>
                  </div>
                </div>
              </>
            )}
          </div>
        </section>
        )}
        <section hidden={activeSection !== 'comfy'} aria-hidden={activeSection !== 'comfy'}>
          <div className="group__title">ComfyUI 연결 및 모델 등록</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">ComfyUI 설치 폴더</div>
                <div className="row__hint">
                  Windows Portable의 최상위 폴더를 선택하세요. 이 경로를 저장하면 Aiso가 ComfyUI를 자동으로 실행하고 모델 파일을 연결할 수 있습니다.
                </div>
              </div>
              <div className="row__control comfy-setting-path">
                <input
                  className="input"
                  value={form.comfyInstallPath}
                  readOnly
                  placeholder="선택되지 않음"
                  title={form.comfyInstallPath || undefined}
                />
                <button className="btn btn--ghost2 btn--sm" type="button" onClick={() => void pickComfyInstall()}>
                  폴더 선택
                </button>
              </div>
            </div>
            <div className="row">
              <div>
                <div className="row__label">ComfyUI 서버 주소</div>
                <div className="row__hint">
                  기본값은 이 PC에서 실행되는 ComfyUI입니다. Aiso는 보안을 위해 127.0.0.1, localhost 또는 ::1 주소만 연결합니다.
                </div>
              </div>
              <input
                className="input"
                value={form.comfyBaseUrl}
                onChange={(e) => set('comfyBaseUrl', e.target.value)}
                placeholder="http://127.0.0.1:8188"
                spellCheck={false}
              />
            </div>
            <div className="row">
              <div>
                <div className="row__label">연동 방식</div>
                <div className="row__hint">
                  ComfyUI는 Aiso에 포함되지 않으며 별도 프로세스로 실행됩니다. 모델 설치·업데이트·삭제도 사용자가 직접 관리합니다.
                </div>
              </div>
              <span className="model-chip">사용자 설치 ComfyUI</span>
            </div>
            <div className="row">
              <div>
                <div className="row__label">이미지 모델 선택</div>
                <div className="row__hint">
                  {form.comfyModelSelectionMode === 'auto'
                    ? '자동: Agent가 요청과 등록된 모델 특성을 비교해 사용할 모델을 선택합니다.'
                    : '수동: Agent 입력창 아래에서 이미지 생성에 사용할 등록 모델을 직접 선택합니다.'}
                </div>
              </div>
              <Segmented
                value={form.comfyModelSelectionMode}
                options={[
                  { v: 'auto', label: '자동' },
                  { v: 'manual', label: '수동' }
                ]}
                onChange={(v) => set('comfyModelSelectionMode', v as ComfyModelSelectionMode)}
              />
            </div>
          </div>
          {comfyConnectionDirty && (
            <div className="comfy-setting-notice">
              {comfyInstallPathDirty
                ? <>설치 폴더를 변경하면 안전을 위해 기존 모델의 Agent 자동 선택이 꺼집니다. 새 설치본에서 모델을 확인한 뒤 다시 켜 주세요.</>
                : <>ComfyUI 서버 주소가 변경되었습니다. 화면 오른쪽 아래의 <b>저장</b>을 누르면 자동 실행과 Agent 연결에 적용됩니다.</>}
            </div>
          )}
          <ComfyModelManager
            installPath={settings.comfyInstallPath}
            hasUnsavedInstallPathChange={comfyInstallPathDirty}
          />
        </section>
        {activeSection === 'llm' && (
        <section>
          <div className="group__title">생성</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">추론 강도</div>
                <div className="row__hint">생각의 깊이를 정합니다. Low는 빠르게, High는 더 꼼꼼하게 답합니다.</div>
              </div>
              <Segmented
                value={form.reasoningEffort}
                options={EFFORTS}
                onChange={(v) => set('reasoningEffort', v)}
              />
            </div>
            <div className="row">
              <div>
                <div className="row__label">생성 온도</div>
                <div className="row__hint">
                  자동은 요청에 맞춰 온도를 조절하고, 커스텀에서만 값을 직접 정합니다.
                </div>
              </div>
              <Dropdown
                value={form.tempPreset}
                options={TEMP_MODE_OPTIONS}
                onChange={(v) => set('tempPreset', v as TempPreset)}
                align="right"
                title="생성 온도 모드"
              />
            </div>
            {form.tempPreset === 'custom' && (
              <div className="row">
                <div>
                  <div className="row__label">커스텀 온도</div>
                  <div className="row__hint">값이 낮으면 일관되게, 높으면 창의적으로 답합니다.</div>
                </div>
                <div className="temp">
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.05}
                    value={form.tempCustom}
                    onChange={(e) => set('tempCustom', Number(e.target.value))}
                    className="range"
                    style={rangeFill(form.tempCustom * 100)}
                  />
                  <span className="temp__val mono">{form.tempCustom.toFixed(2)}</span>
                </div>
              </div>
            )}
            <div className="row">
              <div>
                <div className="row__label">컨텍스트 길이</div>
                <div className="row__hint">모델이 한 번에 기억하는 양으로, 클수록 긴 대화가 가능하지만 VRAM을 더 사용합니다.</div>
              </div>
              <div className="temp">
                <input
                  type="range"
                  min={0}
                  max={TOKEN_STEPS.length - 1}
                  step={1}
                  value={nearestTokenIndex(form.contextLength)}
                  onChange={(e) => set('contextLength', TOKEN_STEPS[Number(e.target.value)])}
                  className="range range--stepped"
                  style={rangeFill((nearestTokenIndex(form.contextLength) / (TOKEN_STEPS.length - 1)) * 100)}
                  list="token-steps"
                />
                <datalist id="token-steps">
                  {TOKEN_STEPS.map((_, i) => (
                    <option key={i} value={i} />
                  ))}
                </datalist>
                <span className="temp__val mono">
                  {TOKEN_LABELS[nearestTokenIndex(form.contextLength)]}
                </span>
              </div>
            </div>
          </div>
        </section>
        )}
        {activeSection === 'llm' && (
        <>
        <section>
          <div className="group__title">웹 검색</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">채팅 웹 검색</div>
                <div className="row__hint">
                  최신 정보가 필요하면 <b>자동으로</b> 인터넷에서 여러 출처를 확인해 답합니다. 끄면 모두 로컬에서만 처리합니다.
                </div>
              </div>
              <Segmented
                value={form.chatWebSearch}
                options={[
                  { v: true, label: '자동' },
                  { v: false, label: '끔' }
                ]}
                onChange={(v) => set('chatWebSearch', v)}
              />
            </div>
          </div>
        </section>

        <section>
          <div className="group__title">RAG · 검색 증강</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">RAG 사용</div>
                <div className="row__hint">작업 폴더의 코드와 문서를 미리 읽어 두고, 관련 내용을 답변에 자동으로 참고합니다.</div>
              </div>
              <Segmented
                value={form.ragEnabled}
                options={[
                  { v: true, label: '사용' },
                  { v: false, label: '끔' }
                ]}
                onChange={(v) => set('ragEnabled', v)}
              />
            </div>
            <div className="row">
              <div>
                <div className="row__label">임베딩 모델</div>
                <div className="row__hint">
                  문서 검색 전용 모델로, 채팅 모델과 분리되어 있어 바꿔도 문제없습니다. 한국어는 bge-m3를 권장합니다.
                </div>
              </div>
              <input
                className="input"
                value={form.embeddingModel}
                onChange={(e) => set('embeddingModel', e.target.value)}
                placeholder="bge-m3"
              />
            </div>
            <div className="row">
              <div>
                <div className="row__label">색인 범위 (최대 파일 수)</div>
                <div className="row__hint">
                  범위를 넓힐수록 더 많이 참고하지만 준비가 느려집니다. 폴더가 크면 값을 늘리도록 안내합니다.
                </div>
              </div>
              <Dropdown
                value={String(form.ragMaxFiles)}
                options={RAG_MAX_FILES_OPTIONS.map((o) => ({
                  value: String(o.value),
                  label: o.label
                }))}
                onChange={(v) => set('ragMaxFiles', Number(v))}
                align="right"
                title="RAG 색인에 넣을 최대 파일 수"
              />
            </div>
          </div>
        </section>
        </>
        )}
        {activeSection === 'llm' && (
        <section>
          <div className="group__title">리소스</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">AI 즉시 정지 (GPU 비우기)</div>
                <div className="row__hint">
                  {dropMsg ||
                    'GPU가 급히 필요할 때, 메모리에 올라간 AI 모델을 지금 바로 내립니다. 생성 중이면 그 작업이 끝난 뒤 내려갑니다.'}
                </div>
              </div>
              <button className="btn btn--stop" disabled={dropping} onClick={() => void dropAI()}>
                {dropping ? '내리는 중…' : 'AI 언로드'}
              </button>
            </div>
            <div className="row">
              <div>
                <div className="row__label">모델 상주 유지</div>
                <div className="row__hint">
                  사용하지 않을 때도 모델을 메모리에 유지합니다. 다시 쓸 때 로딩(약 5.8초)이 없어지지만 VRAM을 계속 사용합니다.
                </div>
              </div>
              <Segmented
                value={form.keepAlive}
                options={KEEP_ALIVE}
                onChange={(v) => set('keepAlive', v)}
              />
            </div>
          </div>
        </section>
        )}
        {activeSection === 'tools' && (
        <section>
          <AgentToolCatalog backend={backend} active={active} />
        </section>
        )}
        {activeSection === 'appearance' && (
        <section>
          <div className="group__title">화면</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">테마</div>
                <div className="row__hint">시스템은 OS 설정을 따릅니다.</div>
              </div>
              <Segmented value={form.theme} options={THEMES} onChange={(v) => set('theme', v)} />
            </div>
          </div>
        </section>
        )}
        {activeSection === 'updates' && (
        <section>
          <div className="group__title">업데이트</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">버전{appVersion && ` · v${appVersion}`}</div>
                <div className="row__hint">{updHint}</div>
              </div>
              <div className="update-actions">
                {upd.state === 'downloaded' ? (
                  <button className="btn" onClick={() => void window.api.updates.install()}>
                    재시작하여 설치
                  </button>
                ) : upd.state === 'available' ? (
                  <button className="btn" onClick={() => void window.api.updates.download()}>
                    v{upd.version} 다운로드
                  </button>
                ) : (
                  <button className="btn btn--ghost2" disabled={updBusy} onClick={checkUpdate}>
                    {upd.state === 'checking' ? '확인 중…' : '업데이트 확인'}
                  </button>
                )}
              </div>
            </div>
            {upd.state === 'downloading' && (
              <div className="row">
                <div className="update-progress">
                  <div
                    className="update-progress__bar"
                    style={{ width: `${upd.percent ?? 0}%` }}
                  />
                </div>
                <span className="temp__val mono">{upd.percent ?? 0}%</span>
              </div>
            )}
          </div>
        </section>
        )}
        {activeSection === 'skills' && (
        <section>
          <div className="group__title">스킬</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">만들어진 스킬</div>
                <div className="row__hint">
                  에이전트가 만든 자동화 도구이며, 여기서 확인하거나 삭제할 수 있습니다.
                </div>
              </div>
              <button className="btn btn--ghost2" onClick={loadSkills}>
                새로고침
              </button>
            </div>
            {skills.length === 0 ? (
              <div className="row">
                <div className="row__hint">
                  아직 만든 스킬이 없습니다. 에이전트에게 반복 작업(예: 알람, 브리핑)을 자동화해 달라고 하면 여기에 추가됩니다.
                </div>
              </div>
            ) : (
              skills.map((s) => (
                <div className="row" key={s.name}>
                  <div>
                    <div className="row__label mono">{s.name}</div>
                    <div className="row__hint">
                      {s.description || '(설명 없음)'}
                      {s.created ? ` · ${fmtSkillDate(s.created)}` : ''}
                    </div>
                  </div>
                  <button className="btn btn--stop" onClick={() => void removeSkill(s.name)}>
                    삭제
                  </button>
                </div>
              ))
            )}
          </div>
        </section>
        )}
        {activeSection === 'discord' && (
        <section>
          <div className="group__title">디스코드 봇 (기본 채팅)</div>
          <div className="group">
            <div className="row">
              <div>
                <div className="row__label">디스코드 봇</div>
                <div className="row__hint">
                  봇 토큰을 넣고 서버에 초대하면 <b>소유자, 명령 채널, 허용목록이 자동으로</b> 설정됩니다(초대 시
                  전용 채널을 만들고 그 채널에서 대화합니다). 개발자 포털에서 <b>Message Content Intent</b>를 켜야 하며,
                  초대 권한에 <b>Manage Channels</b>가 필요합니다. 기본적으로 앱이 켜져 있는 동안에만 동작하며,
                  아래 <b>백그라운드 상주</b>를 켜면 창을 닫아도 계속 작동합니다.
                </div>
              </div>
              <Segmented
                value={form.discordEnabled}
                options={ONOFF}
                onChange={(v) => set('discordEnabled', v)}
              />
            </div>
            <div className="row">
              <div>
                <div className="row__label">백그라운드 상주</div>
                <div className="row__hint">
                  창을 닫아도 앱이 트레이(작업표시줄 우측)에 남아 봇과 예약이 계속 작동합니다. 끄면 창을 닫을 때
                  앱이 종료되어 봇도 멈춥니다. 완전히 끄려면 트레이 아이콘의 <b>완전 종료</b>를 사용합니다.
                </div>
              </div>
              <Segmented
                value={form.trayResident}
                options={ONOFF}
                onChange={(v) => set('trayResident', v)}
              />
            </div>
            <div className="row">
              <div>
                <div className="row__label">로그인 시 자동 실행</div>
                <div className="row__hint">
                  윈도우에 로그인하면 앱이 트레이로 자동 실행되어 봇이 다시 연결됩니다. PC를 켜 두는 한 항상
                  작동하게 하려면 백그라운드 상주와 함께 켜세요.
                </div>
              </div>
              <Segmented
                value={form.autoLaunch}
                options={ONOFF}
                onChange={(v) => set('autoLaunch', v)}
              />
            </div>
            <div className="row">
              <div>
                <div className="row__label">봇 만들기 안내</div>
                <div className="row__hint">처음이라면 아래 단계를 따라 하세요.</div>
              </div>
              <button className="btn btn--ghost2" onClick={() => setShowDcGuide((v) => !v)}>
                {showDcGuide ? '닫기' : '안내 보기'}
              </button>
            </div>
            {showDcGuide && (
              <div className="dc-guide">
                <ol className="dc-guide__steps">
                  <li>
                    <b>개발자 포털에서 앱 만들기</b> —{' '}
                    <a
                      href="https://discord.com/developers/applications"
                      target="_blank"
                      rel="noreferrer noopener"
                    >
                      개발자 포털 열기
                    </a>{' '}
                    → New Application → 이름을 정합니다.
                  </li>
                  <li>
                    <b>봇 토큰 발급</b> — 좌측 <b>Bot</b> 메뉴에서 Reset Token으로 토큰을 복사해 아래 봇 토큰란에
                    붙여넣습니다.
                  </li>
                  <li>
                    <b>Message Content Intent 켜기 (필수)</b> — 같은 Bot 화면의 Privileged Gateway Intents에서
                    MESSAGE CONTENT INTENT를 켭니다. <b>이걸 켜지 않으면 봇이 메시지를 읽지 못해 답하지 않습니다.</b>{' '}
                    (Presence·Server Members는 켤 필요 없습니다.)
                  </li>
                  <li>
                    <b>봇 초대</b> — 아래 봇 토큰란에 토큰을 넣으면 <b>초대 링크가 자동으로</b> 만들어집니다(토큰에
                    Application ID가 들어 있어 따로 입력할 필요가 없습니다). 링크를 열어 서버를 고르고 승인합니다.
                    <div className="dc-guide__invite">
                      {dcInviteUrl ? (
                        <div className="dc-guide__url">
                          <a href={dcInviteUrl} target="_blank" rel="noreferrer noopener">
                            초대 링크 열기
                          </a>
                          <button className="btn btn--ghost2" onClick={copyDcInvite}>
                            {dcCopied ? '복사됨' : '링크 복사'}
                          </button>
                        </div>
                      ) : (
                        <span className="dc-guide__pending">봇 토큰을 넣으면 초대 링크가 여기에 나타납니다.</span>
                      )}
                    </div>
                  </li>
                  <li>
                    <b>연결</b> — 봇 토큰을 붙여넣었으면 <b>연결/적용</b>을 누릅니다.
                  </li>
                </ol>
              </div>
            )}
            <div className="row">
              <div>
                <div className="row__label">봇 토큰</div>
                <div className="row__hint">
                  {discordHasToken ? '저장됨 · 새 값 입력 시 교체' : '아직 없음'} · 암호화 저장(설정에 평문 미보관)
                </div>
              </div>
              <input
                className="input"
                type="password"
                value={discordToken}
                onChange={(e) => setDiscordToken(e.target.value)}
                placeholder={discordHasToken ? '••••••(교체하려면 입력)' : '봇 토큰 붙여넣기'}
              />
            </div>
            <div className="row">
              <div>
                <div className="row__label">연결 상태</div>
                <div className="row__hint">
                  {discordStatusText}
                  {discordStat?.running && discordStat.allowlist ? (
                    <>
                      {' · '}허용 사용자 {discordStat.allowlist.length}명 (디스코드에서 <span className="mono">/allow</span>로 관리)
                    </>
                  ) : null}
                </div>
              </div>
              <button className="btn" disabled={discordApplying} onClick={() => void applyDiscord()}>
                {discordApplying ? '적용 중…' : '연결/적용'}
              </button>
            </div>
            <div className="row">
              <div style={{ width: '100%' }}>
                <div className="row__label">예약</div>
                <div className="row__hint">
                  디스코드 #aiso 채널에서 &quot;오후 8시에 공지방에 회의 알림 보내&quot; 또는 &quot;매일 아침 8시에
                  날씨 브리핑해줘&quot;처럼 자연어로 등록합니다. 예약은 앱이 켜져 있는 동안에만 발화됩니다.
                </div>
                {schedules.length === 0 ? (
                  <div className="row__hint" style={{ marginTop: 8 }}>등록된 예약이 없습니다.</div>
                ) : (
                  <ul className="sched-list">
                    {schedules.map((j) => (
                      <li key={j.id} className="sched-item">
                        <div className="sched-item__main">
                          <span className={`sched-item__tag${j.kind === 'briefing' ? ' sched-item__tag--brief' : ''}`}>
                            {j.kind === 'briefing' ? '브리핑' : '메시지'}
                          </span>
                          <span className="sched-item__when">
                            {j.repeat === 'daily' ? '매일 ' : ''}
                            {String(j.next_run ?? '').replace('T', ' ')} → #{j.channel_name}
                          </span>
                          <div className="sched-item__text">{j.text}</div>
                        </div>
                        <button className="btn btn--sm btn--stop" onClick={() => void removeSchedule(j.id)}>
                          삭제
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          </div>
        </section>
        )}
        {activeSection === 'developer' && form.devMode && (
          <section>
            <div className="group__title">개발자</div>
            <div className="group">
              <div className="row">
                <div>
                  <div className="row__label">온보딩 미리보기</div>
                  <div className="row__hint">
                    모델을 지우지 않고 첫 설치 안내 화면을 다시 표시합니다(저장 후 홈에서 확인할 수 있습니다).
                  </div>
                </div>
                <Segmented
                  value={form.forceOnboarding}
                  options={ONOFF}
                  onChange={(v) => set('forceOnboarding', v)}
                />
              </div>
              <div className="row">
                <div>
                  <div className="row__label">공장초기화</div>
                  <div className="row__hint">
                    설정과 대화, 사용량 기록을 모두 지우고 처음 상태로 되돌립니다(되돌릴 수 없습니다).
                  </div>
                </div>
                <button className="btn btn--stop" onClick={() => void factoryReset()}>
                  초기화
                </button>
              </div>
              <div className="row">
                <div>
                  <div className="row__label">개발자 모드 끄기</div>
                  <div className="row__hint">
                    다시 켜려면 이 화면에서 Ctrl+Shift+F1을 10회 누르세요.
                  </div>
                </div>
                <button className="btn btn--ghost2" onClick={disableDevMode}>
                  끄기
                </button>
              </div>
            </div>
          </section>
        )}

          <div className="actions">
            {saved && <span className="saved">저장됨</span>}
            {saveError && <span className="form-error" role="alert">{saveError}</span>}
            <button className="btn" disabled={!dirty} onClick={submit}>
              저장
            </button>
          </div>
        </div>
      </div>
      {devToast && <div className="dev-toast">{devToast}</div>}
    </div>
  )
}

export default SettingsView
