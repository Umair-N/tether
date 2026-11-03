import { useCallback, useEffect, useRef, useState } from 'react'
import { Activity, X } from 'lucide-react'
import type { Pane } from './api'
import { RunningList, runningPanes, runningSummary } from './Sidebar'
import type { Session } from './Sidebar'

// A floating button that answers "which agents run now?" from any screen. Drag it anywhere; on release it moves to the
// nearer side edge, and the place is kept on this device. Tap: a popover lists the agents that work or need you.
const SIZE = 52
const EDGE = 12
const KEY = 'tether.agent-bubble.v1'
type Place = { side: 'left' | 'right'; y: number }  // y: share (0..1) of the free height, so a rotate or resize keeps it on screen

const loadPlace = (): Place => {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) ?? 'null') as Place | null
    if (saved && (saved.side === 'left' || saved.side === 'right') && saved.y >= 0 && saved.y <= 1) return saved
  } catch { /* storage blocked */ }
  return { side: 'right', y: 0.45 }
}
const savePlace = (place: Place) => { try { localStorage.setItem(KEY, JSON.stringify(place)) } catch { /* storage blocked */ } }

// Free area: inside the safe area, and above the phone tab bar when it shows.
function bounds() {
  const tabs = document.querySelector<HTMLElement>('.phone-tabbar')
  const bottom = tabs ? tabs.getBoundingClientRect().top : window.innerHeight
  return { left: EDGE, right: window.innerWidth - SIZE - EDGE, top: EDGE + 48, bottom: Math.max(EDGE + 48, bottom - SIZE - EDGE) }
}
const toPoint = (place: Place) => { const b = bounds(); return { x: place.side === 'left' ? b.left : b.right, y: b.top + place.y * (b.bottom - b.top) } }

export function AgentBubble({ sessions, selected, onOpen, onHide }: { sessions: Session[]; selected?: string; onOpen: (pane: Pane) => void; onHide: () => void }) {
  const [place, setPlace] = useState(loadPlace)
  const [point, setPoint] = useState(() => toPoint(place))
  const [dragging, setDragging] = useState(false)
  const [open, setOpen] = useState(false)
  const [closing, setClosing] = useState(false)
  const drag = useRef<{ id: number; x: number; y: number; dx: number; dy: number; moved: boolean } | null>(null)
  const dragged = useRef(false)  // the click after a drag must not open the popover
  const button = useRef<HTMLButtonElement>(null)
  const pop = useRef<HTMLDivElement>(null)

  const running = runningPanes(sessions)
  const waiting = running.filter(item => item.pane.state !== 'Working').length
  const working = running.length - waiting
  const tone = waiting ? 'attention' : working ? 'working' : 'idle'

  useEffect(() => {
    const fit = () => setPoint(toPoint(place))
    fit()
    window.addEventListener('resize', fit)
    return () => window.removeEventListener('resize', fit)
  }, [place])

  const close = useCallback((focus = true) => {
    if (!open || closing) return
    setClosing(true)
    window.setTimeout(() => { setOpen(false); setClosing(false); if (focus) button.current?.focus() }, 140)
  }, [open, closing])

  useEffect(() => {
    if (!open) return
    pop.current?.focus()
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.stopPropagation(); close() } }
    const outside = (event: PointerEvent) => { const target = event.target as Node; if (!pop.current?.contains(target) && !button.current?.contains(target)) close(false) }
    window.addEventListener('keydown', key, true)
    window.addEventListener('pointerdown', outside, true)
    return () => { window.removeEventListener('keydown', key, true); window.removeEventListener('pointerdown', outside, true) }
  }, [open, close])

  const down = (event: React.PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return
    try { event.currentTarget.setPointerCapture(event.pointerId) } catch { /* pointer already gone */ }
    dragged.current = false
    drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, dx: event.clientX - point.x, dy: event.clientY - point.y, moved: false }
  }
  const move = (event: React.PointerEvent<HTMLButtonElement>) => {
    const d = drag.current
    if (!d || d.id !== event.pointerId) return
    if (!d.moved && Math.hypot(event.clientX - d.x, event.clientY - d.y) < 6) return
    if (!d.moved) { d.moved = true; setDragging(true); if (open) close(false) }
    setPoint({ x: Math.min(Math.max(event.clientX - d.dx, 0), window.innerWidth - SIZE), y: Math.min(Math.max(event.clientY - d.dy, 0), window.innerHeight - SIZE) })
  }
  const up = (event: React.PointerEvent<HTMLButtonElement>) => {
    const d = drag.current
    drag.current = null
    if (!d || d.id !== event.pointerId) return
    if (!d.moved) return  // a tap: onClick toggles the popover
    setDragging(false)
    dragged.current = true
    const b = bounds()
    const y = Math.min(Math.max(event.clientY - d.dy, b.top), b.bottom)
    const next: Place = { side: event.clientX < window.innerWidth / 2 ? 'left' : 'right', y: b.bottom > b.top ? (y - b.top) / (b.bottom - b.top) : 0 }
    setPlace(next)
    savePlace(next)
  }
  const click = (event: React.MouseEvent) => {
    if (dragging || dragged.current) { dragged.current = false; event.preventDefault(); return }
    if (open) close(); else setOpen(true)
  }

  // The popover opens on the bubble's side, above it in the lower half of the screen and below it in the upper half.
  const above = point.y + SIZE / 2 > window.innerHeight / 2
  const width = Math.min(340, window.innerWidth - 2 * EDGE)
  const left = place.side === 'left' ? EDGE : window.innerWidth - width - EDGE
  const popStyle: React.CSSProperties = {
    left, width,
    ...(above ? { bottom: window.innerHeight - point.y + 10 } : { top: point.y + SIZE + 10 }),
    maxHeight: above ? point.y - 10 - EDGE : window.innerHeight - point.y - SIZE - 10 - EDGE,
    transformOrigin: `${place.side === 'left' ? 'left' : 'right'} ${above ? 'bottom' : 'top'}`,
  }
  const summary = runningSummary(running)
  const label = `Agents: ${summary}`

  return <>
    <button ref={button} type="button" className={`agent-bubble ${tone} ${dragging ? 'dragging' : ''} ${open ? 'open' : ''}`} style={{ transform: `translate3d(${point.x}px, ${point.y}px, 0)` }}
      aria-label={label} title={label} aria-haspopup="dialog" aria-expanded={open}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up} onClick={click}>
      <span className="agent-bubble-ring" aria-hidden="true" />
      {running.length ? <span className="agent-bubble-count" aria-hidden="true">{running.length}</span> : <Activity size={20} aria-hidden="true" />}
    </button>
    {open && <div ref={pop} className={`agent-pop ${closing ? 'closing' : ''}`} style={popStyle} role="dialog" aria-label="Agents" tabIndex={-1}>
      <header className="agent-pop-head">
        <span><strong>Agents</strong><small>{summary}</small></span>
        <button type="button" className="icon-button" aria-label="Close agents" onClick={() => close()}><X size={18} /></button>
      </header>
      <div className="agent-pop-list">
        {running.length
          ? <RunningList running={running} selected={selected} onOpen={pane => { close(false); onOpen(pane) }} />
          : <p className="group-empty">No agent is working. All are idle.</p>}
      </div>
      <footer className="agent-pop-foot">
        <button type="button" className="agent-pop-hide" onClick={() => { close(false); onHide() }}>Hide bubble</button>
        <small>Turn it back on in Settings › Chat.</small>
      </footer>
    </div>}
  </>
}
