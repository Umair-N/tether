import { useRef, useState } from 'react'
import { X } from 'lucide-react'
import type { Pane } from './api'
import { useModal } from './hooks'
import { RunningList, needsYou, runningPanes, unreadPanes } from './Sidebar'
import type { Session } from './Sidebar'

// Two lists answer "what needs a look?": Active (agents that wait on you or work now) and Unread (turns that ended
// while you looked elsewhere). The bubble's popover and the Agents page show the same tabs.
export type AgentTab = 'active' | 'unread'
type Seen = Record<string, number>

export function agentLists(sessions: Session[], seen: Seen, selected?: string) {
  const active = runningPanes(sessions)
  const unread = unreadPanes(sessions, seen, selected)
  return { active, unread, waiting: active.filter(item => needsYou(item.pane.state)).length }
}
// Open on what needs you first, then on what you missed.
export const firstTab = (lists: ReturnType<typeof agentLists>): AgentTab => !lists.waiting && lists.unread.length ? 'unread' : 'active'

export function AgentTabs({ sessions, seen, selected, tab, onTab, onOpen, onMarkRead }: { sessions: Session[]; seen: Seen; selected?: string; tab: AgentTab; onTab: (tab: AgentTab) => void; onOpen: (pane: Pane) => void; onMarkRead: (panes: string[]) => void }) {
  const lists = agentLists(sessions, seen, selected)
  const strip = useRef<HTMLDivElement>(null)
  const tabs: { key: AgentTab; label: string; count: number }[] = [
    { key: 'active', label: 'Active', count: lists.active.length },
    { key: 'unread', label: 'Unread', count: lists.unread.length },
  ]
  const items = tab === 'active' ? lists.active : lists.unread
  return <>
    <div className="agent-tabs" role="tablist" aria-label="Agents" ref={strip} onKeyDown={event => {
      if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return
      event.preventDefault()
      const next = tab === 'active' ? 'unread' : 'active'
      onTab(next)
      strip.current?.querySelector<HTMLButtonElement>(`[data-tab="${next}"]`)?.focus()
    }}>
      {tabs.map(item => <button key={item.key} type="button" role="tab" data-tab={item.key} id={`agent-tab-${item.key}`} aria-selected={tab === item.key} aria-controls="agent-tab-panel" tabIndex={tab === item.key ? 0 : -1} onClick={() => onTab(item.key)}>
        {item.label}<span className={`agent-tab-count ${item.key === 'active' && lists.waiting ? 'attention' : ''}`}>{item.count}</span>
      </button>)}
    </div>
    <div className="agent-tab-panel" role="tabpanel" id="agent-tab-panel" aria-labelledby={`agent-tab-${tab}`}>
      {items.length
        ? <RunningList running={items} selected={selected} seen={seen} onOpen={onOpen} />
        : <p className="group-empty">{tab === 'active' ? 'No agent is working or waiting on you.' : 'Nothing unread. You saw every finished turn.'}</p>}
      {tab === 'unread' && lists.unread.length > 0 && <button type="button" className="agent-mark-read" onClick={() => onMarkRead(lists.unread.map(item => item.pane.id))}>Mark all read</button>}
    </div>
  </>
}

// The Agents page: a full sheet with the same tabs. A tap on Tether's summary notification opens it.
export function AgentsPage({ sessions, seen, selected, onOpen, onMarkRead, onClose }: { sessions: Session[]; seen: Seen; selected?: string; onOpen: (pane: Pane) => void; onMarkRead: (panes: string[]) => void; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const [tab, setTab] = useState<AgentTab>(() => firstTab(agentLists(sessions, seen, selected)))
  return <dialog {...modal} className="sheet agents-sheet" aria-labelledby="agents-title">
    <div className="sheet-heading"><h2 id="agents-title">Agents</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <AgentTabs sessions={sessions} seen={seen} selected={selected} tab={tab} onTab={setTab} onOpen={pane => { onClose(); onOpen(pane) }} onMarkRead={onMarkRead} />
  </dialog>
}
