import { useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, ChevronUp, ChevronDown } from 'lucide-react'
import { PANELS, closedSide, dragSize, handles, panelLabel, sizeKey, sizeVar, type Columns, type Edge, type Found, type Handle } from './panelEdges'
import './PanelHandles.css'

// Drag handles on the inner edge of every side panel, a close button on each, and a tab to open a closed layout
// panel again. The panels are found in the page (panels.ts), so every layout, Classic and future panels get them.

const STEP = 16
// By handle edge, the arrow points the way the panel closes; by reopen side, the way it opens.
const ICON: Record<Edge, typeof ChevronLeft> = { left: ChevronRight, right: ChevronLeft, top: ChevronDown, bottom: ChevronUp }

interface Props {
  grid: Columns
  closed: string[]  // closed layout panels: each gets a tab to open it again
  onSize: (key: string, size: number | null) => void
  onClose: (name: string) => void
  onOpen: (name: string) => void
}

type Box = { left: number; right: number; top: number; bottom: number }
const shellOf = () => document.querySelector<HTMLElement>('.app-shell')

function measure(grid: Columns): { handles: Handle[]; chat: Box | null; gapX: number; gapY: number } {
  const shell = shellOf(), chat = shell?.querySelector<HTMLElement>(':scope > .workspace')
  if (!shell || !chat) return { handles: [], chat: null, gapX: 0, gapY: 0 }
  const found: Found[] = []
  for (const child of Array.from(shell.children) as HTMLElement[]) {
    const name = child.dataset.area || Object.keys(PANELS).find(key => child.matches(PANELS[key]))
    if (!name) continue
    const rect = child.getBoundingClientRect()
    if (rect.width < 24 || rect.height < 24 || getComputedStyle(child).display === 'none') continue
    found.push({ name, rect: { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom } })
  }
  const box = chat.getBoundingClientRect(), style = getComputedStyle(shell)
  return { handles: handles(found, box, grid), chat: { left: box.left, right: box.right, top: box.top, bottom: box.bottom }, gapX: parseFloat(style.columnGap) || 0, gapY: parseFloat(style.rowGap) || 0 }
}

export function PanelHandles({ grid, closed, onSize, onClose, onOpen }: Props) {
  const [shown, setShown] = useState(() => measure(grid))
  const gridRef = useRef(grid)
  useEffect(() => { gridRef.current = grid })
  const [drag, setDrag] = useState<string | null>(null)
  const start = useRef<{ x: number; y: number; handle: Handle; size: number } | null>(null)

  useEffect(() => {
    const shell = shellOf()
    if (!shell) return
    let frame = 0
    const later = () => {
      if (frame) return
      frame = requestAnimationFrame(() => { frame = 0; const next = measure(gridRef.current); setShown(current => JSON.stringify(current) === JSON.stringify(next) ? current : next) })
    }
    const sizes = new ResizeObserver(later)
    const watch = () => { sizes.disconnect(); for (const child of Array.from(shell.children)) sizes.observe(child); later() }
    const changes = new MutationObserver(watch)
    changes.observe(shell, { childList: true, attributes: true, attributeFilter: ['style', 'class', 'data-grid', 'data-layout'] })
    window.addEventListener('resize', later)
    watch()
    return () => { cancelAnimationFrame(frame); sizes.disconnect(); changes.disconnect(); window.removeEventListener('resize', later) }
  }, [])

  const setLive = (key: string, size: number | null) => {
    const shell = shellOf()
    if (!shell) return
    if (size === null) shell.style.removeProperty(sizeVar(key)); else shell.style.setProperty(sizeVar(key), `${size}px`)
  }
  const commit = (key: string, size: number | null) => { setLive(key, size); onSize(key, size) }
  const room = () => shellOf()?.getBoundingClientRect().height ?? window.innerHeight

  const edges = shown.handles.map(handle => {
    const key = sizeKey(handle), vertical = handle.edge === 'left' || handle.edge === 'right'
    const { rect } = handle
    const at = handle.edge === 'right' ? rect.right + shown.gapX / 2 : handle.edge === 'left' ? rect.left - shown.gapX / 2 : handle.edge === 'bottom' ? rect.bottom + shown.gapY / 2 : rect.top - shown.gapY / 2
    const style = vertical ? { left: at - 6, top: rect.top, height: rect.bottom - rect.top } : { top: at - 6, left: rect.left, width: rect.right - rect.left }
    const size = Math.round(vertical ? rect.right - rect.left : rect.bottom - rect.top)
    const label = panelLabel(handle.name), Icon = ICON[handle.edge]
    return <div key={`${handle.name}-${handle.edge}`} className={`panel-edge ${vertical ? 'vertical' : 'horizontal'} edge-${handle.edge}${drag === key ? ' dragging' : ''}`} style={style}>
      <div role="separator" tabIndex={0} className="panel-grip" aria-orientation={vertical ? 'vertical' : 'horizontal'} aria-label={`Resize ${label}`} aria-valuenow={size}
        title={`Drag to resize ${label}. Double-click to reset.`}
        onPointerDown={event => { if (event.button !== 0) return; event.currentTarget.setPointerCapture(event.pointerId); event.preventDefault(); start.current = { x: event.clientX, y: event.clientY, handle, size }; setDrag(key) }}
        onPointerMove={event => { const from = start.current; if (!from) return; from.size = dragSize(from.handle, event.clientX - from.x, event.clientY - from.y, room()); setLive(key, from.size) }}
        onPointerUp={() => { const from = start.current; start.current = null; setDrag(null); if (from) onSize(key, from.size) }}
        onPointerCancel={() => { const from = start.current; start.current = null; setDrag(null); if (from) onSize(key, from.size) }}
        onDoubleClick={() => commit(key, null)}
        onKeyDown={event => {
          const grow = handle.edge === 'right' ? 'ArrowRight' : handle.edge === 'left' ? 'ArrowLeft' : handle.edge === 'bottom' ? 'ArrowDown' : 'ArrowUp'
          const shrink = { ArrowRight: 'ArrowLeft', ArrowLeft: 'ArrowRight', ArrowDown: 'ArrowUp', ArrowUp: 'ArrowDown' }[grow]
          if (event.key === grow || event.key === shrink) {
            event.preventDefault()
            const step = (event.shiftKey ? STEP * 4 : STEP) * (event.key === grow ? 1 : -1)
            const dx = handle.edge === 'right' ? step : handle.edge === 'left' ? -step : 0, dy = handle.edge === 'bottom' ? step : handle.edge === 'top' ? -step : 0
            commit(key, dragSize(handle, dx, dy, room()))
          } else if (event.key === 'Enter') { event.preventDefault(); commit(key, null) }
        }}><span aria-hidden="true" /></div>
      <button type="button" className="panel-close" aria-label={`Close ${label}`} title={`Close ${label}`} onClick={() => onClose(handle.name)}><Icon size={14} aria-hidden="true" /></button>
    </div>
  })

  // Closed layout panels: one tab each, at the edge of the chat the panel sat on.
  const chat = shown.chat
  const bySide: Record<Edge, string[]> = { left: [], right: [], top: [], bottom: [] }
  for (const name of closed) bySide[closedSide(grid, name)].push(name)
  const tabs = chat ? (Object.keys(bySide) as Edge[]).flatMap(side => bySide[side].map((name, index) => {
    const label = panelLabel(name), Icon = ICON[side]
    // Side tabs are slim and vertical at mid-height, inside the chat's side padding: clear of its header and composer.
    const middle = chat.top + (chat.bottom - chat.top) * 0.38 + index * 136
    const style = side === 'left' ? { left: chat.left, top: middle } : side === 'right' ? { right: window.innerWidth - chat.right, top: middle }
      : side === 'top' ? { top: chat.top, left: (chat.left + chat.right) / 2 - 60 + index * 128 } : { bottom: window.innerHeight - chat.bottom, left: (chat.left + chat.right) / 2 - 60 + index * 128 }
    return <button key={name} type="button" className={`panel-reopen side-${side}`} style={style} title={`Open ${label}`} onClick={() => onOpen(name)}><Icon size={14} aria-hidden="true" /><span>{label}</span></button>
  })) : []

  return <div className="panel-handles">{edges}{tabs}</div>
}
