import { vi } from 'vitest'

/**
 * 설정 화면이 뜨는 데 필요한 window.api 대역.
 *
 * 설정 탭은 한 화면 안에 진단·LLM·ComfyUI·My DB·스킬·디스코드가 모두 들어 있어서,
 * 한 섹션만 보려 해도 나머지 섹션의 useEffect 가 함께 돈다. 대역이 한 조각이라도
 * 빠지면 그 섹션이 마운트되며 터지고, 정작 보려던 섹션의 테스트가 실패한다.
 * 그래서 대역은 한 곳에 두고 테스트마다 필요한 조각만 덮어쓴다.
 */
export function installSettingsApiStub(overrides: Record<string, unknown> = {}): void {
  Object.defineProperty(window, 'api', {
    configurable: true,
    value: {
      settings: {
        recoveryStatus: vi.fn().mockResolvedValue({ kind: 'none' })
      },
      nvidia: {
        credential: {
          status: vi.fn().mockResolvedValue({
            encryptionAvailable: true,
            hasStoredCredential: false,
            matchesCurrentBinding: false,
            usableForCurrentBinding: false
          }),
          save: vi.fn().mockResolvedValue(undefined),
          replace: vi.fn().mockResolvedValue(undefined),
          delete: vi.fn().mockResolvedValue(undefined)
        },
        models: {
          refresh: vi.fn().mockResolvedValue({
            models: ['model/a', 'model/b'],
            refreshedAt: '2026-08-02T05:00:00.000Z'
          })
        },
        capabilities: {
          status: vi.fn().mockResolvedValue(null),
          probe: vi.fn().mockResolvedValue({
            schemaVersion: 1,
            binding: {
              deploymentMode: 'build',
              endpoint: 'https://integrate.api.nvidia.com/v1'
            },
            model: 'model/a',
            capabilities: { chat: 'supported', stream: 'supported', tools: 'supported' },
            checkedAt: '2026-08-02T05:01:00.000Z'
          }),
          clear: vi.fn().mockResolvedValue(undefined)
        }
      },
      backend: {
        token: vi.fn(() => 'test-token')
      },
      updates: {
        version: vi.fn().mockResolvedValue('0.3.0'),
        onStatus: vi.fn(() => () => {})
      },
      comfy: {
        pickInstall: vi.fn().mockResolvedValue(null),
        models: {
          list: vi.fn().mockResolvedValue({ profiles: [] }),
          onImportProgress: vi.fn(() => () => {})
        }
      },
      myDb: {
        storageRoot: vi.fn().mockResolvedValue('C:\Users\tester\Documents\Aiso My DB'),
        pickStorageRoot: vi.fn().mockResolvedValue(null)
      },
      skills: {
        list: vi.fn().mockResolvedValue([]),
        remove: vi.fn().mockResolvedValue(undefined)
      },
      discord: {
        hasToken: vi.fn().mockResolvedValue(false),
        status: vi.fn().mockResolvedValue(null),
        schedules: vi.fn().mockResolvedValue({ jobs: [] }),
        scheduleRemove: vi.fn().mockResolvedValue(undefined),
        channels: vi.fn().mockResolvedValue({ guilds: [] }),
        pickRepo: vi.fn().mockResolvedValue(null),
        repoBranches: vi.fn().mockResolvedValue({ ok: true, remote: [], local: [] }),
        repoReportAdd: vi.fn().mockResolvedValue({ ok: true }),
        repoReportNow: vi.fn().mockResolvedValue({ ok: true, detail: '보고를 보냈습니다.' }),
        setLlmProvider: vi.fn(),
        saveToken: vi.fn().mockResolvedValue(undefined),
        apply: vi.fn().mockResolvedValue({ ok: true })
      },
      ...overrides
    }
  })
}
