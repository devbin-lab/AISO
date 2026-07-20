import { useEffect, useState } from 'react'
import type {
  ComfyAssetKind,
  ComfyAssetSlot,
  ComfyGenerationDefaults,
  ComfyModelFamily,
  ComfyModelImportProgress,
  ComfyModelProfile
} from '../../../shared/comfy-model'
import { DEFAULT_COMFY_GENERATION } from '../../../shared/comfy-model'
import { getComfyAgentReadiness } from '../../../shared/comfy-model'
import { confirmDialog } from './ConfirmDialog'

interface Props {
  installPath: string
  onChanged?: (profiles: ComfyModelProfile[]) => void
}

type EditorMode = 'new' | 'asset' | 'edit'

interface EditorState {
  mode: EditorMode
  profileId?: string
  name: string
  family: ComfyModelFamily
  assetKind: ComfyAssetKind
  assetSlot: ComfyAssetSlot
  tags: string
  agentEnabled: boolean
  priority: number
  defaults: ComfyGenerationDefaults
}

const FAMILY_LABEL: Record<ComfyModelFamily, string> = {
  sd15: 'Stable Diffusion 1.5',
  sdxl: 'SDXL',
  flux1: 'FLUX.1',
  flux2: 'FLUX.2',
  custom: '사용자 정의'
}

const ASSET_LABEL: Record<ComfyAssetKind, string> = {
  checkpoint: '체크포인트',
  diffusion_model: 'Diffusion model',
  text_encoder: 'Text encoder',
  vae: 'VAE',
  lora: 'LoRA',
  controlnet: 'ControlNet'
}

const SLOT_BY_KIND: Record<Exclude<ComfyAssetKind, 'text_encoder'>, ComfyAssetSlot> = {
  checkpoint: 'checkpoint',
  diffusion_model: 'diffusion_model',
  vae: 'vae',
  lora: 'lora',
  controlnet: 'controlnet'
}

const SAMPLER_OPTIONS = ['euler', 'euler_ancestral', 'dpmpp_2m', 'dpmpp_2m_sde'] as const
const SCHEDULER_OPTIONS = ['normal', 'karras', 'simple', 'beta'] as const

function defaultsForFamily(family: ComfyModelFamily): Pick<ComfyGenerationDefaults, 'sampler' | 'scheduler'> {
  return family === 'flux1'
    ? { sampler: 'euler', scheduler: 'simple' }
    : { sampler: 'euler_ancestral', scheduler: 'normal' }
}

function emptyEditor(): EditorState {
  return {
    mode: 'new',
    name: '',
    family: 'sdxl',
    assetKind: 'checkpoint',
    assetSlot: 'checkpoint',
    tags: '',
    agentEnabled: true,
    priority: 0,
    defaults: { ...DEFAULT_COMFY_GENERATION }
  }
}

function editorFromProfile(profile: ComfyModelProfile, mode: EditorMode): EditorState {
  return {
    mode,
    profileId: profile.id,
    name: profile.name,
    family: profile.family,
    assetKind: 'checkpoint',
    assetSlot: 'checkpoint',
    tags: profile.tags.join(', '),
    agentEnabled: profile.agentEnabled,
    priority: profile.priority,
    defaults: { ...profile.defaults }
  }
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

function ComfyModelManager({ installPath, onChanged }: Props): React.JSX.Element {
  const [profiles, setProfiles] = useState<ComfyModelProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [progress, setProgress] = useState<ComfyModelImportProgress | null>(null)
  const [message, setMessage] = useState('')

  const load = async (): Promise<void> => {
    setLoading(true)
    try {
      const registry = await window.api.comfy.models.list()
      setProfiles(registry.profiles)
      onChanged?.(registry.profiles)
    } catch (error) {
      setMessage(`모델 목록을 불러오지 못했습니다: ${String(error)}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    return window.api.comfy.models.onImportProgress(setProgress)
  }, [])

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

  const startNew = (): void => {
    setMessage('')
    setProgress(null)
    setEditor(emptyEditor())
  }

  const addAsset = (profile: ComfyModelProfile): void => {
    setMessage('')
    setProgress(null)
    setEditor(editorFromProfile(profile, 'asset'))
  }

  const editProfile = (profile: ComfyModelProfile): void => {
    setMessage('')
    setProgress(null)
    setEditor(editorFromProfile(profile, 'edit'))
  }

  const importAssets = async (): Promise<void> => {
    if (!editor || editor.mode === 'edit' || busy) return
    if (!installPath.trim()) {
      setMessage('먼저 Windows Portable 폴더를 선택하고 설정을 저장해 주세요. 외부 서버 주소만으로는 로컬 파일을 복사할 수 없습니다.')
      return
    }
    if (!editor.name.trim()) {
      setMessage('모델 프로필 이름을 입력해 주세요.')
      return
    }
    setBusy(true)
    setMessage('파일을 선택해 주세요. 원본은 보존되고 ComfyUI 모델 폴더로 복사됩니다.')
    setProgress(null)
    const operationId = typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`
    const assetSlot = editor.assetKind === 'text_encoder'
      ? editor.assetSlot
      : SLOT_BY_KIND[editor.assetKind]
    try {
      const result = await window.api.comfy.models.importAssets({
        operationId,
        ...(editor.profileId ? { profileId: editor.profileId } : {}),
        name: editor.name.trim(),
        family: editor.family,
        capabilities: ['txt2img'],
        tags: editor.tags.split(',').map((tag) => tag.trim()).filter(Boolean),
        agentEnabled: editor.agentEnabled,
        priority: editor.priority,
        defaults: editor.defaults,
        assetKind: editor.assetKind,
        assetSlot
      })
      if (result.canceled) {
        setMessage('모델 가져오기를 취소했습니다.')
        return
      }
      const reused = result.reused.length
      setMessage(
        `모델 프로필을 저장했습니다. 새로 복사 ${result.imported.length}개${reused ? ` · 기존 파일 재사용 ${reused}개` : ''}`
      )
      setEditor(null)
      await load()
    } catch (error) {
      setMessage(`모델 가져오기 실패: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setBusy(false)
    }
  }

  const saveEdit = async (): Promise<void> => {
    if (!editor?.profileId || editor.mode !== 'edit' || busy) return
    setBusy(true)
    setMessage('')
    try {
      await window.api.comfy.models.update(editor.profileId, {
        name: editor.name,
        family: editor.family,
        capabilities: ['txt2img'],
        tags: editor.tags.split(',').map((tag) => tag.trim()).filter(Boolean),
        agentEnabled: editor.agentEnabled,
        priority: editor.priority,
        defaults: editor.defaults
      })
      setEditor(null)
      setMessage('모델 프로필 정보를 저장했습니다.')
      await load()
    } catch (error) {
      setMessage(`모델 정보 저장 실패: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setBusy(false)
    }
  }

  const toggleAgent = async (profile: ComfyModelProfile): Promise<void> => {
    try {
      await window.api.comfy.models.update(profile.id, { agentEnabled: !profile.agentEnabled })
      await load()
    } catch (error) {
      setMessage(`Agent 사용 설정 실패: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  const unregister = async (profile: ComfyModelProfile): Promise<void> => {
    const confirmed = await confirmDialog({
      title: '모델 등록 해제',
      message:
        `'${profile.name}' 프로필을 Aiso 목록에서 제거합니다.\n` +
        'ComfyUI에 복사된 실제 모델 파일은 삭제하지 않습니다.',
      confirmLabel: '등록 해제',
      danger: true
    })
    if (!confirmed) return
    try {
      await window.api.comfy.models.unregister(profile.id)
      setMessage('모델 등록을 해제했습니다. ComfyUI 모델 파일은 그대로 유지됩니다.')
      if (editor?.profileId === profile.id) setEditor(null)
      await load()
    } catch (error) {
      setMessage(`등록 해제 실패: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  return (
    <div className="comfy-library">
      <div className="comfy-library__head">
        <div>
          <div className="row__label">모델 라이브러리</div>
          <div className="row__hint">
            사용자가 직접 받은 <code>.safetensors</code> 파일을 ComfyUI에 복사하고 Agent가 사용할 기술 프로필로 등록합니다.
          </div>
        </div>
        <button className="btn btn--sm" type="button" disabled={busy} onClick={startNew}>
          모델 가져오기
        </button>
      </div>

      {!installPath.trim() && (
        <div className="comfy-library__notice">
          외부 ComfyUI 서버만 연결되어 있습니다. 파일 가져오기를 사용하려면 Windows Portable 폴더를 선택한 뒤 설정을 저장하세요.
        </div>
      )}
      {message && <div className="comfy-library__message" aria-live="polite">{message}</div>}
      {busy && progress && (
        <div className="comfy-import-progress" aria-live="polite">
          <div>{progressLabel(progress)}</div>
          <progress max={Math.max(1, progress.totalBytes)} value={progress.completedBytes} />
        </div>
      )}

      {editor && (
        <div className="comfy-model-editor">
          <div className="comfy-model-editor__title">
            {editor.mode === 'new' ? '새 모델 프로필' : editor.mode === 'asset' ? '모델 자산 추가' : '모델 정보 편집'}
          </div>
          <div className="comfy-model-editor__grid">
            <label>
              <span>프로필 이름</span>
              <input
                className="input"
                value={editor.name}
                maxLength={120}
                onChange={(event) => setEditorField('name', event.target.value)}
              />
            </label>
            <label>
              <span>모델 계열</span>
              <select
                className="input"
                value={editor.family}
                onChange={(event) => {
                  const family = event.target.value as ComfyModelFamily
                  setEditor((current) => current
                    ? {
                        ...current,
                        family,
                        defaults: { ...current.defaults, ...defaultsForFamily(family) }
                      }
                    : current)
                }}
              >
                {Object.entries(FAMILY_LABEL).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </label>
            {editor.mode !== 'edit' && (
              <>
                <label>
                  <span>추가할 파일 역할</span>
                  <select
                    className="input"
                    value={editor.assetKind}
                    onChange={(event) => {
                      const kind = event.target.value as ComfyAssetKind
                      setEditor((current) => current
                        ? {
                            ...current,
                            assetKind: kind,
                            assetSlot: kind === 'text_encoder' ? 'clip_l' : SLOT_BY_KIND[kind]
                          }
                        : current)
                    }}
                  >
                    {Object.entries(ASSET_LABEL).map(([value, label]) => (
                      <option key={value} value={value}>{label}</option>
                    ))}
                  </select>
                </label>
                {editor.assetKind === 'text_encoder' && (
                  <label>
                    <span>Text encoder 슬롯</span>
                    <select
                      className="input"
                      value={editor.assetSlot}
                      onChange={(event) => setEditorField('assetSlot', event.target.value as ComfyAssetSlot)}
                    >
                      <option value="clip_l">CLIP-L</option>
                      <option value="t5xxl">T5XXL</option>
                    </select>
                  </label>
                )}
              </>
            )}
            <label className="comfy-model-editor__wide">
              <span>특성 태그</span>
              <input
                className="input"
                value={editor.tags}
                placeholder="anime, character, illustration"
                onChange={(event) => setEditorField('tags', event.target.value)}
              />
            </label>
            <label>
              <span>기본 너비</span>
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
            <label>
              <span>기본 높이</span>
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
            <label>
              <span>기본 Steps</span>
              <input
                className="input"
                type="number"
                min={1}
                max={60}
                value={editor.defaults.steps}
                onChange={(event) => setDefault('steps', Number(event.target.value))}
              />
            </label>
            <label>
              <span>기본 CFG</span>
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
            <label>
              <span>기본 Sampler</span>
              <select
                className="input"
                value={editor.defaults.sampler ?? defaultsForFamily(editor.family).sampler}
                onChange={(event) => setDefault('sampler', event.target.value)}
              >
                {SAMPLER_OPTIONS.map((sampler) => (
                  <option key={sampler} value={sampler}>{sampler}</option>
                ))}
              </select>
            </label>
            <label>
              <span>기본 Scheduler</span>
              <select
                className="input"
                value={editor.defaults.scheduler ?? defaultsForFamily(editor.family).scheduler}
                onChange={(event) => setDefault('scheduler', event.target.value)}
              >
                {SCHEDULER_OPTIONS.map((scheduler) => (
                  <option key={scheduler} value={scheduler}>{scheduler}</option>
                ))}
              </select>
            </label>
            <label>
              <span>Agent 우선순위</span>
              <input
                className="input"
                type="number"
                min={-100}
                max={100}
                value={editor.priority}
                onChange={(event) => setEditorField('priority', Number(event.target.value))}
              />
            </label>
            <label className="switch comfy-model-editor__switch">
              <input
                type="checkbox"
                checked={editor.agentEnabled}
                onChange={(event) => setEditorField('agentEnabled', event.target.checked)}
              />
              <span className="switch__track" />
              Agent 자동 선택 허용
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
              onClick={() => void (editor.mode === 'edit' ? saveEdit() : importAssets())}
            >
              {busy ? '처리 중…' : editor.mode === 'edit' ? '정보 저장' : '파일 선택 및 가져오기'}
            </button>
          </div>
        </div>
      )}

      <div className="comfy-model-list">
        {loading ? (
          <div className="comfy-model-empty">모델 목록을 불러오는 중…</div>
        ) : profiles.length === 0 ? (
          <div className="comfy-model-empty">Aiso에 등록된 모델 프로필이 없습니다.</div>
        ) : profiles.map((profile) => {
          const readiness = getComfyAgentReadiness(profile)
          return (
          <article className="comfy-model-card" key={profile.id}>
            <div className="comfy-model-card__head">
              <div className="comfy-model-card__identity">
                <strong>{profile.name}</strong>
                <span>{FAMILY_LABEL[profile.family]}</span>
                <span>우선순위 {profile.priority}</span>
              </div>
              <label className="switch">
                <input
                  type="checkbox"
                  checked={profile.agentEnabled}
                  onChange={() => void toggleAgent(profile)}
                />
                <span className="switch__track" />
                Agent
              </label>
            </div>
            {profile.tags.length > 0 && (
              <div className="comfy-model-tags">
                {profile.tags.map((tag) => <span key={tag}>{tag}</span>)}
              </div>
            )}
            <div className={`comfy-model-readiness ${readiness.ready ? 'comfy-model-readiness--ready' : ''}`}>
              {readiness.detail}
              {!readiness.ready && profile.agentEnabled && ' · Agent 자동 선택에서 제외됩니다.'}
            </div>
            {readiness.notices.map((notice) => (
              <div className="comfy-model-readiness" key={notice}>{notice}</div>
            ))}
            <div className="comfy-model-assets">
              {profile.assets.map((asset) => (
                <div key={asset.id} title={`SHA-256 ${asset.sha256}\n${asset.relativePath}`}>
                  <span>{asset.slot ? asset.slot.toUpperCase() : ASSET_LABEL[asset.kind]}</span>
                  <b>{asset.comfyName}</b>
                  <small>{bytesLabel(asset.size)} · {asset.sha256.slice(0, 10)}</small>
                </div>
              ))}
            </div>
            <div className="comfy-model-card__foot">
              <span>
                {profile.defaults.width}×{profile.defaults.height} · {profile.defaults.steps} steps · CFG {profile.defaults.cfg}
                {' · '}{profile.defaults.sampler ?? 'euler'} / {profile.defaults.scheduler ?? (profile.family === 'flux1' ? 'simple' : 'normal')}
              </span>
              <div>
                <button className="btn btn--ghost2 btn--sm" type="button" disabled={busy || !installPath.trim()} onClick={() => addAsset(profile)}>
                  자산 추가
                </button>
                <button className="btn btn--ghost2 btn--sm" type="button" disabled={busy} onClick={() => editProfile(profile)}>
                  정보 편집
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
