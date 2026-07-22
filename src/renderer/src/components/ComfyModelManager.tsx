import { useEffect, useState } from 'react'
import type {
  ComfyGenerationDefaults,
  ComfyModelFamily,
  ComfyModelImportProgress,
  ComfyModelProfile,
  ComfyWorkflowBindingTarget
} from '../../../shared/comfy-model'
import {
  COMFY_ASSET_KIND_LABELS,
  COMFY_ASSET_SLOT_LABELS,
  getComfyAgentReadiness,
  getComfyGenerationDefaults,
  getComfyRequiredSlots
} from '../../../shared/comfy-model'
import { confirmDialog } from './ConfirmDialog'

interface Props {
  /** 저장되어 실제 가져오기와 자동 실행에 사용되는 Portable 설치 경로 */
  installPath: string
  /** 설정 폼에서만 바뀌고 아직 저장되지 않은 Portable 설치 경로가 있는지 */
  hasUnsavedInstallPathChange?: boolean
  onChanged?: (profiles: ComfyModelProfile[]) => void
}

type EditorMode = 'new' | 'asset' | 'edit'

interface EditorState {
  mode: EditorMode
  profileId?: string
  name: string
  family: ComfyModelFamily
  tags: string
  agentEnabled: boolean
  priority: number
  defaults: ComfyGenerationDefaults
}

const SAMPLER_OPTIONS = ['euler', 'euler_ancestral', 'dpmpp_2m', 'dpmpp_2m_sde'] as const
const SCHEDULER_OPTIONS = ['normal', 'karras', 'simple', 'beta'] as const
const MAX_TAG_INPUT_LENGTH = 1_200
const MAX_TAGS = 20
const MAX_TAG_LENGTH = 48

const WORKFLOW_INPUT_LABELS: Record<ComfyWorkflowBindingTarget, string> = {
  positivePrompt: '프롬프트',
  negativePrompt: '네거티브 프롬프트',
  seed: '시드',
  width: '너비',
  height: '높이',
  steps: 'Steps',
  cfg: 'CFG',
  sampler: 'Sampler',
  scheduler: 'Scheduler',
  filenamePrefix: '출력 경로'
}

function isAgentWorkflowFamily(family: ComfyModelFamily): boolean {
  return family === 'sd15' || family === 'sdxl' || family === 'flux1' || family === 'flux2'
}

function emptyEditor(): EditorState {
  return {
    mode: 'new',
    name: '',
    family: 'custom',
    tags: '',
    // 구성 파일을 완성한 뒤에만 명시적으로 Agent에 사용하도록 한다.
    agentEnabled: false,
    priority: 0,
    defaults: getComfyGenerationDefaults('custom')
  }
}

function editorFromProfile(profile: ComfyModelProfile, mode: EditorMode): EditorState {
  return {
    mode,
    profileId: profile.id,
    name: profile.name,
    family: profile.family,
    tags: profile.tags.join(', '),
    agentEnabled: profile.agentEnabled,
    priority: profile.priority,
    defaults: { ...profile.defaults }
  }
}

/** Mirrors the main-process contract so the UI cannot create an impractically large tag patch. */
function normalizedTags(value: string): string[] {
  return [...new Set(
    value
      .slice(0, MAX_TAG_INPUT_LENGTH)
      .split(',')
      .map((tag) => tag.trim().slice(0, MAX_TAG_LENGTH))
      .filter(Boolean)
  )].slice(0, MAX_TAGS)
}

function bytesLabel(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024)).toLocaleString()} KB`
}

function progressLabel(progress: ComfyModelImportProgress): string {
  const phase = {
    hashing: '원본 SHA-256 계산',
    copying: 'ComfyUI로 복사',
    verifying: '복사 결과 검증',
    complete: '완료'
  }[progress.phase]
  const percent = progress.totalBytes > 0
    ? Math.min(100, Math.round((progress.completedBytes / progress.totalBytes) * 100))
    : 0
  return `${phase} · ${progress.fileName} · ${percent}%`
}

function ComfyModelManager({
  installPath,
  hasUnsavedInstallPathChange = false,
  onChanged
}: Props): React.JSX.Element {
  const [profiles, setProfiles] = useState<ComfyModelProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [progress, setProgress] = useState<ComfyModelImportProgress | null>(null)
  const [message, setMessage] = useState('')
  const [activeImportOperationId, setActiveImportOperationId] = useState<string | null>(null)

  const load = async (): Promise<ComfyModelProfile[] | null> => {
    setLoading(true)
    try {
      const registry = await window.api.comfy.models.list()
      setProfiles(registry.profiles)
      onChanged?.(registry.profiles)
      return registry.profiles
    } catch (error) {
      setMessage(`모델 목록을 불러오지 못했습니다: ${String(error)}`)
      return null
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [installPath])

  useEffect(() => window.api.comfy.models.onImportProgress(setProgress), [])

  useEffect(() => {
    if (!editor) return
    const closeOnEscape = (event: KeyboardEvent): void => {
      if (event.key === 'Escape' && !busy) setEditor(null)
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [editor, busy])

  const setEditorField = <K extends keyof EditorState>(key: K, value: EditorState[K]): void => {
    setEditor((current) => current ? { ...current, [key]: value } : current)
  }

  const setDefault = <K extends keyof ComfyGenerationDefaults>(
    key: K,
    value: ComfyGenerationDefaults[K]
  ): void => {
    setEditor((current) => current
      ? { ...current, defaults: { ...current.defaults, [key]: value } }
      : current)
  }

  /**
   * Asset/workflow imports can change a profile's detected family and defaults.
   * Keep an open editor accurate without discarding the name/tags a user has
   * not saved yet.
   */
  const syncOpenEditor = (profile: ComfyModelProfile): void => {
    setEditor((current) => {
      if (!current || current.profileId !== profile.id) return current
      return {
        ...current,
        family: profile.family,
        agentEnabled: profile.agentEnabled,
        defaults: { ...profile.defaults }
      }
    })
  }

  const startNew = (): void => {
    setMessage('')
    setProgress(null)
    setEditor(emptyEditor())
  }

  const addAsset = (profile: ComfyModelProfile): void => {
    void importAssets(editorFromProfile(profile, 'asset'))
  }

  const editProfile = (profile: ComfyModelProfile): void => {
    setMessage('')
    setProgress(null)
    setEditor(editorFromProfile(profile, 'edit'))
  }

  const importAssets = async (draft: EditorState | null = editor): Promise<void> => {
    if (!draft || draft.mode === 'edit' || busy) return
    if (!installPath.trim()) {
      setMessage('먼저 ComfyUI 설치 폴더를 선택하고 설정을 저장해 주세요. 외부 서버 주소만으로는 모델 파일을 연결할 수 없습니다.')
      return
    }
    if (!draft.name.trim()) {
      setMessage('모델 이름을 입력해 주세요.')
      return
    }
    setBusy(true)
    setMessage('파일을 선택해 주세요. 원본 파일은 유지되고 ComfyUI 모델 폴더에 안전하게 복사됩니다.')
    setProgress(null)
    const operationId = typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`
    setActiveImportOperationId(operationId)
    try {
      const result = await window.api.comfy.models.importAssets({
        operationId,
        ...(draft.profileId ? { profileId: draft.profileId } : {}),
        name: draft.name.trim(),
        capabilities: ['txt2img'],
        tags: normalizedTags(draft.tags)
      })
      if (result.canceled) {
        setMessage('파일 연결을 취소했습니다.')
        return
      }
      const reused = result.reused.length
      const direct = [...result.imported, ...result.reused].filter((asset) => asset.kind === 'custom').length
      const readiness = result.profile ? getComfyAgentReadiness(result.profile) : null
      setMessage(
        `${draft.mode === 'new' ? '새 모델을 연결했습니다.' : '구성 파일을 연결했습니다.'} ` +
        `새로 복사 ${result.imported.length}개${reused ? ` · 기존 파일 재사용 ${reused}개` : ''}` +
        `${direct ? ` · 직접 연결 ${direct}개(Agent 자동 선택 미지원)` : ''}` +
        `${readiness ? ` · ${readiness.detail}` : ''}`
      )
      if (draft.mode === 'new' || editor?.profileId !== draft.profileId) {
        setEditor(null)
      } else if (result.profile) {
        syncOpenEditor(result.profile)
      }
      const latest = await load()
      const refreshed = latest?.find((profile) => profile.id === result.profile?.id)
      if (refreshed) syncOpenEditor(refreshed)
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      // 개발 중 Renderer만 최신인데 Electron main이 이전 코드인 경우, 이름과 무관하게
      // 옛 family 필수 검증이 먼저 실패한다. 사용자에게 정확한 복구 동작을 안내한다.
      if (detail.includes('지원하지 않는 모델 계열입니다.')) {
        setMessage('입력한 모델 이름과 무관한 개발 프로세스 불일치입니다. 터미널에서 Ctrl+C 후 npm run dev로 Aiso를 완전히 다시 시작해 주세요.')
      } else {
        setMessage(`파일 연결 실패: ${detail}`)
      }
    } finally {
      setBusy(false)
      setActiveImportOperationId((current) => current === operationId ? null : current)
    }
  }

  const cancelImport = async (): Promise<void> => {
    if (!activeImportOperationId) return
    try {
      const accepted = await window.api.comfy.models.cancelImport(activeImportOperationId)
      setMessage(accepted
        ? '파일 연결을 취소하는 중입니다. 현재 파일 검사가 끝나면 안전하게 정리합니다.'
        : '취소할 파일 연결 작업을 찾지 못했습니다.')
    } catch (error) {
      setMessage(`파일 연결 취소 요청 실패: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  const importWorkflow = async (profile: ComfyModelProfile): Promise<void> => {
    if (busy) return
    setBusy(true)
    setMessage('ComfyUI에서 API 형식으로 내보낸 워크플로 JSON을 선택해 주세요.')
    try {
      const result = await window.api.comfy.models.importWorkflow(profile.id)
      if (result.canceled) {
        setMessage('워크플로 연결을 취소했습니다.')
        return
      }
      setMessage('사용자 워크플로를 연결했습니다. 내용을 확인한 뒤 Agent 자동 선택을 켜 주세요.')
      if (result.profile) syncOpenEditor(result.profile)
      const latest = await load()
      const refreshed = latest?.find((item) => item.id === result.profile?.id)
      if (refreshed) syncOpenEditor(refreshed)
    } catch (error) {
      setMessage(`워크플로 연결 실패: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setBusy(false)
    }
  }

  const removeWorkflow = async (profile: ComfyModelProfile): Promise<void> => {
    if (!profile.workflowTemplate || busy) return
    const confirmed = await confirmDialog({
      title: '사용자 워크플로 연결 해제',
      message:
        `'${profile.workflowTemplate.sourceFileName}' 워크플로 연결을 해제합니다.\n` +
        '모델 파일과 원본 JSON 파일은 삭제하지 않습니다.',
      confirmLabel: '연결 해제',
      danger: true
    })
    if (!confirmed) return
    setBusy(true)
    try {
      const updated = await window.api.comfy.models.removeWorkflow(profile.id)
      setMessage('사용자 워크플로 연결을 해제했습니다.')
      syncOpenEditor(updated)
      const latest = await load()
      const refreshed = latest?.find((item) => item.id === updated.id)
      if (refreshed) syncOpenEditor(refreshed)
    } catch (error) {
      setMessage(`워크플로 연결 해제 실패: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setBusy(false)
    }
  }

  const saveEdit = async (): Promise<void> => {
    if (!editor?.profileId || editor.mode !== 'edit' || busy) return
    const current = profiles.find((profile) => profile.id === editor.profileId)
    if (editor.agentEnabled && current && !getComfyAgentReadiness(current).ready) {
      setMessage('필수 구성 파일을 모두 연결한 뒤 Agent 자동 선택을 켤 수 있습니다.')
      return
    }
    setBusy(true)
    setMessage(editor.agentEnabled && !current?.agentEnabled
      ? 'Agent 자동 선택을 켜기 전에 현재 ComfyUI 설치 폴더의 연결 파일을 SHA-256으로 확인하는 중입니다…'
      : '')
    try {
      await window.api.comfy.models.update(editor.profileId, {
        name: editor.name.trim(),
        capabilities: ['txt2img'],
        tags: normalizedTags(editor.tags),
        agentEnabled: editor.agentEnabled,
        priority: editor.priority,
        defaults: editor.defaults
      })
      setEditor(null)
      setMessage('모델 설정을 저장했습니다.')
      await load()
    } catch (error) {
      setMessage(`모델 설정 저장 실패: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setBusy(false)
    }
  }

  const toggleAgent = async (profile: ComfyModelProfile): Promise<void> => {
    if (busy) return
    const readiness = getComfyAgentReadiness(profile)
    if (!profile.agentEnabled && !readiness.ready) {
      setMessage(readiness.detail)
      return
    }
    setBusy(true)
    setMessage(profile.agentEnabled
      ? ''
      : 'Agent 자동 선택을 켜기 전에 현재 ComfyUI 설치 폴더의 연결 파일을 SHA-256으로 확인하는 중입니다…')
    try {
      const updated = await window.api.comfy.models.update(profile.id, { agentEnabled: !profile.agentEnabled })
      setMessage(profile.agentEnabled ? 'Agent 자동 선택을 껐습니다.' : 'Agent 자동 선택을 켰습니다.')
      syncOpenEditor(updated)
      const latest = await load()
      const refreshed = latest?.find((item) => item.id === updated.id)
      if (refreshed) syncOpenEditor(refreshed)
    } catch (error) {
      setMessage(`Agent 사용 설정 실패: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setBusy(false)
    }
  }

  const unregister = async (profile: ComfyModelProfile): Promise<void> => {
    const confirmed = await confirmDialog({
      title: '모델 등록 해제',
      message:
        `'${profile.name}' 모델 연결을 Aiso 목록에서 제거합니다.\n` +
        'ComfyUI에 복사된 실제 모델 파일은 삭제하지 않습니다.',
      confirmLabel: '등록 해제',
      danger: true
    })
    if (!confirmed) return
    try {
      await window.api.comfy.models.unregister(profile.id)
      setMessage('모델 연결을 해제했습니다. ComfyUI 모델 파일은 그대로 유지됩니다.')
      if (editor?.profileId === profile.id) setEditor(null)
      await load()
    } catch (error) {
      setMessage(`등록 해제 실패: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  const editorProfile = editor?.profileId
    ? profiles.find((profile) => profile.id === editor.profileId)
    : undefined
  const editorReadiness = editorProfile ? getComfyAgentReadiness(editorProfile) : null
  const editorSupportsAgent = Boolean(
    editor && (isAgentWorkflowFamily(editor.family) || editorProfile?.workflowTemplate)
  )
  const hasWorkflowInput = (target: ComfyWorkflowBindingTarget): boolean => {
    const template = editorProfile?.workflowTemplate
    return !template || template.bindings[target].length > 0
  }
  const templateInputLabels = editorProfile?.workflowTemplate
    ? (Object.keys(editorProfile.workflowTemplate.bindings) as ComfyWorkflowBindingTarget[])
      .filter((target) => editorProfile.workflowTemplate?.bindings[target].length)
      .map((target) => WORKFLOW_INPUT_LABELS[target])
    : []
  const canConnectModel = Boolean(installPath.trim()) && !hasUnsavedInstallPathChange

  return (
    <div className="comfy-library">
      <div className="comfy-library__head">
        <div>
          <div className="row__label">사용할 모델</div>
          <div className="row__hint">
            ComfyUI에 설치한 모델을 Aiso와 연결합니다. 자동 인식되지 않는 모델도 ComfyUI API 워크플로를 연결하면 Agent가 해당 고정 그래프로 사용할 수 있습니다.
          </div>
        </div>
        <button
          className="btn btn--sm"
          type="button"
          disabled={busy || !canConnectModel}
          title={!canConnectModel ? 'ComfyUI 설치 폴더를 저장한 뒤 모델을 연결할 수 있습니다.' : undefined}
          onClick={startNew}
        >
          새 모델 연결
        </button>
      </div>

      {hasUnsavedInstallPathChange && (
        <div className="comfy-library__notice">
          ComfyUI 연결 설정이 바뀌었습니다. 화면 오른쪽 아래의 <b>저장</b>을 누른 뒤 모델을 연결하세요. 저장 전에는 기존 설치 경로에 파일이 복사될 수 있어 연결을 잠시 막았습니다.
        </div>
      )}
      {!hasUnsavedInstallPathChange && !installPath.trim() && (
        <div className="comfy-library__notice">
          현재는 외부 ComfyUI 서버 화면만 연결되어 있습니다. 모델 파일 연결과 자동 실행은 Windows Portable 설치 폴더를 선택하고 저장한 뒤 사용할 수 있습니다.
        </div>
      )}
      {message && !editor && <div className="comfy-library__message" aria-live="polite">{message}</div>}
      {busy && progress && !editor && (
        <div className="comfy-import-progress" aria-live="polite">
          <div>{progressLabel(progress)}</div>
          <progress max={Math.max(1, progress.totalBytes)} value={progress.completedBytes} />
          {activeImportOperationId && (
            <button className="btn btn--ghost2 btn--sm" type="button" onClick={() => void cancelImport()}>
              가져오기 취소
            </button>
          )}
        </div>
      )}

      {editor?.mode === 'new' && (
        <section className="comfy-model-editor comfy-model-editor--new" aria-labelledby="comfy-model-editor-title">
          <div className="comfy-model-editor__title" id="comfy-model-editor-title">
            <span>
              새 모델 연결
            </span>
            <button
              className="comfy-model-editor__close"
              type="button"
              aria-label="편집 창 닫기"
              disabled={busy}
              onClick={() => setEditor(null)}
            >
              ×
            </button>
          </div>
          {message && <div className="comfy-model-editor__message" aria-live="polite">{message}</div>}
          {busy && progress && (
            <div className="comfy-import-progress" aria-live="polite">
              <div>{progressLabel(progress)}</div>
              <progress max={Math.max(1, progress.totalBytes)} value={progress.completedBytes} />
              {activeImportOperationId && (
                <button className="btn btn--ghost2 btn--sm" type="button" onClick={() => void cancelImport()}>
                  가져오기 취소
                </button>
              )}
            </div>
          )}
          <p className="comfy-model-editor__intro">
            모델 구조나 파일 역할은 선택하지 않습니다. Aiso가 확인 가능한 SafeTensors는 알맞은 ComfyUI 폴더에 자동 연결합니다. 역할을 확인할 수 없는 유효 파일은 모델 배포 문서에 적힌 ComfyUI/models 하위 폴더를 선택해 직접 연결할 수 있으며, 이 경우 Agent 자동 선택에는 사용하지 않습니다.
          </p>
          <div className="comfy-model-editor__grid">
            <label>
              <span>모델 이름</span>
              <input
                className="input"
                value={editor.name}
                maxLength={120}
                placeholder="예: 모델 파일 이름"
                onChange={(event) => setEditorField('name', event.target.value)}
              />
            </label>
            <label className="comfy-model-editor__wide">
              <span>이미지 특성 태그 (선택)</span>
              <input
                className="input"
                value={editor.tags}
                maxLength={MAX_TAG_INPUT_LENGTH}
                placeholder="예: anime, character, illustration"
                onChange={(event) => setEditorField('tags', event.target.value)}
              />
            </label>
          </div>
          <div className="comfy-model-editor__actions">
            <button className="btn btn--ghost2 btn--sm" type="button" disabled={busy} onClick={() => setEditor(null)}>
              취소
            </button>
            <button
              className="btn btn--sm"
              type="button"
              disabled={busy || !editor.name.trim()}
              onClick={() => void importAssets()}
            >
              {busy ? '처리 중…' : '파일 선택 및 연결'}
            </button>
          </div>
        </section>
      )}

      <div className="comfy-model-list">
        {loading ? (
          <div className="comfy-model-empty">연결한 모델을 불러오는 중…</div>
        ) : profiles.length === 0 ? (
          <div className="comfy-model-empty">아직 Aiso에 연결한 모델이 없습니다.</div>
        ) : profiles.map((profile) => {
          const readiness = getComfyAgentReadiness(profile)
          const requiredSlots = profile.workflowTemplate ? [] : getComfyRequiredSlots(profile.family)
          const connectedSlots = new Set(profile.assets.map((asset) => asset.slot).filter(Boolean))
          const compatibleRequiredConnected = requiredSlots.filter((slot) => (
            connectedSlots.has(slot) && !readiness.incompatibleSlots.includes(slot)
          )).length
          const canEnableAgent = readiness.ready
          return (
            <article
              className={`comfy-model-card ${editor?.mode === 'edit' && editor.profileId === profile.id ? 'comfy-model-card--editing' : ''}`}
              key={profile.id}
            >
              <div className="comfy-model-card__head">
                <div className="comfy-model-card__identity">
                  <strong>{profile.name}</strong>
                  {profile.workflowTemplate && <span>사용자 워크플로</span>}
                </div>
                <label
                  className="switch"
                  title={!profile.agentEnabled && !canEnableAgent ? readiness.detail : undefined}
                >
                  <input
                    type="checkbox"
                    checked={profile.agentEnabled}
                    disabled={busy || (!profile.agentEnabled && !canEnableAgent)}
                    aria-describedby={`comfy-model-readiness-${profile.id}`}
                    onChange={() => void toggleAgent(profile)}
                  />
                  <span className="switch__track" />
                  Agent 자동 선택
                </label>
              </div>
              {profile.tags.length > 0 && (
                <div className="comfy-model-tags">
                  {profile.tags.map((tag) => <span key={tag}>{tag}</span>)}
                </div>
              )}
              <div
                className={`comfy-model-readiness ${readiness.ready ? 'comfy-model-readiness--ready' : ''}`}
                id={`comfy-model-readiness-${profile.id}`}
              >
                {requiredSlots.length > 0 && `호환 필수 구성 파일 ${compatibleRequiredConnected}/${requiredSlots.length} 연결됨 · `}
                {readiness.detail}
                {readiness.ready && (profile.agentEnabled ? ' · Agent 자동 선택 사용 중' : ' · Agent 자동 선택은 꺼져 있습니다.')}
              </div>
              {readiness.notices.map((notice) => (
                <div className="comfy-model-readiness" key={notice}>{notice}</div>
              ))}
              {profile.assets.length > 0 && (
                <div className="comfy-model-assets">
                  {profile.assets.map((asset) => (
                    <div key={asset.id} title={`SHA-256 ${asset.sha256}\n${asset.relativePath}`}>
                      <span>{asset.slot ? COMFY_ASSET_SLOT_LABELS[asset.slot] : COMFY_ASSET_KIND_LABELS[asset.kind]}</span>
                      <b>{asset.comfyName}</b>
                      <small>{bytesLabel(asset.size)} · {asset.sha256.slice(0, 10)}</small>
                    </div>
                  ))}
                </div>
              )}
              {editor?.mode === 'edit' && editor.profileId === profile.id && (
                <section className="comfy-model-editor comfy-model-card__editor" aria-labelledby={`comfy-model-editor-title-${profile.id}`}>
                  <div className="comfy-model-editor__title" id={`comfy-model-editor-title-${profile.id}`}>
                    <span>모델 설정 편집</span>
                    <button
                      className="comfy-model-editor__close"
                      type="button"
                      aria-label="모델 설정 편집 닫기"
                      disabled={busy}
                      onClick={() => setEditor(null)}
                    >
                      ×
                    </button>
                  </div>
                  {message && <div className="comfy-model-editor__message" aria-live="polite">{message}</div>}
          {busy && progress && (
            <div className="comfy-import-progress" aria-live="polite">
              <div>{progressLabel(progress)}</div>
              <progress max={Math.max(1, progress.totalBytes)} value={progress.completedBytes} />
              {activeImportOperationId && (
                <button className="btn btn--ghost2 btn--sm" type="button" onClick={() => void cancelImport()}>
                  가져오기 취소
                </button>
              )}
            </div>
          )}
                  <div className="comfy-model-editor__grid">
                    <label>
                      <span>모델 이름</span>
                      <input
                        className="input"
                        value={editor.name}
                        maxLength={120}
                        placeholder="예: 모델 파일 이름"
                        onChange={(event) => setEditorField('name', event.target.value)}
                      />
                    </label>
                    <label className="comfy-model-editor__wide">
                      <span>이미지 특성 태그 (선택)</span>
                      <input
                        className="input"
                        value={editor.tags}
                        maxLength={MAX_TAG_INPUT_LENGTH}
                        placeholder="예: anime, character, illustration"
                        onChange={(event) => setEditorField('tags', event.target.value)}
                      />
                    </label>
                    {editorSupportsAgent && (
                      <>
                        {hasWorkflowInput('width') && (
                          <label>
                            <span>기본 이미지 너비</span>
                            <input
                              className="input"
                              type="number"
                              min={256}
                              max={2048}
                              step={64}
                              value={editor.defaults.width}
                              onChange={(event) => setDefault('width', Number(event.target.value))}
                            />
                          </label>
                        )}
                        {hasWorkflowInput('height') && (
                          <label>
                            <span>기본 이미지 높이</span>
                            <input
                              className="input"
                              type="number"
                              min={256}
                              max={2048}
                              step={64}
                              value={editor.defaults.height}
                              onChange={(event) => setDefault('height', Number(event.target.value))}
                            />
                          </label>
                        )}
                        <label className="switch comfy-model-editor__switch">
                          <input
                            type="checkbox"
                            checked={editor.agentEnabled}
                            disabled={!editor.agentEnabled && !editorReadiness?.ready}
                            onChange={(event) => setEditorField('agentEnabled', event.target.checked)}
                          />
                          <span className="switch__track" />
                          Agent 자동 선택 사용
                        </label>
                      </>
                    )}
                  </div>

                  {!editorSupportsAgent && (
                    <p className="comfy-model-editor__intro">
                      자동 워크플로 정보를 아직 확인하지 못했습니다. 모델 배포 문서의 구성 파일을 더 연결하거나 ComfyUI에서 직접 사용하세요.
                    </p>
                  )}
                  {editorProfile?.workflowTemplate && (
                    <>
                      <div className="comfy-workflow-summary">
                        <div>
                          <strong>사용자 워크플로</strong>
                          <span>{editorProfile.workflowTemplate.sourceFileName}</span>
                          <small>검증 ID {editorProfile.workflowTemplate.sha256.slice(0, 12)}</small>
                          {templateInputLabels.length > 0 && (
                            <small>Agent 입력: {templateInputLabels.join(' · ')}</small>
                          )}
                        </div>
                        <button
                          className="btn btn--ghost2 btn--sm"
                          type="button"
                          disabled={busy}
                          onClick={() => void removeWorkflow(editorProfile)}
                        >
                          연결 해제
                        </button>
                      </div>
                      {editorProfile.workflowTemplate.assetBindings.length > 0 && (
                        <ul className="comfy-workflow-bindings" aria-label="워크플로 모델 파일 연결 계약">
                          {editorProfile.workflowTemplate.assetBindings.map((binding) => {
                            const asset = editorProfile.assets.find((item) => item.id === binding.assetId)
                            return (
                              <li key={`${binding.nodeId}.${binding.input}`}>
                                <code>{binding.nodeId}.{binding.input}</code>
                                <span>→</span>
                                <b>{asset?.comfyName ?? binding.comfyName}</b>
                                <small>SHA-256 {binding.sha256.slice(0, 10)}</small>
                              </li>
                            )
                          })}
                        </ul>
                      )}
                    </>
                  )}
                  {editorSupportsAgent && (
                    <details className="comfy-model-editor__advanced">
                      <summary>고급 생성 기본값</summary>
                      {editorProfile?.workflowTemplate && (
                        <p className="comfy-model-editor__field-hint">
                          연결한 워크플로가 선언한 입력만 표시합니다. 제안 목록 외 값도 입력할 수 있지만, 실제 ComfyUI 노드 계약으로 검증됩니다.
                        </p>
                      )}
                      <div className="comfy-model-editor__grid">
                        {hasWorkflowInput('steps') && (
                          <label>
                            <span>반복 횟수 (Steps)</span>
                            <input
                              className="input"
                              type="number"
                              min={1}
                              max={60}
                              value={editor.defaults.steps}
                              onChange={(event) => setDefault('steps', Number(event.target.value))}
                            />
                          </label>
                        )}
                        {hasWorkflowInput('cfg') && (
                          <label>
                            <span>프롬프트 반영도 (CFG)</span>
                            <input
                              className="input"
                              type="number"
                              min={0}
                              max={30}
                              step={0.1}
                              value={editor.defaults.cfg}
                              onChange={(event) => setDefault('cfg', Number(event.target.value))}
                            />
                          </label>
                        )}
                        {hasWorkflowInput('sampler') && (
                          <label>
                            <span>Sampler</span>
                            <input
                              className="input"
                              list={`comfy-sampler-suggestions-${profile.id}`}
                              value={editor.defaults.sampler ?? getComfyGenerationDefaults(editor.family).sampler ?? ''}
                              onChange={(event) => setDefault('sampler', event.target.value)}
                            />
                            <datalist id={`comfy-sampler-suggestions-${profile.id}`}>
                              {SAMPLER_OPTIONS.map((sampler) => <option key={sampler} value={sampler} />)}
                            </datalist>
                          </label>
                        )}
                        {hasWorkflowInput('scheduler') && (editorProfile?.workflowTemplate !== undefined || editor.family !== 'flux2') && (
                          <label>
                            <span>Scheduler</span>
                            <input
                              className="input"
                              list={`comfy-scheduler-suggestions-${profile.id}`}
                              value={editor.defaults.scheduler ?? getComfyGenerationDefaults(editor.family).scheduler ?? ''}
                              onChange={(event) => setDefault('scheduler', event.target.value)}
                            />
                            <datalist id={`comfy-scheduler-suggestions-${profile.id}`}>
                              {SCHEDULER_OPTIONS.map((scheduler) => <option key={scheduler} value={scheduler} />)}
                            </datalist>
                          </label>
                        )}
                        <label>
                          <span>에이전트 선택 우선순위</span>
                          <input
                            className="input"
                            type="number"
                            min={-100}
                            max={100}
                            value={editor.priority}
                            onChange={(event) => setEditorField('priority', Number(event.target.value))}
                          />
                        </label>
                      </div>
                    </details>
                  )}
                  <div className="comfy-model-editor__actions">
                    <button className="btn btn--ghost2 btn--sm" type="button" disabled={busy} onClick={() => setEditor(null)}>
                      취소
                    </button>
                    <button
                      className="btn btn--sm"
                      type="button"
                      disabled={busy || !editor.name.trim()}
                      onClick={() => void saveEdit()}
                    >
                      {busy ? '처리 중…' : '설정 저장'}
                    </button>
                  </div>
                </section>
              )}
              <div className="comfy-model-card__foot">
                <span>
                  {isAgentWorkflowFamily(profile.family) || profile.workflowTemplate
                    ? `${profile.defaults.width}×${profile.defaults.height} · ${profile.defaults.steps} steps · CFG ${profile.defaults.cfg} · ${profile.defaults.sampler ?? 'euler'}${profile.family === 'flux2' ? ' · Flux2Scheduler' : ` / ${profile.defaults.scheduler ?? (profile.family === 'flux1' ? 'simple' : 'normal')}`}`
                    : '자동 워크플로 정보 확인 필요 · ComfyUI에서 직접 사용 가능'}
                </span>
                <div>
                  <button
                    className="btn btn--ghost2 btn--sm"
                    type="button"
                    disabled={busy || !canConnectModel}
                    onClick={() => addAsset(profile)}
                  >
                    구성 파일 추가
                  </button>
                  <button
                    className="btn btn--ghost2 btn--sm"
                    type="button"
                    disabled={busy}
                    title="ComfyUI에서 Save (API Format)으로 내보낸 JSON을 연결합니다."
                    onClick={() => void importWorkflow(profile)}
                  >
                    {profile.workflowTemplate ? '워크플로 교체' : '워크플로 연결'}
                  </button>
                  <button className="btn btn--ghost2 btn--sm" type="button" disabled={busy} onClick={() => editProfile(profile)}>
                    설정 편집
                  </button>
                  <button className="btn btn--stop btn--sm" type="button" disabled={busy} onClick={() => void unregister(profile)}>
                    등록 해제
                  </button>
                </div>
              </div>
            </article>
          )
        })}
      </div>
    </div>
  )
}

export default ComfyModelManager
