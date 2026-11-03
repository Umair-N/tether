import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { Inbox as InboxIcon, X } from 'lucide-react'
import { ApiError, errorText } from './api'
import type { Api, Approval, Choice, Pane } from './api'
import { AgentIcon, agentNames, paneTitle } from './Sidebar'
import type { Session } from './Sidebar'
import { KIND_LABEL, MOD, age } from './needs'
import type { InboxItem } from './needs'

// Desktop "Needs you" list: every pane, in every session, that waits on you. Newest first.
// Permission requests and menus can be answered here, so the slots do not move.

const rowTitle = (pane: Pane, sessions: Session[]) => {
  const session = sessions.find(item => item.id === pane.tmux_session)
  if (!session) return paneTitle(pane)
  return session.panes.length > 1 ? `${session.name} · ${paneTitle(pane)}` : session.name
}

interface InboxProps {
  api: Api
  items: InboxItem[]
  sessions: Session[]
  selected?: string
  open: boolean
  onToggle: () => void
  onOpen: (pane: Pane) => void
  onUpdate: () => void
}

/** The button in the workspace bar that shows and hides the list. */
export function InboxButton({ count, open, onToggle }: { count: number; open: boolean; onToggle: () => void }) {
  const label = `Needs you${count ? `, ${count}` : ''} (${MOD}I)`
  return <button type="button" className="icon-button inbox-toggle" aria-label={label} title={label} aria-pressed={open} onClick={onToggle}>
    <InboxIcon size={17} />{count > 0 && <span className="inbox-badge" aria-hidden="true">{count > 99 ? '99+' : count}</span>}
  </button>
}

const focusRow = (id: string) => document.querySelector<HTMLButtonElement>(`.inbox [data-pane="${CSS.escape(id)}"] .inbox-open`)?.focus()

export function Inbox({ api, items, sessions, selected, open, onToggle, onOpen, onUpdate }: InboxProps) {
  const [busy, setBusy] = useState<string | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => { const timer = setInterval(() => setNow(Date.now() / 1000), 30000); return () => clearInterval(timer) }, [])
  // After an answer, focus moves to the row that was below it, so Y, N and the digits keep working down the list.
  const refocus = useRef<string | null>(null)
  const ids = items.map(item => item.pane.id).join(',')
  useEffect(() => {
    setErrors(current => { const kept = Object.entries(current).filter(([id]) => ids.split(',').includes(id)); return kept.length === Object.keys(current).length ? current : Object.fromEntries(kept) })
    const id = refocus.current
    if (!id || !open) return
    if (!document.activeElement || document.activeElement === document.body || !document.activeElement.isConnected) focusRow(id)
    refocus.current = null
  }, [ids, open])

  // ⌘I (Ctrl+I) shows or hides the list. ⌘J (Ctrl+J) opens the next pane that needs you. Letters are read from event.code.
  const keys = useRef({ items, selected, open, onToggle, onOpen })
  useEffect(() => { keys.current = { items, selected, open, onToggle, onOpen } })
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey || document.querySelector('dialog[open]')) return
      const current = keys.current
      if (event.code === 'KeyI') { event.preventDefault(); current.onToggle() }
      else if (event.code === 'KeyJ') {
        event.preventDefault()
        const list = current.items
        if (!list.length) return
        const at = list.findIndex(item => item.pane.id === current.selected)
        const next = list[(at + 1) % list.length]
        current.onOpen(next.pane)
        if (current.open) requestAnimationFrame(() => focusRow(next.pane.id))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const answer = async (item: InboxItem, option: Choice) => {
    const { pane } = item
    if (busy) return
    const at = items.indexOf(item)
    refocus.current = (items[at + 1] ?? items[at - 1])?.pane.id ?? null
    setBusy(pane.id)
    setErrors(current => Object.fromEntries(Object.entries(current).filter(([id]) => id !== pane.id)))
    try {
      const approval = pane.approval as Approval | null
      if (item.kind === 'approval' && approval?.id) await api('approval', { session: pane.id, thread: pane.thread, approval: approval.id, choice: option.id })
      else await api('menu', { session: pane.id, thread: pane.thread, choice: option.id, label: option.text })
      onUpdate()
    } catch (error) {
      refocus.current = null
      const text = error instanceof ApiError && error.status === 409 ? 'This pane changed. Open it to check.' : errorText(error)
      setErrors(current => ({ ...current, [pane.id]: text }))
      onUpdate()
    } finally { setBusy(null) }
  }

  // Y, N and 1-3 answer the row that has focus. Arrow keys move between rows.
  const onRowKey = (event: ReactKeyboardEvent<HTMLLIElement>, item: InboxItem) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return
    const rows = [...event.currentTarget.parentElement!.querySelectorAll<HTMLButtonElement>('.inbox-open')]
    const at = rows.indexOf(event.currentTarget.querySelector('.inbox-open')!)
    let option: Choice | undefined
    if (event.code === 'ArrowDown' || event.code === 'ArrowUp') {
      event.preventDefault(); event.stopPropagation()
      rows[Math.min(rows.length - 1, Math.max(0, at + (event.code === 'ArrowDown' ? 1 : -1)))]?.focus()
      return
    }
    if (event.shiftKey) return
    if (event.code === 'KeyY') option = item.yes
    else if (event.code === 'KeyN') option = item.no
    else { const digit = /^(?:Digit|Numpad)([1-3])$/.exec(event.code); if (digit) option = item.menu?.find(choice => choice.id === Number(digit[1])) }
    // Keep the key out of the message box even when this row has no such answer.
    if (/^Key[YN]$|^(?:Digit|Numpad)[1-3]$/.test(event.code)) { event.preventDefault(); event.stopPropagation() }
    if (option) void answer(item, option)
  }

  if (!open) return null
  return <aside className="inbox" aria-labelledby="inbox-title">
    <div className="inbox-head">
      <h2 id="inbox-title">Needs you</h2>
      <span className="inbox-count">{items.length || ''}</span>
      <span className="workspace-spacer" />
      <button type="button" className="inbox-next" disabled={!items.length} title={`Open the next pane that needs you (${MOD}J)`}
        onClick={() => { const at = items.findIndex(item => item.pane.id === selected); const next = items[(at + 1) % items.length]; if (next) onOpen(next.pane) }}>Next <kbd>{MOD}J</kbd></button>
      <button type="button" className="icon-button" aria-label={`Hide Needs you (${MOD}I)`} title={`Hide (${MOD}I)`} onClick={onToggle}><X size={16} /></button>
    </div>
    {items.length ? <ul className="inbox-list" aria-label="Panes that need you">
      {items.map(item => {
        const { pane } = item
        const title = rowTitle(pane, sessions)
        const waiting = busy === pane.id
        return <li key={pane.id} data-pane={pane.id} className={`inbox-row ${item.kind}`} aria-current={pane.id === selected ? 'true' : undefined} onKeyDown={event => onRowKey(event, item)}>
          <button type="button" className="inbox-open" onClick={() => onOpen(pane)}
            aria-label={`${KIND_LABEL[item.kind]}: ${title}, ${agentNames[pane.agent]}. ${item.text}. ${age(item.at, now)}.`}>
            <span className="inbox-kind"><span className="inbox-dot" aria-hidden="true" />{KIND_LABEL[item.kind]}<span className="inbox-age">{age(item.at, now)}</span></span>
            <span className="inbox-title">{title}</span>
            <span className="inbox-meta"><AgentIcon agent={pane.agent} size={13} />{agentNames[pane.agent]}</span>
            {item.text && <span className="inbox-text" title={item.text}>{item.text}</span>}
          </button>
          {(item.yes || item.no || item.menu) && <div className="inbox-actions" role="group" aria-label={`Answer ${title}`}>
            {item.yes && <button type="button" className="inbox-yes" disabled={waiting} title={item.yes.text} onClick={() => void answer(item, item.yes!)}>Yes <kbd>Y</kbd></button>}
            {item.no && <button type="button" disabled={waiting} title={item.no.text} onClick={() => void answer(item, item.no!)}>No <kbd>N</kbd></button>}
            {item.menu?.map(option => <button type="button" key={option.id} disabled={waiting} title={option.text} onClick={() => void answer(item, option)}><kbd>{option.id}</kbd><span>{option.text}</span></button>)}
          </div>}
          {errors[pane.id] && <p className="inbox-error" role="alert">{errors[pane.id]}</p>}
        </li>
      })}
    </ul> : <p className="inbox-empty">Nothing needs you.</p>}
  </aside>
}
