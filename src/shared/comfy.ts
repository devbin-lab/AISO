export interface ComfyDeviceInfo {
  name: string
  type: string
  vramTotal?: number
  vramFree?: number
}

export interface ComfyHealthInfo {
  online: boolean
  baseUrl: string
  version?: string
  frontendVersion?: string
  devices: ComfyDeviceInfo[]
  detail?: string
}

export interface ComfyCheckpointsInfo {
  checkpoints: string[]
}

export type ComfyLaunchState = 'started' | 'already-running' | 'already-started' | 'error'

export interface ComfyLaunchResult {
  ok: boolean
  state: ComfyLaunchState
  detail?: string
  pid?: number
}

export interface ComfySurfaceBounds {
  x: number
  y: number
  width: number
  height: number
}

export interface ComfySurfaceRequest {
  visible: boolean
  baseUrl: string
  bounds?: ComfySurfaceBounds
}
