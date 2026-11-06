import type { Pane } from './api'
import type { ArchivedTab } from './prefs'
import type { Session } from './Sidebar'

export function isTabArchived(pane: Pane, map: Record<string, ArchivedTab>): boolean {
  const entry = map[pane.id]
  return !!entry && entry.dir === pane.dir && (entry.thread === pane.thread || entry.thread === null || pane.thread === null)
}

export function withTabArchived(map: Record<string, ArchivedTab>, pane: Pane, liveIds: ReadonlySet<string>): Record<string, ArchivedTab> {
  return { ...Object.fromEntries(Object.entries(map).filter(([id]) => liveIds.has(id))), [pane.id]: { thread: pane.thread, dir: pane.dir } }
}

export function withoutTab(map: Record<string, ArchivedTab>, paneId: string): Record<string, ArchivedTab> {
  const next = { ...map }
  delete next[paneId]
  return next
}

export function tabNeighbor(session: Session, paneId: string): Pane | undefined {
  const at = session.panes.findIndex(pane => pane.id === paneId)
  return at < 0 ? undefined : session.panes[at - 1] ?? session.panes[at + 1]
}
