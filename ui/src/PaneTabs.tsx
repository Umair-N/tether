import { forwardRef, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react'
import { useWords } from './words'
import { Plus } from 'lucide-react'
import type { Pane } from './api'
import { AgentIcon, needsYou, paneTitle, shownIndex } from './Sidebar'
import { anchorOf, clipTitle } from './hooks'
import type { Anchor } from './hooks'
import type { Session } from './Sidebar'

// Put panes in the saved order. Panes not in the list (new splits) keep tmux order at the end.
export function orderPanes(panes: Pane[], order: string[] | undefined) {
  if (!order?.length) return panes
  const rank = (pane: Pane) => { const at = order.indexOf(pane.id); return at < 0 ? Number.MAX_SAFE_INTEGER : at }
  return panes.map((pane, at) => ({ pane, at })).sort((a, b) => rank(a.pane) - rank(b.pane) || a.at - b.at).map(item => item.pane)
}

interface Props {
  session: Session
  pane: Pane
  onOpenPane: (pane: Pane) => void
  onPaneActions: (id: string, at?: Anchor) => void
  onSplit: (direction: 'right') => void
  onReorder: (ids: string[]) => void
}
// One shared pill marks the active tab. A side swipe on the chat slides it toward the next tab with the finger.
const SLIDE_CURVE = 'cubic-bezier(.2,.8,.2,1)'
const scrollBySession = new Map<string, number>()  // the strip remounts with each pane: keep its scroll place
const swipeScroll = new WeakMap<HTMLElement, number>()  // strip scroll when the swipe started
const scrollTween = new WeakMap<HTMLElement, number>()

/** Place the pill `progress` (0..1) of the way from the active tab to the tab `toId`. `ms` animates the move. */
export function movePill(strip: HTMLElement | null, toId: string | null, progress: number, ms = 0) {
  const pill = strip?.querySelector<HTMLElement>('.pane-tab-pill')
  const from = strip?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')
  if (!strip || !pill || !from) return
  const to = toId ? strip.querySelector<HTMLElement>(`[data-pane="${CSS.escape(toId)}"]`) : null
  const p = to ? Math.min(1, Math.max(0, progress)) : 0
  const end = to ?? from
  pill.style.transition = ms ? `transform ${ms}ms ${SLIDE_CURVE}, width ${ms}ms ${SLIDE_CURVE}` : ''
  pill.style.transform = `translate(${from.offsetLeft + (end.offsetLeft - from.offsetLeft) * p}px, ${from.offsetTop}px)`
  pill.style.width = `${from.offsetWidth + (end.offsetWidth - from.offsetWidth) * p}px`
  pill.style.height = `${from.offsetHeight}px`
  // Scroll the strip with the same progress, so a target tab off screen comes into view.
  cancelAnimationFrame(scrollTween.get(strip) ?? 0)
  let base = swipeScroll.get(strip)
  if (!p) { if (base !== undefined) { swipeScroll.delete(strip); scrollTo(strip, base, ms) } return }
  if (base === undefined) { base = strip.scrollLeft; swipeScroll.set(strip, base) }
  const pad = 12, view = strip.clientWidth
  const goal = end.offsetLeft - pad < base ? end.offsetLeft - pad : end.offsetLeft + end.offsetWidth + pad > base + view ? end.offsetLeft + end.offsetWidth + pad - view : base
  scrollTo(strip, base + (goal - base) * p, ms)
}
// A scroll that follows the same curve and time as the pill (smooth scrolling has its own timing).
function scrollTo(strip: HTMLElement, left: number, ms: number) {
  if (!ms) { strip.scrollLeft = left; return }
  const start = strip.scrollLeft, began = performance.now()
  const step = (now: number) => {
    const t = Math.min(1, (now - began) / ms), eased = 1 - Math.pow(1 - t, 3)
    strip.scrollLeft = start + (left - start) * eased
    if (t < 1) scrollTween.set(strip, requestAnimationFrame(step))
  }
  scrollTween.set(strip, requestAnimationFrame(step))
}

interface Drag { id: string; startX: number; startY: number; lifted: boolean; moved: boolean; touch: boolean; timer: number; centers: { id: string; x: number }[] }
const HOLD_MS = 350

// Tabs for the panes of one session. Reorder: long-press a tab, then drag it (a mouse drags at once).
// A long press without a drag opens the pane actions, as before. Only Tether's order changes; tmux panes stay put.
export const PaneTabs = forwardRef<HTMLDivElement, Props>(function PaneTabs({ session, pane, onOpenPane, onPaneActions, onSplit, onReorder }, ref) {
  const strip = useRef<HTMLDivElement>(null)
  const words = useWords()
  useImperativeHandle(ref, () => strip.current!)
  const drag = useRef<Drag | null>(null)
  const suppressClick = useRef(false)
  const [preview, setPreviewState] = useState<string[] | null>(null)
  const previewNow = useRef<string[] | null>(null)
  const setPreview = (next: string[] | null) => { previewNow.current = next; setPreviewState(next) }
  const [dragging, setDragging] = useState<string | null>(null)
  const ids = session.panes.map(item => item.id)
  const order = ids.join()
  // Before paint: restore the strip scroll and put the pill on the active tab. Tab sizes change with names and state dots.
  useLayoutEffect(() => {
    const box = strip.current
    if (!box) return
    box.scrollLeft = scrollBySession.get(session.id) ?? 0
    const save = () => scrollBySession.set(session.id, box.scrollLeft)
    const place = () => { if (swipeScroll.get(box) === undefined) movePill(box, null, 0) }
    place()
    const observer = new ResizeObserver(place)
    for (const tab of box.querySelectorAll('[role="tab"]')) observer.observe(tab)
    box.addEventListener('scroll', save, { passive: true })
    return () => { observer.disconnect(); box.removeEventListener('scroll', save) }
  }, [session.id, pane.id, order])
  const latest = useRef({ ids, onReorder, onPaneActions })
  latest.current = { ids, onReorder, onPaneActions }

  useEffect(() => {
    const box = strip.current
    if (!box) return
    const tabOf = (target: EventTarget | null) => (target as Element | null)?.closest<HTMLElement>('[data-pane]')
    // Tab centers in strip coordinates, so a scrolled strip still maps the finger to the right slot.
    const lift = (state: Drag) => {
      const left = box.getBoundingClientRect().left - box.scrollLeft
      state.centers = [...box.querySelectorAll<HTMLElement>('[data-pane]')].map(tab => { const rect = tab.getBoundingClientRect(); return { id: tab.dataset.pane!, x: rect.left - left + rect.width / 2 } })
      state.lifted = true
      if (state.touch) navigator.vibrate?.(15)
      setDragging(state.id)
      setPreview(latest.current.ids)
    }
    const move = (state: Drag, x: number) => {
      const rect = box.getBoundingClientRect()
      if (x < rect.left + 32) box.scrollLeft -= 8
      else if (x > rect.right - 32) box.scrollLeft += 8
      const at = x - rect.left + box.scrollLeft
      const others = state.centers.filter(item => item.id !== state.id)
      const slot = others.filter(item => item.x < at).length
      const next = others.map(item => item.id)
      next.splice(slot, 0, state.id)
      if (previewNow.current?.join() !== next.join()) setPreview(next)
    }
    const finish = (commit: boolean) => {
      const state = drag.current
      drag.current = null
      if (!state) return
      window.clearTimeout(state.timer)
      if (state.lifted) {
        suppressClick.current = true
        window.setTimeout(() => { suppressClick.current = false }, 400)
        const order = previewNow.current
        if (commit && state.moved && order && order.join() !== latest.current.ids.join()) latest.current.onReorder(order)
        setPreview(null)
        if (commit && !state.moved && state.touch) latest.current.onPaneActions(state.id)
      }
      setDragging(null)
    }
    const touchStart = (event: TouchEvent) => {
      const tab = tabOf(event.target)
      if (!tab || event.touches.length > 1) return
      const touch = event.touches[0]
      const state: Drag = { id: tab.dataset.pane!, startX: touch.clientX, startY: touch.clientY, lifted: false, moved: false, touch: true, timer: 0, centers: [] }
      state.timer = window.setTimeout(() => { if (drag.current === state) lift(state) }, HOLD_MS)
      drag.current = state
    }
    const touchMove = (event: TouchEvent) => {
      const state = drag.current
      if (!state) return
      const touch = event.touches[0]
      if (!state.lifted) {
        // The finger moved before the hold time: this is a scroll of the strip, not a drag.
        if (Math.hypot(touch.clientX - state.startX, touch.clientY - state.startY) > 10) { window.clearTimeout(state.timer); drag.current = null }
        return
      }
      event.preventDefault()
      if (Math.abs(touch.clientX - state.startX) > 6) state.moved = true
      if (state.moved) move(state, touch.clientX)
    }
    const touchEnd = (event: TouchEvent) => { if (drag.current?.lifted) event.preventDefault(); finish(event.type === 'touchend') }
    const mouseDown = (event: MouseEvent) => {
      const tab = tabOf(event.target)
      if (!tab || event.button !== 0) return
      drag.current = { id: tab.dataset.pane!, startX: event.clientX, startY: event.clientY, lifted: false, moved: false, touch: false, timer: 0, centers: [] }
    }
    const mouseMove = (event: MouseEvent) => {
      const state = drag.current
      if (!state || state.touch) return
      if (!state.lifted) { if (Math.abs(event.clientX - state.startX) < 6) return; lift(state) }
      state.moved = true
      move(state, event.clientX)
    }
    const mouseUp = () => { if (drag.current && !drag.current.touch) finish(true) }
    box.addEventListener('touchstart', touchStart, { passive: true })
    box.addEventListener('touchmove', touchMove, { passive: false })
    box.addEventListener('touchend', touchEnd, { passive: false })
    box.addEventListener('touchcancel', touchEnd)
    box.addEventListener('mousedown', mouseDown)
    window.addEventListener('mousemove', mouseMove)
    window.addEventListener('mouseup', mouseUp)
    return () => {
      if (drag.current) window.clearTimeout(drag.current.timer)
      box.removeEventListener('touchstart', touchStart)
      box.removeEventListener('touchmove', touchMove)
      box.removeEventListener('touchend', touchEnd)
      box.removeEventListener('touchcancel', touchEnd)
      box.removeEventListener('mousedown', mouseDown)
      window.removeEventListener('mousemove', mouseMove)
      window.removeEventListener('mouseup', mouseUp)
    }
  }, [])

  const byId = new Map(session.panes.map(item => [item.id, item]))
  const shown = preview ? preview.map(id => byId.get(id)).filter((item): item is Pane => !!item) : session.panes
  // While tabs are reordered, each tab draws its own highlight again.
  return <div className={preview ? 'pane-tabs' : 'pane-tabs has-pill'} ref={strip} role="tablist" aria-label={`Panes of ${session.name}`}>
    <span className="pane-tab-pill" aria-hidden="true" />
    {shown.map(item => <button key={item.id} data-pane={item.id} role="tab" aria-selected={item.id === pane.id} className={[item.id === pane.id ? 'active' : '', dragging === item.id ? 'dragging' : ''].filter(Boolean).join(' ')}
      onClick={() => { if (!suppressClick.current) onOpenPane(item) }}
      // A touch long press is handled above (drag, or actions on release). A right-click still opens the actions.
      onMouseEnter={clipTitle} onContextMenu={event => { event.preventDefault(); if (!drag.current?.touch && !suppressClick.current) onPaneActions(item.id, anchorOf(event)) }}>
      <AgentIcon agent={item.agent} icon={item.pane_icon} size={14} /><span className="pane-tab-name">{paneTitle(item)}</span>{shownIndex(item) && <span className="pane-index">{shownIndex(item)}</span>}
      {item.state === 'Working' || needsYou(item.state) ? <span className={`state-dot ${item.state === 'Working' ? 'working' : 'approval'}`} aria-label={item.state === 'Working' ? 'Working' : 'Needs you'} /> : null}
    </button>)}
    <button className="tab-add" aria-label={words.newRight} title={words.newRight} onClick={() => onSplit('right')}><Plus size={16} /></button>
  </div>
})
