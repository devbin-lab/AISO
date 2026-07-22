import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = ResizeObserverMock
}

afterEach(() => {
  cleanup()
})
