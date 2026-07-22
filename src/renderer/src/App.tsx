import { lazy, Suspense, useEffect, useState } from 'react'
import type { AppSettings } from '../../shared/settings'
import { DEFAULT_SETTINGS } from '../../shared/settings'
import type { BackendInfo, HealthInfo } from '../../shared/backend'
import Titlebar from './components/Titlebar'
import Sidebar, { type ViewKey } from './components/Sidebar'
import TooltipHost from './components/Tooltip'
import { ConfirmHost } from './components/ConfirmDialog'
import { applyTheme } from './lib/theme'
import { authHeaders } from './lib/backend'

// Heavy views (Markdown, ComfyUI controls, settings integrations) are split from
// the startup bundle. A visited view remains mounted so active conversations and
// unsaved settings retain their existing behavior.
const HomeView = lazy(() => import('./views/HomeView'))
const ChatView = lazy(() => import('./views/ChatView'))
const AgentView = lazy(() => import('./views/AgentView'))
const ComfyView = lazy(() => import('./views/ComfyView'))
const SettingsView = lazy(() => import('./views/SettingsView'))

function ViewFallback(): React.JSX.Element {
  return <div className="view-loading" role="status">화면을 준비하고 있습니다…</div>
}

function App(): React.JSX.Element {
  const [view, setView] = useState<ViewKey>('home')
  const [visitedViews, setVisitedViews] = useState<Set<ViewKey>>(() => new Set(['home']))
  const [settings, setSettings] = useState<AppSettings>(DEFAULT_SETTINGS)
  const [backend, setBackend] = useState<BackendInfo>({ state: 'starting', port: null })
  const [health, setHealth] = useState<HealthInfo | null>(null)
  // 대화 목록 패널 접힘 상태 — 뷰별로 별도 유지, 토글 버튼은 타이틀바에 있다
  const [chatConvCollapsed, setChatConvCollapsed] = useState(false)
  const [agentConvCollapsed, setAgentConvCollapsed] = useState(false)

  // 메인 프로세스에서 저장된 설정 불러오기
  useEffect(() => {
    let cancelled = false
    const load = async (): Promise<void> => {
      try {
        if (window.api?.settings) {
          const s = await window.api.settings.get()
          if (!cancelled) setSettings(s)
        }
      } catch (err) {
        console.error('설정 불러오기 실패:', err)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [])

  // 백엔드(FastAPI 사이드카) 상태 구독
  useEffect(() => {
    if (!window.api?.backend) {
      setBackend({ state: 'error', port: null, detail: 'Electron 환경 아님 (미리보기)' })
      return
    }
    window.api.backend.info().then(setBackend).catch(() => {})
    const unsubscribe = window.api.backend.onStatus(setBackend)
    return unsubscribe
  }, [])

  // Ollama 헬스 폴링 (백엔드 준비 후 5초 간격)
  useEffect(() => {
    if (backend.state !== 'ready' || backend.port == null) {
      setHealth(null)
      return
    }
    let alive = true
    const poll = async (): Promise<void> => {
      try {
        const r = await fetch(
          `http://127.0.0.1:${backend.port}/health?host=${encodeURIComponent(settings.ollamaHost)}`,
          { headers: authHeaders() }
        )
        const j = await r.json()
        if (alive) setHealth({ ollama: j.ollama === true, models: j.models ?? [], detail: j.detail })
      } catch {
        if (alive) setHealth(null)
      }
    }
    void poll()
    const t = window.setInterval(poll, 5000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [backend.state, backend.port, settings.ollamaHost])

  // 테마 적용 (html data-theme + 네이티브 타이틀바 색)
  useEffect(() => {
    applyTheme(settings.theme)
  }, [settings.theme])

  const pickWorkspace = async (): Promise<void> => {
    try {
      const path = await window.api?.pickWorkspace?.()
      if (path) await saveSettings({ workspace: path })
    } catch (err) {
      console.error('작업 폴더 선택 실패:', err)
    }
  }

  /** 실제 저장에 성공했을 때만 Renderer 상태를 갱신한다.
   * false는 호출자가 저장 완료 UI나 후속 작업을 진행하면 안 된다는 뜻이다. */
  const saveSettings = async (patch: Partial<AppSettings>): Promise<boolean> => {
    try {
      let next: AppSettings = { ...settings, ...patch }
      if (window.api?.settings) {
        next = await window.api.settings.set(patch)
      }
      setSettings(next)
      return true
    } catch (err) {
      console.error('설정 저장 실패:', err)
      return false
    }
  }

  // 뷰는 숨김 처리로 유지 → 채팅 대화가 뷰 전환에 사라지지 않는다
  const wrap = (k: ViewKey): string => `viewwrap${view === k ? '' : ' viewwrap--hidden'}`
  const navigate = (next: ViewKey): void => {
    setVisitedViews((current) => current.has(next) ? current : new Set(current).add(next))
    setView(next)
  }

  const convToggle =
    view === 'chat'
      ? { collapsed: chatConvCollapsed, onToggle: () => setChatConvCollapsed((v) => !v) }
      : view === 'agent'
        ? { collapsed: agentConvCollapsed, onToggle: () => setAgentConvCollapsed((v) => !v) }
        : null

  return (
    <div className="frame">
      <Titlebar backend={backend} health={health} convToggle={convToggle} />
      <div className="body">
        <Sidebar view={view} onNavigate={navigate} />
        <main className="content">
          {visitedViews.has('home') && <div className={wrap('home')}><Suspense fallback={<ViewFallback />}><HomeView settings={settings} backend={backend} health={health} onSaveSettings={saveSettings} /></Suspense></div>}
          {visitedViews.has('chat') && <div className={wrap('chat')}><Suspense fallback={<ViewFallback />}><ChatView settings={settings} backend={backend} health={health} onSaveSettings={saveSettings} convCollapsed={chatConvCollapsed} /></Suspense></div>}
          {visitedViews.has('agent') && <div className={wrap('agent')}><Suspense fallback={<ViewFallback />}><AgentView settings={settings} backend={backend} health={health} onPickWorkspace={pickWorkspace} onSaveSettings={saveSettings} convCollapsed={agentConvCollapsed} /></Suspense></div>}
          {visitedViews.has('comfy') && <div className={wrap('comfy')}><Suspense fallback={<ViewFallback />}><ComfyView settings={settings} backend={backend} active={view === 'comfy'} onSaveSettings={saveSettings} /></Suspense></div>}
          {visitedViews.has('settings') && <div className={wrap('settings')}><Suspense fallback={<ViewFallback />}><SettingsView settings={settings} backend={backend} health={health} onSave={saveSettings} active={view === 'settings'} /></Suspense></div>}
        </main>
      </div>
      <TooltipHost />
      <ConfirmHost />
    </div>
  )
}

export default App
