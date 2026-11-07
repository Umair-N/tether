import type { Pane } from './api'

// A turn that ended after you last looked at the pane. The open pane never counts: you are reading it.
// A permission prompt ("Approval") or a question, menu or plan ("Input needed"): the agent waits on you.
export const needsYou = (state: string) => state === 'Approval' || state === 'Input needed'
export const isDone = (pane: Pane, seen: Record<string, number>, selected?: string) =>
  pane.id !== selected && pane.state !== 'Working' && !needsYou(pane.state) && !!pane.finished && pane.finished > (seen[pane.id] ?? 0)

// T3's formatElapsedSeconds: 42s, 3m 05s, 1h 07m.
export function formatElapsed(totalSeconds: number) {
  const seconds = Math.max(0, Math.floor(totalSeconds))
  const minutes = Math.floor(seconds / 60)
  if (minutes === 0) return `${seconds}s`
  const hours = Math.floor(minutes / 60)
  if (hours === 0) return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`
  return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`
}

// "~/apps/tether/src/panels" → "…/src/panels": the end of the path names where you are.
export function shortFolder(dir: string) {
  const parts = (dir || '~').split('/')
  return parts.length > 3 ? `…/${parts.slice(-2).join('/')}` : dir || '~'
}

