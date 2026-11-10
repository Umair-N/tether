import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { KeyboardEvent, PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { GripHorizontal, PanelRightOpen, X } from 'lucide-react'
import { BrowserPanel } from './BrowserPanel'
import type { Api, Pane } from './api'
import type { PanelBodyProps } from './panels'
import './BrowserWindow.css'

const FLOAT_KEY = 'tether-browser-floating'
const RECT_KEY = 'tether-browser-window'
const MIN = { width: 360, height: 320 }
type Rect = { x: number; y: number; width: number; height: number }

function load<T>(key: string, fallback: T): T {
  try { const text = localStorage.getItem(key); return text ? JSON.parse(text) as T : fallback } catch { return fallback }
}
function save(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* private mode */ }
}
// Keep the whole window on screen, so its title bar and resize corner stay in reach.
function clamp(rect: Rect): Rect {
  const width = Math.min(Math.max(MIN.width, rect.width), innerWidth - 16)
  const height = Math.min(Math.max(MIN.height, rect.height), innerHeight - 16)
  return { width, height, x: Math.min(Math.max(8, rect.x), innerWidth - width - 8), y: Math.min(Math.max(8, rect.y), innerHeight - height - 8) }
}
const defaultRect = (): Rect => clamp({ width: 480, height: Math.min(760, innerHeight - 96), x: innerWidth - 480 - 24, y: 72 })

// One floating flag for the app. The window is drawn by the app (BrowserFloat), not by the panel, so closing the side panel keeps it open.
let floatingNow = load(FLOAT_KEY, false)
const listeners = new Set<() => void>()
function setFloating(value: boolean) {
  floatingNow = value
  save(FLOAT_KEY, value)
  listeners.forEach(listener => listener())
}
const useFloating = () => useSyncExternalStore(listener => { listeners.add(listener); return () => { listeners.delete(listener) } }, () => floatingNow)

/** The Browser panel body: the browser, or a note when it is popped out into its own window. */
export function BrowserDock(props: PanelBodyProps) {
  const floating = useFloating()
  if (!floating) return <BrowserPanel {...props} onPopOut={() => setFloating(true)} />
  return <div className="panel-empty browser-away">
    <p>The browser is in its own window.</p>
    <button type="button" className="browser-text-button" onClick={() => setFloating(false)}>Put it back here</button>
  </div>
}

/** The popped-out browser window. The app mounts it once, beside the workspace.
 * onDock: open the side Browser panel if it is closed. onClose: close the side Browser panel if it is open. */
export function BrowserFloat({ api, pane, onDock, onClose }: { api: Api; pane: Pane; onDock: () => void; onClose: () => void }) {
  const floating = useFloating()
  if (!floating) return null
  return createPortal(<BrowserWindow onDock={() => { setFloating(false); onDock() }} onClose={() => { setFloating(false); onClose() }}>
    <BrowserPanel api={api} pane={pane} folder={pane.dir || null} active />
  </BrowserWindow>, document.body)
}

function BrowserWindow({ onDock, onClose, children }: { onDock: () => void; onClose: () => void; children: ReactNode }) {
  const [rect, setRect] = useState<Rect>(() => clamp(load(RECT_KEY, defaultRect())))
  const windowRef = useRef<HTMLElement>(null)
  const frame = useRef(0)
  const rectRef = useRef(rect)
  useEffect(() => { rectRef.current = rect }, [rect])
  useEffect(() => () => cancelAnimationFrame(frame.current), [])
  const drag = useRef<{ kind: 'move' | 'size'; x: number; y: number; start: Rect } | null>(null)
  useEffect(() => {
    const onResize = () => setRect(old => clamp(old))
    addEventListener('resize', onResize)
    return () => removeEventListener('resize', onResize)
  }, [])

  const start = (kind: 'move' | 'size', event: ReactPointerEvent) => {
    if (event.button !== 0 || (event.target as HTMLElement).closest('button:not(.browser-window-grip)')) return
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    const el = windowRef.current
    if (el) {
      const matrix = new DOMMatrix(getComputedStyle(el).transform)
      rectRef.current = { ...rectRef.current, x: matrix.m41, y: matrix.m42 }
      el.style.transition = 'none'
      el.style.transform = `translate3d(${matrix.m41}px, ${matrix.m42}px, 0)`
    }
    drag.current = { kind, x: event.clientX, y: event.clientY, start: rectRef.current }
  }
  const move = (event: ReactPointerEvent) => {
    const now = drag.current
    if (!now) return
    const dx = event.clientX - now.x, dy = event.clientY - now.y
    const { start } = now
    const raw = now.kind === 'move' ? { ...start, x: start.x + dx, y: start.y + dy }
      : { ...start, width: start.width + dx, height: start.height + dy }
    const bounded = clamp(raw)
    const elastic = (value: number, edge: number) => edge + (value - edge) * 0.25
    rectRef.current = now.kind === 'move' ? { ...start, x: elastic(raw.x, bounded.x), y: elastic(raw.y, bounded.y) }
      : { ...start, width: elastic(raw.width, Math.min(bounded.width, innerWidth - start.x - 8)), height: elastic(raw.height, Math.min(bounded.height, innerHeight - start.y - 8)) }
    if (!frame.current) frame.current = requestAnimationFrame(() => {
      frame.current = 0
      const el = windowRef.current, next = rectRef.current
      if (el) { el.style.transform = `translate3d(${next.x}px, ${next.y}px, 0)`; el.style.width = `${next.width}px`; el.style.height = `${next.height}px` }
    })
  }
  const end = () => {
    if (!drag.current) return
    cancelAnimationFrame(frame.current); frame.current = 0
    const current = rectRef.current
    const next = clamp(drag.current.kind === 'size' ? { ...current, width: Math.min(current.width, innerWidth - current.x - 8), height: Math.min(current.height, innerHeight - current.y - 8) } : current)
    drag.current = null
    if (windowRef.current) {
      windowRef.current.style.transition = `transform ${matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 200}ms cubic-bezier(.32,.72,0,1)`
      windowRef.current.style.transform = `translate3d(${next.x}px, ${next.y}px, 0)`
      windowRef.current.style.width = `${next.width}px`; windowRef.current.style.height = `${next.height}px`
    }
    rectRef.current = next; setRect(next); save(RECT_KEY, next)
  }
  // Keyboard: arrows move the window from its grip; with Shift they resize it.
  const onKey = (event: KeyboardEvent) => {
    const step = { ArrowLeft: [-16, 0], ArrowRight: [16, 0], ArrowUp: [0, -16], ArrowDown: [0, 16] }[event.key]
    if (!step) return
    event.preventDefault()
    if (windowRef.current) windowRef.current.style.transition = 'none'
    const old = rectRef.current
    const next = clamp(event.shiftKey ? { ...old, width: old.width + step[0], height: old.height + step[1] } : { ...old, x: old.x + step[0], y: old.y + step[1] })
    setRect(next); save(RECT_KEY, next)
  }

  return <section ref={windowRef} className="browser-window" role="dialog" aria-label="Browser" style={{ left: 0, top: 0, transform: `translate3d(${rect.x}px, ${rect.y}px, 0)`, width: rect.width, height: rect.height }}>
    <header className="browser-window-head" onPointerDown={event => start('move', event)} onPointerMove={move} onPointerUp={end} onPointerCancel={end}>
      <button type="button" className="browser-window-grip" aria-label="Move the browser window. Arrow keys move it, Shift with arrows resizes it." onKeyDown={onKey}><GripHorizontal size={16} /></button>
      <strong>Browser</strong>
      <button type="button" className="browser-icon" aria-label="Put the browser back in the side column" title="Put back in the side column" onClick={onDock}><PanelRightOpen size={17} /></button>
      <button type="button" className="browser-icon" aria-label="Close the browser window" title="Close" onClick={onClose}><X size={17} /></button>
    </header>
    {children}
    <div className="browser-window-size" aria-hidden="true" onPointerDown={event => start('size', event)} onPointerMove={move} onPointerUp={end} onPointerCancel={end} />
  </section>
}
