import { NVIDIA_CAPABILITY_MAX_AGE_MS } from './nvidia-capability-cache.ts'

/** A grant must expire no later than the capability evidence that authorized it. */
export function capabilityBoundGrantTtlSeconds(
  checkedAt: string,
  now: number = Date.now()
): number {
  const checkedAtMs = Date.parse(checkedAt)
  const remainingMs = NVIDIA_CAPABILITY_MAX_AGE_MS - (now - checkedAtMs)
  const ttlSeconds = Math.min(60, Math.floor(remainingMs / 1000))
  if (!Number.isFinite(ttlSeconds) || ttlSeconds < 1) {
    throw new Error('NVIDIA capability authorization expired')
  }
  return ttlSeconds
}
