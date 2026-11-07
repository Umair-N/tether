import type { Pane } from './api'
import type { Session } from './Sidebar'
import { inboxItems } from './needs'
import type { BrowserAskRow, InboxItem } from './needs'
import { formatElapsed, isDone, needsYou } from './paneState'

export interface HomeCard extends InboxItem { session?: Session }
export interface HomeGroups { needs: HomeCard[]; working: HomeCard[]; done: HomeCard[] }

const oldestFirst = (a: HomeCard, b: HomeCard) =>
  (a.at || Infinity) - (b.at || Infinity) || a.pane.id.localeCompare(b.pane.id)

export function homeGroups(panes: Pane[], sessions: Session[], seen: Record<string, number>, selected: string | undefined, asks: BrowserAskRow[]): HomeGroups {
  const agents = panes.filter(pane => pane.agent !== 'shell')
  const visible = sessions.flatMap(session => session.panes).filter(pane => pane.agent !== 'shell')
  const withSession = (item: InboxItem): HomeCard => ({ ...item, session: sessions.find(session => session.id === item.pane.tmux_session) })
  const asking = inboxItems(agents, seen, selected, asks).filter(item => item.kind !== 'done')
  // A restart stopped this tab's turn (auto_resume.py). Nothing runs until the person sends a message.
  const stopped = agents.filter(pane => pane.state === 'Stopped' && !asking.some(item => item.pane.id === pane.id))
    .map((pane): InboxItem => ({ pane, kind: 'stopped', text: '', at: pane.since ?? 0 }))
  const needs = [...asking, ...stopped].map(withSession).sort(oldestFirst)
  const waiting = new Set(needs.map(item => item.pane.id))
  const working = visible.filter(pane => pane.state === 'Working' && !waiting.has(pane.id)).map(pane => withSession({
    pane, kind: 'working', text: pane.tasks?.join(', ') ?? '', at: pane.since ?? 0,
  })).sort(oldestFirst)
  const done = inboxItems(visible.filter(pane => !needsYou(pane.state) && !waiting.has(pane.id) && isDone(pane, seen, selected)), seen, selected)
    .map(withSession).sort((a, b) => b.at - a.at || a.pane.id.localeCompare(b.pane.id))
  return { needs, working, done }
}

export function cardTime(card: { kind: string; at: number | null | undefined }, now: number): string {
  if (!card.at) return ''
  const seconds = Math.max(0, now - card.at)
  if (card.kind === 'done') {
    if (seconds < 60) return 'Just now'
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
    return `${Math.floor(seconds / 86400)}d ago`
  }
  if (seconds < 86400) return formatElapsed(seconds)
  return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds % 86400 / 3600)}h`
}

export function hasAgents(panes: Pane[]): boolean {
  return panes.some(pane => pane.agent !== 'shell')
}
