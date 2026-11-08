import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import type { Api, Pane } from './api'
import { Card } from './Home'
import type { HomeCard, HomeGroups } from './homeGroups'
import { KIND_LABEL } from './needs'
import { AgentIcon, chatTitle, sessionName } from './Sidebar'
import type { Session } from './Sidebar'
import { useAnswers } from './useAnswers'

// Panels that only layouts show (layouts.ts). The waiting slot is one component, so Attention mode can swap in its
// own "Waiting on you" box here without other changes.

const focusWaiting = (id: string) => document.querySelector<HTMLButtonElement>(`.l-waiting [data-pane="${CSS.escape(id)}"] .home-open`)?.focus()

/** The `waiting` panel: Home's "Needs you" cards, with the same answer buttons and keys. */
export function WaitingPanel({ api, cards, edge, asks, onOpen, onUpdate }: { api: Api; cards: HomeCard[]; edge?: boolean; asks?: ReactNode; onOpen: (pane: Pane) => void; onUpdate: () => void }) {
  const { busy, errors, answer, fail, onAnswerKey } = useAnswers({ api, items: cards, active: true, onUpdate, focusRow: focusWaiting })
  const onKey = (event: ReactKeyboardEvent<HTMLLIElement>, card: HomeCard) => { if (card.kind !== 'working' && card.kind !== 'done') onAnswerKey(event, card) }
  return <section className="l-panel l-waiting" data-edge={edge ? '' : undefined} aria-labelledby="l-waiting-title">
    <h2 id="l-waiting-title" className="l-panel-title"><span className="state-dot approval" aria-hidden="true" />Needs you<span className="l-count">{cards.length}</span></h2>
    {asks}
    {cards.length ? <ul className="l-waiting-list">{cards.map(card => <Card key={`${card.pane.id}:${card.kind}${card.ask ?? ''}`} api={api} card={card} group="needs" waiting={busy === card.pane.id} error={errors[card.pane.id]}
      onOpen={onOpen} onAnswer={answer} onFail={fail} onKey={event => onKey(event, card)} onMarkRead={() => undefined} />)}</ul>
      : !asks && <p className="l-empty">Nothing waits on you.</p>}
  </section>
}

const STATUS_GROUPS = [
  { key: 'needs', label: 'Needs you', dot: 'approval' },
  { key: 'working', label: 'Working', dot: 'working' },
  { key: 'done', label: 'Done', dot: 'done' },
  { key: 'idle', label: 'Idle', dot: 'idle' },
] as const

/** The `status` panel: every agent, grouped by what it needs, so the state shows without a click. */
export function StatusColumn({ groups, panes, sessions, selected, onOpen }: { groups: HomeGroups; panes: Pane[]; sessions: Session[]; selected?: string; onOpen: (pane: Pane) => void }) {
  const busy = new Set([...groups.needs, ...groups.working, ...groups.done].map(card => card.pane.id))
  const idle = panes.filter(pane => pane.agent !== 'shell' && !busy.has(pane.id)).map(pane => ({ pane, kind: 'done' as const, text: '', at: 0 }))
  const lists = { needs: groups.needs, working: groups.working, done: groups.done, idle }
  const title = (pane: Pane) => chatTitle(pane, sessions.find(item => item.id === pane.tmux_session) ?? { name: sessionName(pane) })
  return <aside className="l-panel l-status" aria-label="Agents by status">
    {STATUS_GROUPS.map(group => {
      const items = lists[group.key]
      return <section key={group.key} className={`l-status-group ${group.key}`} aria-labelledby={`l-status-${group.key}`}>
        <h2 id={`l-status-${group.key}`} className="l-panel-title"><span className={`state-dot ${group.dot}`} aria-hidden="true" />{group.label}<span className="l-count">{items.length}</span></h2>
        {items.length > 0 && <ul>{items.map(item => <li key={`${item.pane.id}:${item.kind}`}>
          <button type="button" className="l-status-row" aria-current={item.pane.id === selected ? 'true' : undefined} onClick={() => onOpen(item.pane)}>
            <AgentIcon agent={item.pane.agent} icon={item.pane.pane_icon} size={14} />
            <span className="l-status-name">{title(item.pane)}</span>
            {group.key === 'needs' && <span className="l-status-kind">{KIND_LABEL[item.kind]}</span>}
          </button>
        </li>)}</ul>}
      </section>
    })}
  </aside>
}
