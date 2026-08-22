import { lazy, Suspense, useEffect, useState } from 'react'
import type { AppSettings } from '../../shared/settings'
import { DEFAULT_SETTINGS } from '../../shared/settings'
import type { BackendInfo, HealthInfo } from '../../shared/backend'
import type { ConversationKind } from '../../shared/conversation'
import Titlebar from './components/Titlebar'
import Sidebar, { type ConversationRequest, type ViewKey } from './components/Sidebar'
import TooltipHost from './components/Tooltip'
import { ConfirmHost } from './components/ConfirmDialog'
import { applyTheme } from './lib/theme'
import { authHeaders } from './lib/backend'

// Heavy views (Markdown, ComfyUI controls, settings integrations) are split from
// the startup bundle. A visited view remains mounted so active conversations and
// unsaved settings retain their existing behavior.
const ChatView = lazy(() => import('./views/ChatView'))
const AgentView = lazy(() => import('./views/AgentView'))
const TodoView = lazy(() => import('./views/TodoView'))
const MyDbView = lazy(() => import('./views/MyDbView'))
const ComfyView = lazy(() => import('./views/ComfyView'))
const SettingsView = lazy(() => import('./views/SettingsView'))

function ViewFallback(): React.JSX.Element {
  return <div className="view-loading" role="status">화면을 준비하고 있습니다…</div>
}

/**
 * 내용이 같으면 **이전 객체를 그대로** 돌려준다.
 *
 * health 는 5초마다 폴링하는데, 매번 새 객체를 만들면 값이 하나도 안 바뀌어도
 * 참조가 달라진다. health 를 의존성에 둔 useCallback/useEffect 가 전부 다시 돌아
 * 홈 대시보드가 5초마다 통째로 재로드됐다(실측: 60초에 12회, 내용 변화 0회).
 * 진단 센터는 같은 문제를 ref 가드로 우회했지만, 여기서 참조를 안정시키면
 * health 를 쓰는 모든 화면이 함께 이득을 본다.
 *
 * models 는 순서까지 의미가 있으므로 순서를 포함해 비교한다.
 *
 * export 하는 이유는 테스트뿐이다 — 이 판정이 무너지면 증상이 조용히 돌아오는데,
 * App 을 통째로 렌더하는 테스트로는 폴링 참조 안정성을 확인하기 어렵다.
 */
export function keepIfSame(prev: HealthInfo | null, next: HealthInfo): HealthInfo {
  if (
    prev !== null &&
    prev.ollama === next.ollama &&
    prev.detail === next.detail &&
    prev.models.length === next.models.length &&
    prev.models.every((m, i) => m === next.models[i])
  ) {
    return prev
  }
  return next
}

function App(): React.JSX.Element {
  const [view, setView] = useState<ViewKey>('chat')
  const [visitedViews, setVisitedViews] = useState<Set<ViewKey>>(() => new Set(['chat']))
  const [settings, setSettings] = useState<AppSettings>(DEFAULT_SETTINGS)
  const [backend, setBackend] = useState<BackendInfo>({ state: 'starting', port: null })
  const [health, setHealth] = useState<HealthInfo | null>(null)
  // 대화 목록 패널 접힘 상태 — 뷰별로 별도 유지, 토글 버튼은 타이틀바에 있다
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [conversationRequest, setConversationRequest] = useState<ConversationRequest | null>(null)

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
    if (settings.activeLlmProvider !== 'ollama') {
      setHealth(null)
      return
    }
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
        if (alive) setHealth((prev) => keepIfSame(prev, { ollama: j.ollama === true, models: j.models ?? [], detail: j.detail }))
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
  }, [backend.state, backend.port, settings.activeLlmProvider, settings.ollamaHost])

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
  const wrap = (k: ViewKey): string => `viewwrap${k === 'todo' ? ' viewwrap--todo' : ''}${k === 'graph' ? ' viewwrap--graph' : ''}${view === k ? '' : ' viewwrap--hidden'}`
  const navigate = (next: ViewKey): void => {
    setVisitedViews((current) => current.has(next) ? current : new Set(current).add(next))
    setView(next)
  }

  const convToggle = {
    collapsed: sidebarCollapsed,
    onToggle: () => setSidebarCollapsed((value) => !value)
  }

  const requestConversation = (kind: ConversationKind, id: string | null): void => {
    navigate(kind)
    setSidebarCollapsed(false)
    setConversationRequest({ kind, id, nonce: Date.now() })
  }

  return (
    <div className="frame">
      <Titlebar backend={backend} health={health} provider={settings.activeLlmProvider} convToggle={convToggle} />
      <div className="body">
        <Sidebar
          view={view}
          onNavigate={navigate}
          onNewConversation={(kind) => requestConversation(kind, null)}
          onOpenConversation={requestConversation}
          activeConversation={conversationRequest}
          collapsed={sidebarCollapsed}
        />
        <main className="content">
          {/* 채팅은 시작 화면이라 늘 방문 상태다 — visitedViews 검사가 참일 수밖에 없어 두지 않는다. */}
          <div className={wrap('chat')}><Suspense fallback={<ViewFallback />}><ChatView onNavigate={navigate} active={view === 'chat'} settings={settings} backend={backend} health={health} onSaveSettings={saveSettings} conversationRequest={conversationRequest} onConversationActive={(id) => setConversationRequest({ kind: 'chat', id, nonce: Date.now() })} /></Suspense></div>
          {visitedViews.has('agent') && <div className={wrap('agent')}><Suspense fallback={<ViewFallback />}><AgentView onNavigate={navigate} active={view === 'agent'} settings={settings} backend={backend} health={health} onPickWorkspace={pickWorkspace} onSaveSettings={saveSettings} conversationRequest={conversationRequest} onConversationActive={(id) => setConversationRequest({ kind: 'agent', id, nonce: Date.now() })} /></Suspense></div>}
          {visitedViews.has('todo') && <div className={wrap('todo')}><Suspense fallback={<ViewFallback />}><TodoView active={view === 'todo'} backend={backend} /></Suspense></div>}
          {visitedViews.has('graph') && <div className={wrap('graph')}><Suspense fallback={<ViewFallback />}><MyDbView active={view === 'graph'} /></Suspense></div>}
          {visitedViews.has('comfy') && <div className={wrap('comfy')}><Suspense fallback={<ViewFallback />}><ComfyView settings={settings} backend={backend} active={view === 'comfy'} onSaveSettings={saveSettings} /></Suspense></div>}
          {visitedViews.has('settings') && <div className={wrap('settings')}><Suspense fallback={<ViewFallback />}><SettingsView settings={settings} backend={backend} health={health} onSave={saveSettings} onExternalSettingsChange={setSettings} active={view === 'settings'} /></Suspense></div>}
        </main>
      </div>
      <TooltipHost />
      <ConfirmHost />
    </div>
  )
}

export default App
