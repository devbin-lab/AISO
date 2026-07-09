export interface PingResult {
  message: string
  time: string
  versions: {
    electron: string
    chrome: string
    node: string
    v8: string
  }
}
