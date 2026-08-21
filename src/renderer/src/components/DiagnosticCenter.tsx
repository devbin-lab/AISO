import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { BackendInfo, HealthInfo } from '../../../shared/backend'
import type { AppSettings } from '../../../shared/settings'
import type { PingResult } from '../../../shared/ipc'
import type { UsageSummary } from '../../../shared/usage'
import { authHeaders } from '../lib/backend'
import {
  backendUrl,
  collectConnectionChecks,
  stateLabel,
  summarizeChecks,
  type Check
} from '../lib/diagnostics'
import { RefreshIcon } from './icons'
import SetupCard from './SetupCard'

interface Scenario {
  id: string
  title: string
  status: 'pass' | 'fail'
  detail: string
  durationMs: number
}

interface QaReport {
  executedAt: string
  summary: { passed: number; failed: number; total: number }
  scenarios: Scenario[]
}

interface Props {
  backend: BackendInfo
  health: HealthInfo | null
  settings: AppSettings
  onSaveSettings: (patch: Partial<AppSettings>) => Promise<boolean>
  active: boolean
}

type Conn = 'checking' | 'ok' | 'error'

const fmtTokens = (n: number): string =>
  n >= 1_000_000
    ? `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`
    : n >= 10_000
      ? `${(n / 1000).toFixed(0)}K`
      : n.toLocaleString('en-US')

function DiagnosticRow({ check }: { check: Check | undefined }): React.JSX.Element | null {
  if (!check) return null
  return <li className={`kv__diagnostic kv__diagnostic--${check.state}`}>
    <span>{check.label}</span>
    <div><b>{stateLabel(check.state)}</b><small>{check.detail}</small></div>
  </li>
}

export default function DiagnosticCenter({ backend, health, settings, onSaveSettings, active }: Props): React.JSX.Element {
  const [checks, setChecks] = useState<Check[]>([])
  const [qa, setQa] = useState<QaReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [ping, setPing] = useState<PingResult | null>(null)
  const [conn, setConn] = useState<Conn>('checking')
  const [usage, setUsage] = useState<UsageSummary | null>(null)
  // App은 Ollama health를 주기적으로 새 객체로 갱신한다. 그 값에 refresh
  // callback을 묶으면 설정 탭을 열어 둔 것만으로 네트워크 상태 점검이 반복된다.
  // 진입 시 한 번만 자동 점검하고, 그 이후에는 사용자가 명시적으로 새로고침한다.
  const didInitialRefresh = useRef(false)

  const refreshChecks = useCallback(async (): Promise<void> => {
    setBusy(true)
    setMessage('')
    setChecks(await collectConnectionChecks(backend, health, settings))
    setBusy(false)
  }, [backend, health, settings])

  const refreshRuntime = useCallback(async (): Promise<void> => {
    setConn('checking')
    try {
      if (!window.api?.ping) throw new Error('preload API 없음')
      setPing(await window.api.ping())
      setConn('ok')
    } catch {
      setConn('error')
    }
    try {
      const nextUsage = await window.api?.usage?.summary()
      setUsage(nextUsage ?? null)
    } catch {
      setUsage(null)
    }
  }, [])

  const refreshAll = useCallback(async (): Promise<void> => {
    await Promise.all([refreshChecks(), refreshRuntime()])
  }, [refreshChecks, refreshRuntime])

  useEffect(() => {
    if (!active) {
      didInitialRefresh.current = false
      return
    }
    if (didInitialRefresh.current) return
    didInitialRefresh.current = true
    void refreshAll()
  }, [active, refreshAll])

  const runQa = async (): Promise<void> => {
    const url = backendUrl(backend, '/qa/scenarios/run')
    if (!url) return
    setBusy(true)
    setMessage('')
    try {
      const response = await fetch(url, { method: 'POST', headers: authHeaders() })
      const result = await response.json() as QaReport & { detail?: string }
      if (!response.ok) throw new Error(result.detail || `QA 실행 실패 (${response.status})`)
      setQa(result)
      setMessage(result.summary.failed === 0 ? '시나리오 QA 평가팩을 모두 통과했습니다.' : `${result.summary.failed}개 시나리오를 확인해야 합니다.`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '시나리오 QA를 실행하지 못했습니다.')
    } finally {
      setBusy(false)
    }
  }

  const summary = useMemo(() => summarizeChecks(checks), [checks])

  const connText = conn === 'ok' ? 'IPC 정상' : conn === 'checking' ? '확인 중' : '연결 안 됨'
  const daily = usage?.daily ?? []
  const maxTokens = Math.max(1, ...daily.map((day) => day.tokens))
  const activeModel = settings.activeLlmProvider === 'nvidia' ? settings.nvidiaModel || '—' : settings.model
  const checksById = useMemo(() => new Map(checks.map((check) => [check.id, check])), [checks])

  return <section className="diagnostic-center">
    <div className="diagnostic-center__head">
      <div><div className="tool-panel__eyebrow">SYSTEM DIAGNOSTICS</div><h2>진단 센터</h2><p>실행 환경, 연결 상태, 시작 준비, 사용량과 회귀 검증 결과를 한곳에서 확인합니다. API 키나 문서 원문은 표시하지 않습니다.</p></div>
      <div className="diagnostic-center__head-actions"><span className="diagnostic-center__summary">{summary}</span><button type="button" className="btn btn--ghost2" disabled={busy} onClick={() => void refreshAll()}>{busy ? '점검 중…' : '상태 새로고침'}</button></div>
    </div>
    <SetupCard settings={settings} backend={backend} health={health} onSaveSettings={onSaveSettings} />
    <div className="grid diagnostic-runtime-grid">
        <section className="panel">
          <div className="panel__head"><span className="panel__title">시스템</span><button className="iconbtn" data-tip="실행 환경 새로고침" aria-label="실행 환경 새로고침" onClick={() => void refreshRuntime()}><RefreshIcon /></button></div>
          <div className={`state state--${conn}`}><span className="state__dot" />{connText}</div>
          <ul className="kv">
            <DiagnosticRow check={checksById.get('backend')} />
            <DiagnosticRow check={checksById.get('comfy')} />
            <DiagnosticRow check={checksById.get('discord')} />
            {ping && <><li><span>Electron</span><b className="mono">{ping.versions.electron}</b></li><li><span>Chromium</span><b className="mono">{ping.versions.chrome}</b></li><li><span>Node</span><b className="mono">{ping.versions.node}</b></li><li><span>V8</span><b className="mono">{ping.versions.v8}</b></li></>}
          </ul>
        </section>
        <section className="panel">
          <div className="panel__head"><span className="panel__title">선택한 AI 엔진</span></div>
          <ul className="kv">
            <li><span>공급자</span><b>{settings.activeLlmProvider === 'nvidia' ? 'NVIDIA' : 'Ollama'}</b></li>
            <li><span>모델</span><b className="mono">{activeModel}</b></li>
            <DiagnosticRow check={checksById.get('llm')} />
            {settings.activeLlmProvider === 'ollama' && <li><span>설치된 모델</span><b>{health ? `${health.models.length}개` : '—'}</b></li>}
            <li><span>추론 강도</span><b>{settings.reasoningEffort}</b></li>
          </ul>
        </section>
        <section className="panel panel--wide">
          <div className="panel__head"><span className="panel__title">토큰 사용량</span><button className="iconbtn" data-tip="사용량 새로고침" aria-label="사용량 새로고침" onClick={() => void refreshRuntime()}><RefreshIcon /></button></div>
          <div className="usage-tiles">
            <div className="usage-tile"><span className="usage-tile__num mono">{fmtTokens(usage?.today ?? 0)}</span><span className="usage-tile__label">오늘</span></div>
            <div className="usage-tile"><span className="usage-tile__num mono">{fmtTokens(usage?.week ?? 0)}</span><span className="usage-tile__label">최근 7일</span></div>
            <div className="usage-tile"><span className="usage-tile__num mono">{fmtTokens(usage?.month ?? 0)}</span><span className="usage-tile__label">최근 30일</span></div>
          </div>
          <div className="usage-chart" role="img" aria-label="최근 30일 일별 토큰 사용량">
            {daily.map((day, index) => {
              const percent = (day.tokens / maxTokens) * 100
              const height = day.tokens > 0 ? Math.max(6, percent) : 2
              const isToday = index === daily.length - 1
              return <span key={day.day} className={`usage-bar ${isToday ? 'usage-bar--today' : ''} ${day.tokens === 0 ? 'usage-bar--empty' : ''}`} style={{ height: `${height}%` }} data-tip={`${day.day.slice(5).replace('-', '/')} · ${day.tokens.toLocaleString('en-US')} 토큰`} />
            })}
          </div>
          <div className="usage-axis"><span>30일 전</span><span>오늘</span></div>
        </section>
    </div>
    <div className="diagnostic-qa">
      <div><div className="tool-panel__eyebrow">REGRESSION PACK</div><b>시나리오 기반 QA 평가팩</b><p>문서 추출, 원문 근거 일정, 저장·상태 변경, PPTX 위치 추출, 작업 폴더 경계를 토큰 소모 없이 검사합니다.</p></div>
      <button type="button" className="btn" disabled={busy || backend.state !== 'ready'} onClick={() => void runQa()}>QA 평가 실행</button>
    </div>
    {message && <div className="diagnostic-center__message" role="status">{message}</div>}
    {qa && <div className="diagnostic-qa__results">
      <div className="diagnostic-qa__results-head"><b>{qa.summary.passed}/{qa.summary.total} 통과</b><span>{new Date(qa.executedAt).toLocaleString('ko-KR')}</span></div>
      {qa.scenarios.map((scenario) => <div className={`diagnostic-scenario diagnostic-scenario--${scenario.status}`} key={scenario.id}><b>{scenario.status === 'pass' ? 'PASS' : 'FAIL'}</b><span>{scenario.title}</span><small>{scenario.detail} · {scenario.durationMs}ms</small></div>)}
    </div>}
  </section>
}
