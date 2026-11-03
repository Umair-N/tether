import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, GripVertical, Pin, PinOff, Plus, Search, X } from 'lucide-react'
import type { Pane } from './api'
import type { SidePin } from './prefs'
import { agentNames, agoSeconds, dot, needsYou, paneTitle, status, unreadPanes } from './Sidebar'
import type { Session } from './Sidebar'
import { Elapsed } from './Notices'
import { useDockedBubble } from './dockedBubble'
import './SidePins.css'

type PinnedChat = { pin: SidePin; pane: Pane; session: Session }
interface Props {
  pins: SidePin[]
  sessions: Session[]
  seen: Record<string, number>
  selected?: string
  onOpen: (pane: Pane) => void
  onPin: (pane: string) => void
  onUnpin: (pane: string) => void
  onNote: (pane: string, note: string) => void
  onMove: (pane: string, to: string) => void
}

// The row leaves space beside its open button for a trailing action.
function PinnedRow({ pin, pane, session, seen, selected, onOpen }: PinnedChat & Pick<Props, 'seen' | 'selected' | 'onOpen'>) {
  const needs = needsYou(pane.state)
  const ago = pane.finished ? agoSeconds(pane.finished) : ''
  return <li className={`side-pin-row ${pane.id === selected ? 'selected' : ''}`}>
    <button type="button" className="side-pin-open" aria-current={pane.id === selected ? 'page' : undefined} onClick={() => onOpen(pane)}>
      {dot(status([pane], seen, selected))}
      <span className="side-pin-text">
        <span className="side-pin-line"><strong>{paneTitle(pane)}</strong><small className="side-pin-time">{pane.state === 'Working' && pane.since
          ? <Elapsed since={pane.since * 1000} /> : ago ? ago === 'now' ? 'Just now' : `${ago} ago` : ''}</small></span>
        {needs ? <><small className="side-pin-needs">Needs you{pane.needs?.text ? ` · ${pane.needs.text}` : ''}</small>{pin.note && <small className="side-pin-note">{pin.note}</small>}</>
          : <small>{pin.note && <><span className="side-pin-note">{pin.note}</span> · </>}{session.name} · {agentNames[pane.agent]}</small>}
      </span>
    </button>
  </li>
}

export function SidePins({ pins, sessions, seen, selected, onOpen, onPin, onUnpin, onNote, onMove }: Props) {
  const [view, setView] = useState<'list' | 'picker' | 'edit'>('list')
  const [query, setQuery] = useState('')
  const edit = useRef<HTMLButtonElement>(null)
  const back = () => { setView('list'); requestAnimationFrame(() => edit.current?.focus()) }
  const { place, docked, dragging, open, closing, button, pop, shape, popStyle, close, down, move, up, click } = useDockedBubble('tether.pin-bubble.v1', { side: 'right', y: 0.62 }, () => {
    if (view === 'list') return false
    back()
    return true
  })
  const chats = sessions.flatMap(session => session.panes.filter(pane => pane.agent !== 'shell').map(pane => ({ pane, session })))
  const live: PinnedChat[] = pins.flatMap(pin => { const chat = chats.find(item => item.pane.id === pin.pane); return chat ? [{ ...chat, pin }] : [] })
  const waiting = live.filter(item => needsYou(item.pane.state)).length
  const unread = new Set(unreadPanes(sessions, seen, selected).map(item => item.pane.id))
  const unreadCount = live.filter(item => unread.has(item.pane.id)).length
  const rest = chats.filter(item => !pins.some(pin => pin.pane === item.pane.id))
  const search = query.trim().toLowerCase()
  const matching = rest.filter(({ pane, session }) => `${paneTitle(pane)} ${session.name} ${session.dir} ${pane.dir}`.toLowerCase().includes(search))
  const current = rest.find(item => item.pane.id === selected)
  // Pins of closed panes make room at the cap (App drops them on the next pin).
  const full = live.length >= 40
  const label = `Pinned chats: ${live.length}${waiting ? `, ${waiting} needs you` : ''}${unreadCount ? `, ${unreadCount} unread` : ''}`
  const input = useRef<HTMLInputElement>(null)
  const list = useRef<HTMLUListElement>(null)
  const drag = useRef<{ id: number; pane: string } | null>(null)
  const [moving, setMoving] = useState<string | null>(null)

  useEffect(() => { if (open && view === 'picker' && matchMedia('(pointer: fine)').matches) input.current?.focus() }, [open, view])
  const gripDown = (event: React.PointerEvent<HTMLButtonElement>, pane: string) => {
    if (event.button !== 0) return
    event.preventDefault(); event.stopPropagation()
    event.currentTarget.focus()
    list.current?.setPointerCapture(event.pointerId)
    drag.current = { id: event.pointerId, pane }
    setMoving(pane)
  }
  const gripMove = (event: React.PointerEvent<HTMLUListElement>) => {
    const held = drag.current
    if (!held || held.id !== event.pointerId || !list.current) return
    event.preventDefault(); event.stopPropagation()
    const rows = [...list.current.querySelectorAll<HTMLElement>('[data-pin]')]
    const target = rows.find(row => { const box = row.getBoundingClientRect(); return event.clientY >= box.top && event.clientY <= box.bottom }) ?? (event.clientY < list.current.getBoundingClientRect().top ? rows[0] : rows[rows.length - 1])
    if (target?.dataset.pin && target.dataset.pin !== held.pane) onMove(held.pane, target.dataset.pin)
    const box = list.current.getBoundingClientRect()
    if (event.clientY < box.top + 24) list.current.scrollTop -= 12
    else if (event.clientY > box.bottom - 24) list.current.scrollTop += 12
  }
  const gripUp = (event: React.PointerEvent<HTMLUListElement>) => {
    if (drag.current?.id !== event.pointerId) return
    event.stopPropagation()
    drag.current = null
    setMoving(null)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }

  return <>
    <button ref={button} type="button" className={`agent-bubble side-pin-bubble ${waiting ? 'attention' : unreadCount ? 'unread' : ''} ${place.side} ${docked ? 'docked' : ''} ${dragging ? 'dragging' : ''} ${open ? 'open' : ''}`} style={shape}
      aria-label={label} title={label} aria-haspopup="dialog" aria-expanded={open}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up} onClick={event => click(event, () => { setView('list'); setQuery('') })}>
      <span key={docked ? 'tab' : 'bubble'} className="side-pin-count" aria-hidden="true"><Pin size={docked ? 13 : 16} /><span>{waiting || live.length}</span></span>
    </button>
    {open && <div ref={pop} className={`agent-pop side-pin-pop ${closing ? 'closing' : ''}`} style={popStyle} role="dialog" aria-label="Pinned chats" tabIndex={-1}>
      <header className="agent-pop-head side-pin-head">
        {view === 'picker' && <button type="button" className="icon-button" aria-label="Back" onClick={() => { back(); pop.current?.focus() }}><ArrowLeft size={18} /></button>}
        <span><strong>{view === 'picker' ? 'Pin a chat' : 'Pinned'}</strong><small>{view === 'edit' ? 'Drag to reorder. Notes show in the list and the chat.' : view === 'picker' ? `${rest.length} not pinned`
          : waiting || unreadCount ? <>{waiting > 0 && <span className="side-pin-needs">{waiting} needs you</span>}{waiting > 0 && unreadCount > 0 && ' · '}{unreadCount > 0 && `${unreadCount} unread`}</> : `${live.length} chats`}</small></span>
        {view !== 'picker' && <button ref={edit} type="button" className="side-pin-edit" onClick={() => setView(view === 'edit' ? 'list' : 'edit')}>{view === 'edit' ? 'Done' : 'Edit'}</button>}
        <button type="button" className="icon-button" aria-label="Close pinned chats" onClick={() => close()}><X size={18} /></button>
      </header>
      {view === 'picker' && <label className="side-pin-search"><Search size={16} /><input ref={input} aria-label="Search chats to pin" placeholder="Search chats" value={query} onChange={event => setQuery(event.target.value)} /></label>}
      <ul ref={list} className="agent-pop-list side-pin-list" onPointerMove={gripMove} onPointerUp={gripUp} onPointerCancel={gripUp} onLostPointerCapture={gripUp}>
        {view === 'picker' ? matching.map(({ pane, session }) => <li key={pane.id} className="side-pin-picker-row">
          {dot(status([pane], seen, selected))}<span className="side-pin-text"><strong>{paneTitle(pane)}</strong><small>{session.name} · {agentNames[pane.agent]}</small></span>
          <button type="button" className="icon-button" aria-label={`Pin ${paneTitle(pane)}`} disabled={full} title={full ? 'Up to 40 chats can be pinned.' : undefined} onClick={() => onPin(pane.id)}><Plus size={18} /></button>
        </li>) : view === 'edit' ? live.map(({ pane, pin }) => <li key={pane.id} data-pin={pane.id} className={`side-pin-edit-row ${moving === pane.id ? 'moving' : ''}`}>
          <button type="button" className="side-pin-grip" aria-label={`Move ${paneTitle(pane)}. Alt+Up or Alt+Down.`} onPointerDown={event => gripDown(event, pane.id)}
            onKeyDown={event => {
              if (!event.altKey || (event.key !== 'ArrowUp' && event.key !== 'ArrowDown')) return
              event.stopPropagation(); event.preventDefault()
              const at = live.findIndex(item => item.pane.id === pane.id), target = live[at + (event.key === 'ArrowUp' ? -1 : 1)]
              if (target) onMove(pane.id, target.pane.id)
            }}><GripVertical size={14} /></button>
          <span className="side-pin-edit-body"><strong>{paneTitle(pane)}</strong><input maxLength={60} placeholder="Add a note" aria-label={`Note for ${paneTitle(pane)}`} value={pin.note ?? ''} onChange={event => onNote(pane.id, event.target.value)} onBlur={event => onNote(pane.id, event.target.value.trim())} /></span>
          <button type="button" className="icon-button" aria-label={`Unpin ${paneTitle(pane)}`} onClick={() => { if (live.length < 2) close(false); onUnpin(pane.id) }}><PinOff size={16} /></button>
        </li>) : live.map(chat => <PinnedRow key={chat.pane.id} {...chat} seen={seen} selected={selected} onOpen={pane => { onOpen(pane); close(false) }} />)}
        {view === 'picker' && !matching.length && <li className="side-pin-empty">No other chats match.</li>}
      </ul>
      {view === 'list' && <footer className="agent-pop-foot side-pin-foot">
        <button type="button" onClick={() => { setQuery(''); setView('picker') }}><Plus size={16} />Pin a chat</button>
        {current && <button type="button" disabled={full} title={full ? 'Up to 40 chats can be pinned.' : undefined} onClick={() => onPin(current.pane.id)}><Pin size={16} />Pin this chat</button>}
      </footer>}
    </div>}
  </>
}
