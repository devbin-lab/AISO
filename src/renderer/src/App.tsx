import { useEffect, useState } from 'react'
import type { AppSettings } from '../../shared/settings'
import { DEFAULT_SETTINGS } from '../../shared/settings'
import type { BackendInfo, HealthInfo } from '../../shared/backend'
import Titlebar from './components/Titlebar'
import Sidebar, { type ViewKey } from './components/Sidebar'
import HomeView from './views/HomeView'
import ChatView from './views/ChatView'
import AgentView from './views/AgentView'
import SettingsView from './views/SettingsView'
import { applyTheme } from './lib/theme'

function App(): React.JSX.Element {
  const [view, setView] = useState<ViewKey>('home')
  const [settings, setSettings] = useState<AppSettings>(DEFAULT_SETTINGS)
  const [backend, setBackend] = useState<BackendInfo>({ state: 'starting', port: null })
  const [health, setHealth] = useState<HealthInfo | null>(null)

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
          `http://127.0.0.1:${backend.port}/health?host=${encodeURIComponent(settings.ollamaHost)}`
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

  const saveSettings = async (patch: Partial<AppSettings>): Promise<void> => {
    let next: AppSettings = { ...settings, ...patch }
    try {
      if (window.api?.settings) {
        next = await window.api.settings.set(patch)
      }
    } catch (err) {
      console.error('설정 저장 실패:', err)
    }
    setSettings(next)
  }

  // 뷰는 숨김 처리로 유지 → 채팅 대화가 뷰 전환에 사라지지 않는다
  const wrap = (k: ViewKey): string => `viewwrap${view === k ? '' : ' viewwrap--hidden'}`

  return (
    <div className="frame">
      <Titlebar backend={backend} health={health} />
      <div className="body">
        <Sidebar view={view} onNavigate={setView} />
        <main className="content">
          <div className={wrap('home')}>
            <HomeView settings={settings} backend={backend} health={health} />
          </div>
          <div className={wrap('chat')}>
            <ChatView
              settings={settings}
              backend={backend}
              health={health}
              onSaveSettings={saveSettings}
            />
          </div>
          <div className={wrap('agent')}>
            <AgentView
              settings={settings}
              backend={backend}
              health={health}
              onPickWorkspace={pickWorkspace}
              onSaveSettings={saveSettings}
            />
          </div>
          <div className={wrap('settings')}>
            <SettingsView settings={settings} health={health} onSave={saveSettings} />
          </div>
        </main>
      </div>
    </div>
  )
}

export default App
