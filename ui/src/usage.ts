import { useSyncExternalStore } from 'react'
import type { UsageData } from './api'

// The sidebar already polls plan usage. It shares the latest report here, so the status bar shows it without a second poll.
let latest: UsageData | undefined
const listeners = new Set<() => void>()
export function shareUsage(data: UsageData | undefined) {
  if (!data || data === latest) return
  latest = data
  listeners.forEach(listener => listener())
}
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } }
export const useSharedUsage = () => useSyncExternalStore(subscribe, () => latest)
