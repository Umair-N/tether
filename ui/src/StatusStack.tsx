import { useId, useLayoutEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

const HEIGHT_KEY = 'tether.statusHeight'

interface Item { id: string; label: string; active: boolean; content: ReactNode }
// A fresh question takes priority; polling the same question preserves the person's choice.
export function StatusStack({ priorityKey, items, chips }: { priorityKey: string; items: Item[]; chips: ReactNode }) {
  const prefix = useId()
  const stack = useRef<HTMLElement>(null)
  const [selection, setSelection] = useState<{ priority: string; id: string | null } | null>(null)
  const available = items.filter(item => item.active)
  const preferred = available.find(item => item.id === 'action' || item.id === 'heads')?.id ?? null
  const selected = selection?.priority === priorityKey ? selection.id : preferred
  const open = available.find(item => item.id === selected)
  useLayoutEffect(() => { if (stack.current) stack.current.scrollTop = 0 }, [open?.id, priorityKey])
  // The person sets the height with the grip; it is kept per device. Without a saved height, use half the screen.
  const [height, setHeight] = useState<number | null>(() => { try { const saved = Number(localStorage.getItem(HEIGHT_KEY)); return saved > 0 ? saved : null } catch { return null } })
  const limit = (value: number) => Math.round(Math.min(window.innerHeight * 0.85, Math.max(96, value)))
  const save = (value: number | null) => { setHeight(value); try { if (value) localStorage.setItem(HEIGHT_KEY, String(value)); else localStorage.removeItem(HEIGHT_KEY) } catch { /* storage blocked: keep it for this visit */ } }
  const drag = useRef<{ y: number; start: number } | null>(null)
  const grip = {
    onPointerDown: (event: React.PointerEvent<HTMLDivElement>) => { event.currentTarget.setPointerCapture(event.pointerId); drag.current = { y: event.clientY, start: stack.current?.getBoundingClientRect().height ?? 200 } },
    onPointerMove: (event: React.PointerEvent<HTMLDivElement>) => { if (drag.current) setHeight(limit(drag.current.start + drag.current.y - event.clientY)) },
    onPointerUp: () => { if (drag.current) { drag.current = null; save(height) } },
    onPointerCancel: () => { drag.current = null },
    onDoubleClick: () => save(null),
    onKeyDown: (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return
      event.preventDefault(); save(limit((height ?? stack.current?.getBoundingClientRect().height ?? 200) + (event.key === 'ArrowUp' ? 40 : -40)))
    },
  }
  const toggle = (id: string) => setSelection({ priority: priorityKey, id: open?.id === id ? null : id })
  return <section ref={stack} hidden={!available.length && !chips} className="status-stack" aria-label="Session status" style={height ? { maxHeight: height } : undefined}>
    {open && <div className="status-grip" role="separator" aria-orientation="horizontal" aria-label="Resize status panel. Drag, or use the arrow keys. Double-tap to reset." tabIndex={0} aria-valuenow={height ?? undefined} {...grip}><span /></div>}
    <div className="status-rail">
      {available.map(item => <button key={item.id} type="button" className={`status-chip ${item.id === 'action' ? 'needs-action' : ''}`} aria-expanded={open?.id === item.id} aria-controls={`${prefix}-${item.id}`} onClick={() => toggle(item.id)}>{item.label}</button>)}
      {chips}
    </div>
    {available.map(item => <div key={item.id} id={`${prefix}-${item.id}`} className="status-content" hidden={open?.id !== item.id}>
      {item.content}
    </div>)}
  </section>
}
