import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { AppSettings } from '../../../shared/settings'
import type { BackendInfo } from '../../../shared/backend'
import type { ComfyHealthInfo, ComfyLaunchResult } from '../../../shared/comfy'
import type { ComfyModelProfile } from '../../../shared/comfy-model'
import { getComfyAgentReadiness } from '../../../shared/comfy-model'
import { RefreshIcon } from '../components/icons'
import {
  fetchComfyCheckpoints,
  fetchComfyHealth,
  normalizeLocalComfyUrl
} from '../lib/comfy'

interface Props {
  settings: AppSettings
  backend: BackendInfo
  active: boolean
  onSaveSettings: (patch: Partial<AppSettings>) => Promise<void>
}

function gib(bytes?: number): string {
  return typeof bytes === 'number' && Number.isFinite(bytes)
    ? `${(bytes / 1024 ** 3).toFixed(1)} GB`
    : ''
}

function launchMessage(result: ComfyLaunchResult): string {
  if (!result.ok) return result.detail ?? 'ComfyUI를 시작하지 못했습니다.'
  if (result.state === 'already-running') return result.detail ?? '실행 중인 ComfyUI에 연결합니다.'
  if (result.state === 'already-started') return 'ComfyUI가 시작되는 중입니다.'
  return 'ComfyUI를 시작했습니다. 초기 로딩을 기다리고 있습니다.'
}

function ComfyView({ settings, backend, active, onSaveSettings }: Props): React.JSX.Element {
  const safeBaseUrl = useMemo(
    () => normalizeLocalComfyUrl(settings.comfyBaseUrl),
    [settings.comfyBaseUrl]
  )
  const [health, setHealth] = useState<ComfyHealthInfo | null>(null)
  const [checkpoints, setCheckpoints] = useState<string[]>([])
  const [registeredModels, setRegisteredModels] = useState<ComfyModelProfile[]>([])
  const [checking, setChecking] = useState(false)
  const [starting, setStarting] = useState(false)
  const [message, setMessage] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const [showModels, setShowModels] = useState(false)
  const autoStartKey = `${settings.comfyInstallPath}\n${safeBaseUrl ?? ''}`
  const autoStartedRef = useRef('')
  const startTimeoutRef = useRef<number | null>(null)
  const surfaceRef = useRef<HTMLDivElement>(null)

  useEffect(() => () => {
    if (startTimeoutRef.current != null) window.clearTimeout(startTimeoutRef.current)
  }, [])

  useEffect(() => {
    autoStartedRef.current = ''
    if (startTimeoutRef.current != null) {
      window.clearTimeout(startTimeoutRef.current)
      startTimeoutRef.current = null
    }
    setStarting(false)
    setHealth(null)
    setCheckpoints([])
    setMessage('')
  }, [autoStartKey])

  useEffect(() => {
    if (!active) return
    let alive = true
    window.api.comfy.models.list()
      .then((registry) => {
        if (alive) setRegisteredModels(registry.profiles)
      })
      .catch(() => {
        if (alive) setRegisteredModels([])
      })
    return () => {
      alive = false
    }
  }, [active, refreshKey])

  const startServer = useCallback(async (): Promise<void> => {
    if (!settings.comfyInstallPath) {
      setMessage('먼저 ComfyUI Windows Portable 폴더를 선택해 주세요.')
      return
    }
    setStarting(true)
    setMessage('ComfyUI를 시작하고 있습니다…')
    try {
      const result = await window.api.comfy.start()
      setMessage(launchMessage(result))
      if (!result.ok || result.state === 'already-running') {
        setStarting(false)
      } else {
        if (startTimeoutRef.current != null) window.clearTimeout(startTimeoutRef.current)
        startTimeoutRef.current = window.setTimeout(() => {
          setStarting(false)
          setMessage('60초 안에 ComfyUI 응답을 받지 못했습니다. 실행 로그와 설정 주소를 확인해 주세요.')
        }, 60_000)
      }
    } catch (err) {
      setStarting(false)
      setMessage(`ComfyUI 시작 실패: ${String(err)}`)
    }
  }, [settings.comfyInstallPath])

  const check = useCallback(async (signal: AbortSignal): Promise<ComfyHealthInfo | null> => {
    if (backend.state !== 'ready' || backend.port == null || !safeBaseUrl) return null
    setChecking(true)
    try {
      const next = await fetchComfyHealth(backend.port, safeBaseUrl, signal)
      if (signal.aborted) return null
      setHealth(next)
      if (next.online) {
        if (startTimeoutRef.current != null) {
          window.clearTimeout(startTimeoutRef.current)
          startTimeoutRef.current = null
        }
        setStarting(false)
        setMessage('')
        try {
          const models = await fetchComfyCheckpoints(backend.port, safeBaseUrl, signal)
          if (!signal.aborted) setCheckpoints(models.checkpoints)
        } catch (err) {
          if (!signal.aborted) setMessage(`체크포인트 조회 실패: ${String(err)}`)
        }
      } else {
        setCheckpoints([])
      }
      return next
    } catch (err) {
      if (!signal.aborted) {
        setHealth(null)
        setCheckpoints([])
        setMessage(String(err))
      }
      return null
    } finally {
      if (!signal.aborted) setChecking(false)
    }
  }, [backend.port, backend.state, safeBaseUrl])

  // 최초 ComfyUI 탭 진입 때만 연결한다. 설치 경로가 있으면 오프라인 상태에서 한 번 자동 시작한다.
  useEffect(() => {
    if (!active || backend.state !== 'ready' || backend.port == null || !safeBaseUrl) return
    const controller = new AbortController()
    let timer = 0
    const poll = async (): Promise<void> => {
      const next = await check(controller.signal)
      if (controller.signal.aborted) return
      if (settings.comfyInstallPath && autoStartedRef.current !== autoStartKey) {
        autoStartedRef.current = autoStartKey
        await startServer()
        timer = window.setTimeout(poll, 1000)
        return
      }
      timer = window.setTimeout(poll, next?.online ? 5000 : 2000)
    }
    void poll()
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [active, autoStartKey, backend.port, backend.state, check, refreshKey, safeBaseUrl, settings.comfyInstallPath, startServer])

  // 원본 UI는 별도 WebContentsView로 격리한다. DOM 자리의 실제 화면 좌표만 제한된 IPC로 전달한다.
  useLayoutEffect(() => {
    if (!window.api?.comfy?.setSurface) return
    if (!active || health?.online !== true || !safeBaseUrl) {
      void window.api.comfy.setSurface({ visible: false, baseUrl: safeBaseUrl ?? '' })
      return
    }
    const element = surfaceRef.current
    if (!element) return
    let disposed = false
    const syncBounds = (): void => {
      if (disposed) return
      const rect = element.getBoundingClientRect()
      void window.api.comfy.setSurface({
        visible: true,
        baseUrl: safeBaseUrl,
        bounds: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
      }).catch((err) => {
        if (!disposed) setMessage(`ComfyUI 화면 표시 실패: ${String(err)}`)
      })
    }
    syncBounds()
    const observer = new ResizeObserver(syncBounds)
    observer.observe(element)
    window.addEventListener('resize', syncBounds)
    return () => {
      disposed = true
      observer.disconnect()
      window.removeEventListener('resize', syncBounds)
      void window.api.comfy.setSurface({ visible: false, baseUrl: safeBaseUrl })
    }
  }, [active, health?.online, refreshKey, safeBaseUrl])

  const chooseInstall = async (): Promise<void> => {
    try {
      const selected = await window.api.comfy.pickInstall()
      if (selected) await onSaveSettings({ comfyInstallPath: selected })
    } catch (err) {
      setMessage(`폴더 선택 실패: ${String(err)}`)
    }
  }

  const online = health?.online === true
  const statusText = online
    ? `연결됨${health.version ? ` · v${health.version}` : ''}`
    : starting
      ? '시작 중'
      : checking
        ? '확인 중'
        : '연결 안 됨'
  const device = health?.devices[0]

  if (!safeBaseUrl) {
    return (
      <div className="comfy-view comfy-view--center">
        <div className="comfy-setup">
          <div className="comfy-setup__title">ComfyUI 주소를 확인해 주세요</div>
          <p>설정의 ComfyUI 주소에는 <code>http://127.0.0.1:8188</code> 같은 로컬 주소만 사용할 수 있습니다.</p>
        </div>
      </div>
    )
  }

  if (!online) {
    const backendUnavailable = backend.state !== 'ready' || backend.port == null
    return (
      <div className="comfy-view comfy-view--center">
        <div className="comfy-setup" aria-live="polite">
          <div className="comfy-setup__eyebrow">COMFYUI · {statusText}</div>
          <div className="comfy-setup__title">Aiso에서 ComfyUI 열기</div>
          <p>
            사용자가 설치한 Windows Portable을 별도 프로세스로 실행하고, 로컬 UI를 이 화면에 표시합니다.
            Aiso는 ComfyUI나 모델을 다운로드·수정하지 않습니다.
          </p>
          <div className="comfy-path" title={settings.comfyInstallPath || undefined}>
            {settings.comfyInstallPath || '설치 폴더가 아직 등록되지 않았습니다.'}
          </div>
          {message && <div className="comfy-message">{message}</div>}
          {!message && health?.detail && <div className="comfy-message">{health.detail}</div>}
          {backendUnavailable && <div className="comfy-message">Aiso 백엔드를 먼저 시작해야 합니다.</div>}
          <div className="comfy-actions">
            <button className="btn btn--ghost2" type="button" onClick={() => void chooseInstall()}>
              {settings.comfyInstallPath ? '폴더 다시 선택' : 'Portable 폴더 선택'}
            </button>
            <button
              className="btn"
              type="button"
              disabled={!settings.comfyInstallPath || starting || backendUnavailable}
              onClick={() => {
                autoStartedRef.current = autoStartKey
                void startServer().then(() => setRefreshKey((v) => v + 1))
              }}
            >
              {starting ? '시작 중…' : 'ComfyUI 시작'}
            </button>
            <button
              className="btn btn--ghost2"
              type="button"
              disabled={checking || backendUnavailable}
              onClick={() => setRefreshKey((v) => v + 1)}
            >
              연결 다시 확인
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="comfy-view">
      <header className="comfy-toolbar">
        <div className="comfy-toolbar__identity">
          <strong>ComfyUI</strong>
          <span className="comfy-status comfy-status--online">{statusText}</span>
          {device && (
            <span className="comfy-meta" title={device.name}>
              {device.type.toUpperCase()}{device.vramTotal ? ` · ${gib(device.vramTotal)}` : ''}
            </span>
          )}
        </div>
        <div className="comfy-toolbar__actions">
          <button
            className="comfy-models"
            type="button"
            aria-expanded={showModels}
            onClick={() => setShowModels((v) => !v)}
          >
            등록 {registeredModels.length} · 체크포인트 {checkpoints.length}
          </button>
          <a className="btn btn--ghost2 comfy-external" href={safeBaseUrl} target="_blank" rel="noreferrer">
            브라우저로 열기
          </a>
          <button
            className="btn btn--ghost2 comfy-refresh"
            type="button"
            aria-label="ComfyUI 새로고침"
            data-tip="ComfyUI 새로고침"
            onClick={() => {
              setRefreshKey((v) => v + 1)
              void window.api.comfy.reloadSurface()
            }}
          >
            <RefreshIcon />
          </button>
        </div>
      </header>
      {checkpoints.length === 0 && registeredModels.length === 0 && (
        <div className="comfy-warning" role="status">
          등록된 모델이 없습니다. 설정 → ComfyUI → 모델 라이브러리에서 <code>.safetensors</code> 파일을 가져오세요.
        </div>
      )}
      {showModels && (
        <div className="comfy-model-panel">
          <div className="comfy-model-panel__title">Aiso 모델 프로필</div>
          {registeredModels.length === 0
            ? <div>등록된 프로필이 없습니다.</div>
            : registeredModels.map((profile) => {
                const readiness = getComfyAgentReadiness(profile)
                return (
                  <div className="comfy-model-panel__profile" key={profile.id}>
                    <b>{profile.name}</b>
                    <span>{profile.family.toUpperCase()} · {profile.assets.length}개 자산</span>
                    <small className={readiness.ready ? 'is-ready' : ''}>
                      {profile.agentEnabled ? readiness.detail : 'Agent 자동 선택 꺼짐'}
                    </small>
                  </div>
                )
              })}
          <div className="comfy-model-panel__title">ComfyUI 인식 체크포인트</div>
          {checkpoints.length === 0
            ? <div>인식된 체크포인트가 없습니다.</div>
            : checkpoints.map((name) => <div key={name}>{name}</div>)}
        </div>
      )}
      <div className="comfy-surface" ref={surfaceRef} aria-label="ComfyUI 작업 화면" />
    </div>
  )
}

export default ComfyView
