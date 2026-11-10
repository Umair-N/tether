import { useEffect, useState, useSyncExternalStore } from 'react'
import type { Look } from './layouts'

// A new or changed custom layout can hide the controls that would undo it. So Tether tries it for 10 seconds and goes
// back to the previous look unless the user keeps it (like a display-resolution change). Restore default works the
// other way round: it applies at once and offers Undo for 10 seconds.

export interface Trial { previous: Look | null; kind: 'trial' | 'undo'; name: string; until: number }
export const TRIAL_MS = 10000

let current: Trial | null = null
const listeners = new Set<() => void>()
const emit = () => listeners.forEach(listener => listener())

export function startTrial(previous: Look | null, kind: Trial['kind'], name: string) {
  current = { previous, kind, name, until: Date.now() + TRIAL_MS }
  emit()
}
export function endTrial() { if (current) { current = null; emit() } }

export function useTrial() {
  return useSyncExternalStore(listener => { listeners.add(listener); return () => listeners.delete(listener) }, () => current)
}

/** Seconds left, updated every 250 ms while a trial runs. At zero a trial reverts and an undo offer goes away. */
export function useTrialClock(trial: Trial | null, revert: (previous: Look | null) => void) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!trial) return
    const tick = () => {
      const time = Date.now()
      setNow(time)
      if (time < trial.until) return
      endTrial()
      if (trial.kind === 'trial') revert(trial.previous)
    }
    const timer = setInterval(tick, 250)
    return () => clearInterval(timer)
  }, [trial, revert])
  return trial ? Math.max(0, Math.ceil((trial.until - now) / 1000)) : 0
}
