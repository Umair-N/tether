import { useEffect, useRef, useState } from 'react'

export type FloatMode = 'window' | 'bubble' | 'full'
export type Rect = { x: number; y: number; w: number; h: number }
export type Edge = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw'
type Handlers = Pick<React.HTMLAttributes<HTMLElement>, 'onPointerDown' | 'onPointerMove' | 'onPointerUp' | 'onPointerCancel' | 'onKeyDown'>
export type Point = { x: number; y: number }
type Options = { minW: number; minH: number; bubble: number; initial: () => Rect; initialMode?: FloatMode }
type Place = { mode: FloatMode; rect: Rect; bubble: Point }
type Drag = { id: number; target: HTMLElement; kind: 'move' | 'resize' | 'bubble'; edge?: Edge; x: number; y: number; rect: Rect; bubble: Point; moved: boolean }

const MARGIN = 8
const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), Math.max(min, max))
const bounds = () => ({ w: window.visualViewport?.width ?? window.innerWidth, h: window.visualViewport?.height ?? window.innerHeight })
const fitRect = (rect: Rect, minW: number, minH: number): Rect => {
  const b = bounds()
  const w = clamp(rect.w, minW, b.w - 2 * MARGIN)
  const h = clamp(rect.h, minH, b.h - 2 * MARGIN)
  return { x: clamp(rect.x, MARGIN, b.w - MARGIN - w), y: clamp(rect.y, MARGIN, b.h - MARGIN - h), w, h }
}
const fitBubble = (point: Point, size: number): Point => {
  const b = bounds()
  return { x: clamp(point.x, MARGIN, b.w - MARGIN - size), y: clamp(point.y, MARGIN, b.h - MARGIN - size) }
}
const isPoint = (value: unknown): value is Point => {
  if (!value || typeof value !== 'object') return false
  const p = value as Point
  return Number.isFinite(p.x) && Number.isFinite(p.y)
}
const isRect = (value: unknown): value is Rect => {
  if (!isPoint(value)) return false
  const r = value as Rect
  return Number.isFinite(r.w) && r.w > 0 && Number.isFinite(r.h) && r.h > 0
}
const isMode = (value: unknown): value is FloatMode => value === 'window' || value === 'bubble' || value === 'full'
const loadPlace = (storageKey: string, options: Options): Place => {
  const b = bounds()
  let rect = options.initial()
  let bubble = { x: b.w - MARGIN - options.bubble, y: b.h - 88 - options.bubble }
  let mode = options.initialMode ?? 'window'
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(storageKey) ?? 'null')
    if (saved && typeof saved === 'object') {
      const place = saved as Partial<Place>
      if (isRect(place.rect)) rect = place.rect
      if (isPoint(place.bubble)) bubble = place.bubble
      if (isMode(place.mode)) mode = options.initialMode ?? place.mode
    }
  } catch { /* storage blocked */ }
  return { mode, rect: fitRect(rect, options.minW, options.minH), bubble: fitBubble(bubble, options.bubble) }
}
const savePlace = (storageKey: string, place: Place) => { try { localStorage.setItem(storageKey, JSON.stringify(place)) } catch { /* storage blocked */ } }

const resizeRect = (rect: Rect, edge: Edge, dx: number, dy: number, minW: number, minH: number): Rect => {
  const b = bounds()
  let { x, y, w, h } = fitRect(rect, minW, minH)
  if (edge.includes('w')) {
    const right = x + w
    x = clamp(x + dx, MARGIN, right - minW)
    w = right - x
  }
  if (edge.includes('e')) w = clamp(w + dx, minW, b.w - MARGIN - x)
  if (edge.includes('n')) {
    const bottom = y + h
    y = clamp(y + dy, MARGIN, bottom - minH)
    h = bottom - y
  }
  if (edge.includes('s')) h = clamp(h + dy, minH, b.h - MARGIN - y)
  return fitRect({ x, y, w, h }, minW, minH)
}
const keyStep = (event: React.KeyboardEvent, shift = true): Point | null => {
  const step = shift && event.shiftKey ? 64 : 16
  switch (event.key) {
    case 'ArrowLeft': return { x: -step, y: 0 }
    case 'ArrowRight': return { x: step, y: 0 }
    case 'ArrowUp': return { x: 0, y: -step }
    case 'ArrowDown': return { x: 0, y: step }
    default: return null
  }
}

export function useFloatingWindow(storageKey: string, options: Options): {
  mode: FloatMode
  setMode: (mode: FloatMode) => void
  rect: Rect
  bubble: Point
  moving: boolean
  windowStyle: React.CSSProperties
  bubbleStyle: React.CSSProperties
  move: Handlers
  resize: (edge: Edge) => Handlers
  bubbleHandlers: Handlers & { onClick: (e: React.MouseEvent) => void }
} {
  const { minW, minH, bubble: size } = options
  const [place, setPlace] = useState(() => loadPlace(storageKey, options))
  const current = useRef(place)
  const [moving, setMoving] = useState(false)
  const drag = useRef<Drag | null>(null)
  const dragged = useRef(false)  // swallow the click after a bubble drag

  const update = (next: Place, commit = false) => {
    current.current = next
    setPlace(next)
    if (commit) savePlace(storageKey, next)
  }
  const setMode = (mode: FloatMode) => {
    const p = current.current
    update({ ...p, mode, rect: fitRect(p.rect, minW, minH), bubble: fitBubble(p.bubble, size) }, true)
  }

  useEffect(() => {
    const fit = () => {
      const p = current.current
      const next = { ...p, rect: fitRect(p.rect, minW, minH), bubble: fitBubble(p.bubble, size) }
      current.current = next
      setPlace(next)
    }
    fit()
    const viewport = window.visualViewport
    window.addEventListener('resize', fit)
    viewport?.addEventListener('resize', fit)
    return () => {
      window.removeEventListener('resize', fit)
      viewport?.removeEventListener('resize', fit)
    }
  }, [minW, minH, size])

  useEffect(() => () => {
    const d = drag.current
    if (d) {
      try { d.target.releasePointerCapture(d.id) } catch { /* pointer already gone */ }
    }
    drag.current = null
  }, [])

  const down = (event: React.PointerEvent<HTMLElement>, kind: Drag['kind'], edge?: Edge) => {
    if (event.button !== 0 || drag.current) return
    if (kind === 'move' && event.target instanceof Element) {
      const target = event.target
      if (target.closest('button, a, input, select, textarea, [role="menu"]') && !target.closest('[data-float-grip]')) return
    }
    if (kind === 'resize') { event.preventDefault(); event.stopPropagation() }
    try { event.currentTarget.setPointerCapture(event.pointerId) } catch { /* pointer already gone */ }
    if (kind === 'bubble') dragged.current = false
    const p = current.current
    drag.current = { id: event.pointerId, target: event.currentTarget, kind, edge, x: event.clientX, y: event.clientY, rect: p.rect, bubble: p.bubble, moved: kind === 'resize' }
    if (kind === 'resize') setMoving(true)
  }
  const pointerMove = (event: React.PointerEvent<HTMLElement>) => {
    const d = drag.current
    if (!d || d.id !== event.pointerId) return
    const dx = event.clientX - d.x
    const dy = event.clientY - d.y
    if (!d.moved && Math.hypot(dx, dy) < 6) return
    d.moved = true
    setMoving(true)
    const p = current.current
    if (d.kind === 'bubble') update({ ...p, bubble: fitBubble({ x: d.bubble.x + dx, y: d.bubble.y + dy }, size) })
    else if (d.kind === 'resize' && d.edge) update({ ...p, rect: resizeRect(d.rect, d.edge, dx, dy, minW, minH) })
    else update({ ...p, rect: fitRect({ ...d.rect, x: d.rect.x + dx, y: d.rect.y + dy }, minW, minH) })
  }
  const finish = (event: React.PointerEvent<HTMLElement>, cancel = false) => {
    const d = drag.current
    if (!d || d.id !== event.pointerId) return
    if (!cancel) pointerMove(event)
    drag.current = null
    try { d.target.releasePointerCapture(d.id) } catch { /* pointer already gone */ }
    setMoving(false)
    let next = current.current
    if (d.kind === 'bubble' && d.moved) {
      dragged.current = true
      const b = bounds()
      const right = Math.max(MARGIN, b.w - MARGIN - size)
      const x = next.bubble.x - MARGIN < right - next.bubble.x ? MARGIN : right
      next = { ...next, bubble: fitBubble({ ...next.bubble, x }, size) }
    }
    update(next, true)
  }
  const handlers = (kind: Drag['kind'], edge?: Edge): Handlers => ({
    onPointerDown: event => down(event, kind, edge),
    onPointerMove: pointerMove,
    onPointerUp: event => finish(event),
    onPointerCancel: event => finish(event, true),
    onKeyDown: event => {
      const step = keyStep(event, kind !== 'bubble')
      if (!step) return
      if (kind === 'resize' && (!edge || (step.x !== 0 && !/[ew]/.test(edge)) || (step.y !== 0 && !/[ns]/.test(edge)))) return
      event.preventDefault()
      const p = current.current
      if (kind === 'bubble') update({ ...p, bubble: fitBubble({ x: p.bubble.x + step.x, y: p.bubble.y + step.y }, size) }, true)
      else if (kind === 'resize' && edge) update({ ...p, rect: resizeRect(p.rect, edge, step.x, step.y, minW, minH) }, true)
      else update({ ...p, rect: fitRect({ ...p.rect, x: p.rect.x + step.x, y: p.rect.y + step.y }, minW, minH) }, true)
    },
  })

  const { mode, rect, bubble } = place
  return {
    mode, setMode, rect, bubble, moving,
    windowStyle: mode === 'full' ? {} : { transform: `translate3d(${rect.x}px, ${rect.y}px, 0)`, width: rect.w, height: rect.h },
    bubbleStyle: { transform: `translate3d(${bubble.x}px, ${bubble.y}px, 0)`, width: size, height: size },
    move: handlers('move'),
    resize: edge => handlers('resize', edge),
    bubbleHandlers: {
      ...handlers('bubble'),
      onClick: event => {
        if (moving || dragged.current) { dragged.current = false; event.preventDefault(); return }
        setMode('window')
      },
    },
  }
}
