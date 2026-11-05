import { useCallback, useEffect, useRef, useState } from 'react'
import { useBackStep } from './backNav'

const SIZE = 52
const TAB_W = 26
const TAB_H = 48
const EDGE = 12
export type Place = { side: 'left' | 'right'; y: number }  // y: share (0..1) of the free height, so a rotate or resize keeps it on screen

const loadPlace = (storageKey: string, fallback: Place): Place => {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) ?? 'null') as Place | null
    if (saved && (saved.side === 'left' || saved.side === 'right') && saved.y >= 0 && saved.y <= 1) return saved
  } catch { /* storage blocked */ }
  return fallback
}
const savePlace = (storageKey: string, place: Place) => { try { localStorage.setItem(storageKey, JSON.stringify(place)) } catch { /* storage blocked */ } }

// Free area: inside the safe area, and above the phone tab bar.
// A focused text box hides the tab bar (display: none, top 0). Keep its last height, so the keyboard
// does not throw the bubble to the top and leave it there.
let tabbarHeight = 0
function bounds() {
  const tabs = document.querySelector<HTMLElement>('.phone-tabbar')
  if (tabs && tabs.offsetHeight) tabbarHeight = tabs.offsetHeight
  const bottom = window.innerHeight - (tabs ? tabbarHeight : 0)
  return { left: EDGE, right: window.innerWidth - SIZE - EDGE, top: EDGE + 48, bottom: Math.max(EDGE + 48, bottom - SIZE - EDGE) }
}
const toPoint = (place: Place) => { const b = bounds(); return { x: place.side === 'left' ? b.left : b.right, y: b.top + place.y * (b.bottom - b.top) } }

export function useDockedBubble(storageKey: string, fallback: Place, onEscape?: () => boolean) {
  const [place, setPlace] = useState(() => loadPlace(storageKey, fallback))
  const [point, setPoint] = useState(() => toPoint(place))
  const [dragging, setDragging] = useState(false)
  const [open, setOpen] = useState(false)
  const [closing, setClosing] = useState(false)
  const drag = useRef<{ id: number; x: number; y: number; dx: number; dy: number; moved: boolean } | null>(null)
  const dragged = useRef(false)  // the click after a drag must not open the popover
  const button = useRef<HTMLButtonElement>(null)
  const pop = useRef<HTMLDivElement>(null)

  const timer = useRef(0)
  const escape = useRef(onEscape)
  useEffect(() => { escape.current = onEscape })
  const docked = !open && !dragging

  useEffect(() => {
    const fit = () => setPoint(toPoint(place))
    fit()
    window.addEventListener('resize', fit)
    return () => window.removeEventListener('resize', fit)
  }, [place])

  const close = useCallback((focus = true) => {
    if (!open || closing) return
    setClosing(true)
    timer.current = window.setTimeout(() => { setOpen(false); setClosing(false); if (focus) button.current?.focus() }, 140)
  }, [open, closing])
  useBackStep(open, close)

  useEffect(() => {
    if (!open) return
    pop.current?.focus()
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { if (escape.current) event.preventDefault(); event.stopPropagation(); if (!escape.current?.()) close() } }
    const outside = (event: PointerEvent) => { const target = event.target as Node; if (!pop.current?.contains(target) && !button.current?.contains(target)) close(false) }
    window.addEventListener('keydown', key, true)
    window.addEventListener('pointerdown', outside, true)
    return () => { window.removeEventListener('keydown', key, true); window.removeEventListener('pointerdown', outside, true) }
  }, [open, close])

  const down = (event: React.PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return
    try { event.currentTarget.setPointerCapture(event.pointerId) } catch { /* pointer already gone */ }
    dragged.current = false
    // From the tab, the bubble centres under the finger; from the open bubble, it keeps its offset.
    drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, dx: docked ? SIZE / 2 : event.clientX - point.x, dy: docked ? SIZE / 2 : event.clientY - point.y, moved: false }
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
    savePlace(storageKey, next)
  }
  const click = (event: React.MouseEvent, beforeOpen?: () => void) => {
    if (dragging || dragged.current) { dragged.current = false; event.preventDefault(); return }
    if (open) close(); else { beforeOpen?.(); window.dispatchEvent(new CustomEvent('tether:bubble-open', { detail: storageKey })); setOpen(true) }
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
  const vw = document.documentElement.clientWidth || window.innerWidth
  const shape: React.CSSProperties = docked
    ? { transform: `translate3d(${place.side === 'left' ? 0 : vw - TAB_W}px, ${point.y + (SIZE - TAB_H) / 2}px, 0)`, width: TAB_W, height: TAB_H, borderRadius: place.side === 'left' ? `0 ${TAB_H / 2}px ${TAB_H / 2}px 0` : `${TAB_H / 2}px 0 0 ${TAB_H / 2}px` }
    : { transform: `translate3d(${point.x}px, ${point.y}px, 0)`, width: SIZE, height: SIZE, borderRadius: SIZE / 2 }

  useEffect(() => {
    // A second bubble takes over without returning focus to the first one.
    const other = (event: Event) => {
      if ((event as CustomEvent<string>).detail === storageKey) return
      window.clearTimeout(timer.current)
      setOpen(false); setClosing(false)
    }
    window.addEventListener('tether:bubble-open', other)
    return () => { window.clearTimeout(timer.current); window.removeEventListener('tether:bubble-open', other) }
  }, [storageKey])

  return { point, place, docked, dragging, open, closing, button, pop, shape, popStyle, close, down, move, up, click }
}
