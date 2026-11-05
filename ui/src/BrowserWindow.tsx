import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent, PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { GripHorizontal, PanelRightOpen } from 'lucide-react'
import { BrowserPanel } from './BrowserPanel'
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

/** The Browser panel body: in the column, or popped out into a window the person can move and resize. */
export function BrowserDock(props: PanelBodyProps) {
  const [floating, setFloating] = useState(() => load(FLOAT_KEY, false))
  const change = (value: boolean) => { setFloating(value); save(FLOAT_KEY, value) }
  if (!floating) return <BrowserPanel {...props} onPopOut={() => change(true)} />
  return <>
    <div className="panel-empty browser-away">
      <p>The browser is in its own window.</p>
      <button type="button" className="browser-text-button" onClick={() => change(false)}>Put it back here</button>
    </div>
    {createPortal(<BrowserWindow onDock={() => change(false)}><BrowserPanel {...props} /></BrowserWindow>, document.body)}
  </>
}

function BrowserWindow({ onDock, children }: { onDock: () => void; children: ReactNode }) {
  const [rect, setRect] = useState<Rect>(() => clamp(load(RECT_KEY, defaultRect())))
  const rectRef = useRef(rect)
  rectRef.current = rect
  const drag = useRef<{ kind: 'move' | 'size'; x: number; y: number; start: Rect } | null>(null)
  useEffect(() => {
    const onResize = () => setRect(old => clamp(old))
    addEventListener('resize', onResize)
    return () => removeEventListener('resize', onResize)
  }, [])

  const start = (kind: 'move' | 'size') => (event: ReactPointerEvent) => {
    if (event.button !== 0 || (event.target as HTMLElement).closest('button:not(.browser-window-grip)')) return
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    drag.current = { kind, x: event.clientX, y: event.clientY, start: rectRef.current }
  }
  const move = (event: ReactPointerEvent) => {
    const now = drag.current
    if (!now) return
    const dx = event.clientX - now.x, dy = event.clientY - now.y
    const { start } = now
    setRect(clamp(now.kind === 'move' ? { ...start, x: start.x + dx, y: start.y + dy }
      : { ...start, width: Math.min(start.width + dx, innerWidth - start.x - 8), height: Math.min(start.height + dy, innerHeight - start.y - 8) }))
  }
  const end = () => { if (drag.current) { drag.current = null; save(RECT_KEY, rectRef.current) } }
  // Keyboard: arrows move the window from its grip; with Shift they resize it.
  const onKey = (event: KeyboardEvent) => {
    const step = { ArrowLeft: [-16, 0], ArrowRight: [16, 0], ArrowUp: [0, -16], ArrowDown: [0, 16] }[event.key]
    if (!step) return
    event.preventDefault()
    const old = rectRef.current
    const next = clamp(event.shiftKey ? { ...old, width: old.width + step[0], height: old.height + step[1] } : { ...old, x: old.x + step[0], y: old.y + step[1] })
    setRect(next); save(RECT_KEY, next)
  }

  return <section className="browser-window" role="dialog" aria-label="Browser" style={{ left: rect.x, top: rect.y, width: rect.width, height: rect.height }}>
    <header className="browser-window-head" onPointerDown={start('move')} onPointerMove={move} onPointerUp={end} onPointerCancel={end}>
      <button type="button" className="browser-window-grip" aria-label="Move the browser window. Arrow keys move it, Shift with arrows resizes it." onKeyDown={onKey}><GripHorizontal size={16} /></button>
      <strong>Browser</strong>
      <button type="button" className="browser-icon" aria-label="Put the browser back in the side column" title="Put back in the side column" onClick={onDock}><PanelRightOpen size={17} /></button>
    </header>
    {children}
    <div className="browser-window-size" aria-hidden="true" onPointerDown={start('size')} onPointerMove={move} onPointerUp={end} onPointerCancel={end} />
  </section>
}
