import type { Pane } from './api'
import type { Session } from './Sidebar'

export interface SelectionSnapshot { sessions: Session[]; selected?: string }

/** Resolve a missing selection against the last visible tab order. */
export function selectPane(sessions: Session[], selected: string | null, previous: SelectionSnapshot, defaultThread: string | null | undefined, lastPane: Record<string, string>): Pane | undefined {
  const panes = sessions.flatMap(session => session.panes)
  const wanted = selected ?? previous.selected
  const current = panes.find(pane => pane.id === wanted)
  if (current) return current
  const oldOwner = previous.selected === wanted ? previous.sessions.find(session => session.panes.some(pane => pane.id === wanted)) : undefined
  if (oldOwner) {
    const owner = sessions.find(session => session.id === oldOwner.id)
    const at = oldOwner.panes.findIndex(pane => pane.id === wanted)
    // Search outward in the visible order. Multiple tabs can end in the same poll.
    const neighbors = [...oldOwner.panes.slice(0, at).reverse(), ...oldOwner.panes.slice(at + 1)]
    const neighbor = neighbors.map(old => owner?.panes.find(pane => pane.id === old.id)).find(Boolean)
    if (neighbor) return neighbor
    if (owner?.panes.length) return owner.panes[0]
    const sessionAt = previous.sessions.indexOf(oldOwner)
    const adjacent = [...previous.sessions.slice(0, sessionAt).reverse(), ...previous.sessions.slice(sessionAt + 1)]
      .map(old => sessions.find(session => session.id === old.id)).find(session => session?.panes.length)
    const next = adjacent ?? sessions.find(session => session.panes.length)
    return next?.panes.find(pane => pane.id === lastPane[next.id]) ?? next?.panes[0]
  }
  return panes.find(pane => pane.id === selected) ?? panes.find(pane => defaultThread != null && pane.thread === defaultThread) ?? sessions[0]?.panes[0]
}
