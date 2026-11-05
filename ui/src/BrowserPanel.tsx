import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'
import { ArrowLeft, ArrowRight, Bot, Check, Crosshair, HatGlasses, Keyboard, KeyRound, Monitor, PenLine, Plus, RotateCw, Send, Smartphone, Square, Tablet, Trash2, Type, X } from 'lucide-react'
import { errorText } from './api'
import type { Api, Pane } from './api'
import { usePaneLabel, WATCH_EVENT } from './AgentBrowser'
import { useHost } from './host'

// Same shape as PanelProps in src/panels.ts (frame lane); kept here until the lanes merge.
export interface BrowserPanelProps { api: Api; pane: Pane; folder: string | null; active: boolean }

type Mode = 'private' | 'saved'
// 'agent': a tab an agent opened over CDP (server/browser_cdp.py). The agent owns its size until a person takes over
// (controller 'user'); Stop gives the agent its own viewport back.
interface BrowserTab { id: string; url: string; title: string; preset: string; mode: Mode | 'agent'; width: number; height: number; touch: boolean; owner?: { pane: string | null } | null; controller?: 'user' | 'agent' }
interface Preset { id: string; label: string; width: number; height: number; touch: boolean }
interface BrowserState { tabs: BrowserTab[]; presets: Preset[]; nav?: { back: boolean; forward: boolean } }
interface FrameMessage { seq: number; width: number; height: number; jpeg: string }
type Gesture = Record<string, string | number | string[]>
type Tool = 'point' | 'pen' | 'rect' | 'text'
type Shape = { kind: 'point'; x: number; y: number } | { kind: 'pen'; points: number[] } | { kind: 'rect'; x: number; y: number; w: number; h: number } | { kind: 'text'; x: number; y: number; text: string }

// The composer of the pane with this id takes the files and uploads them (see Composer.tsx). It cancels the event when it does.
const ATTACH_EVENT = 'tether-attach'
const MARK = '#e5372b'
const KEYS = new Set(['Enter', 'Backspace', 'Tab', 'Escape', 'Delete', 'ArrowLeft', 'ArrowUp', 'ArrowRight', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'])
const COMMANDS: Record<string, string> = { a: 'selectAll', c: 'copy', x: 'cut', z: 'undo', y: 'redo' }
const BUTTONS = ['left', 'middle', 'right']
const SENTINEL = '_' // the hidden text box keeps one character, so a phone keyboard's Backspace has something to delete
const TOOLS: { id: Tool; label: string; icon: typeof PenLine }[] = [
  { id: 'point', label: 'Point', icon: Crosshair }, { id: 'pen', label: 'Pen', icon: PenLine },
  { id: 'rect', label: 'Rectangle', icon: Square }, { id: 'text', label: 'Text', icon: Type },
]

function modifiers(event: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }) {
  return (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0)
}
function shown(url: string) { return url === 'about:blank' ? '' : url }
function tabName(tab: BrowserTab) {
  if (tab.title && tab.title !== 'about:blank') return tab.title
  try { return tab.url === 'about:blank' ? 'New tab' : new URL(tab.url).host || tab.url } catch { return tab.url || 'New tab' }
}
function DeviceIcon({ preset }: { preset: string }) {
  return preset === 'desktop' ? <Monitor size={17} /> : preset === 'tablet' ? <Tablet size={17} /> : <Smartphone size={17} />
}

// Close a menu on Escape or on a click outside it.
function useMenu() {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false) }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])
  const onKeyDown = (event: ReactKeyboardEvent) => { if (event.key === 'Escape' && open) { event.stopPropagation(); setOpen(false); ref.current?.querySelector<HTMLButtonElement>('button')?.focus() } }
  return { open, setOpen, ref, onKeyDown }
}

function usePageVisible() {
  const [visible, setVisible] = useState(() => document.visibilityState === 'visible')
  useEffect(() => {
    const update = () => setVisible(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', update)
    return () => document.removeEventListener('visibilitychange', update)
  }, [])
  return visible
}

/** A tab of the server's headless Chromium: tabs, address bar, device presets, the streamed page, and notes on a still frame. */
// A phone: tabs it opens or takes over start at phone width.
const onPhone = () => typeof matchMedia === 'function' && matchMedia('(pointer: coarse) and (max-width: 820px)').matches
const savedNote = (host: string) => `Saved logins · readable by agents on ${host}`
export function BrowserPanel({ api, pane, active }: BrowserPanelProps) {
  const host = useHost()
  const [tabs, setTabs] = useState<BrowserTab[]>([])
  const [presets, setPresets] = useState<Preset[]>([])
  const [nav, setNav] = useState({ back: false, forward: false })
  const [current, setCurrent] = useState<string | null>(() => { try { return localStorage.getItem('tether-browser-tab') } catch { return null } })
  const [loaded, setLoaded] = useState(false)
  const [failure, setFailure] = useState('')
  const [status, setStatus] = useState('')
  const [live, setLive] = useState(true)
  const [typing, setTyping] = useState(false)
  const [draft, setDraft] = useState<string | null>(null)
  const [stage, setStage] = useState({ width: 0, height: 0 })
  const [frame, setFrame] = useState<{ width: number; height: number } | null>(null)
  const [noting, setNoting] = useState(false)
  const [tool, setTool] = useState<Tool>('pen')
  const [shapes, setShapes] = useState<Shape[]>([])
  const [label, setLabel] = useState<{ x: number; y: number; left: number; top: number; text: string } | null>(null)
  const visible = usePageVisible()
  const tabMenu = useMenu()
  const deviceMenu = useMenu()
  const tab = tabs.find(item => item.id === current) ?? tabs[0] ?? null
  const tabId = tab?.id ?? null
  // Agent tabs are watch-only until the person takes over: no clicks, keys or navigation reach the page.
  // The server holds who controls the viewport, so every phone and desktop shows the same state.
  const watching = tab?.mode === 'agent' && tab.controller !== 'user'
  const [handing, setHanding] = useState(false)
  const watchingRef = useRef(watching)
  watchingRef.current = watching
  const owner = usePaneLabel(tab?.owner?.pane)

  const stageRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const marksRef = useRef<HTMLCanvasElement>(null)
  const keysRef = useRef<HTMLTextAreaElement>(null)
  const urlRef = useRef<HTMLInputElement>(null)
  const tabRef = useRef(tab)
  tabRef.current = tab
  const frameRef = useRef(frame)
  frameRef.current = frame
  const notingRef = useRef(noting)
  notingRef.current = noting

  function apply(state: BrowserState) {
    setTabs(state.tabs)
    if (state.presets) setPresets(state.presets)
    if (state.nav) setNav(state.nav)
  }
  function choose(id: string) {
    setCurrent(id)
    setFrame(null)
    setNav({ back: false, forward: false })
    try { localStorage.setItem('tether-browser-tab', id) } catch { /* private mode */ }
  }
  async function load() {
    try { apply(await api<BrowserState>('browser/tabs')); setFailure('') } catch (error) { setFailure(errorText(error)) } finally { setLoaded(true) }
  }
  // Start the browser only when the panel is first shown.
  useEffect(() => { if (active && !loaded) void load() }, [active]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (tab && tab.id !== current) choose(tab.id) }, [tab?.id]) // eslint-disable-line react-hooks/exhaustive-deps
  // "Watch" in a chat card (AgentBrowser.tsx): show that tab. Reload the list: the tab can be newer than the stream's last list.
  useEffect(() => {
    const onWatch = (event: Event) => { const id = (event as CustomEvent<{ tab: string }>).detail.tab; choose(id); void load() }
    window.addEventListener(WATCH_EVENT, onWatch)
    return () => window.removeEventListener(WATCH_EVENT, onWatch)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Stage size: the page is scaled to fit it, and a Desktop tab takes its size.
  useEffect(() => {
    const box = stageRef.current
    if (!box) return
    const observer = new ResizeObserver(([entry]) => setStage({ width: Math.floor(entry.contentRect.width), height: Math.floor(entry.contentRect.height) }))
    observer.observe(box)
    return () => observer.disconnect()
  }, [loaded])
  useEffect(() => {
    if (!tab || watching || tab.preset !== 'desktop' || stage.width < 50 || stage.height < 50 || noting) return
    const width = Math.max(320, stage.width), height = Math.max(240, stage.height)
    if (Math.abs(width - tab.width) < 3 && Math.abs(height - tab.height) < 3) return
    const timer = setTimeout(() => void api<BrowserTab>('browser/device', { tab: tab.id, preset: 'desktop', width, height }).then(row => setTabs(list => list.map(item => item.id === row.id ? row : item))).catch(() => undefined), 400)
    return () => clearTimeout(timer)
  }, [tab?.id, tab?.preset, tab?.width, tab?.height, stage.width, stage.height, noting, watching]) // eslint-disable-line react-hooks/exhaustive-deps

  // Frames: decode the newest one and skip any that arrive while one decodes.
  const pending = useRef<FrameMessage | null>(null)
  const decoding = useRef(false)
  async function paint() {
    decoding.current = true
    while (pending.current) {
      const message = pending.current
      pending.current = null
      try {
        const binary = atob(message.jpeg)
        const bytes = new Uint8Array(binary.length)
        for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index)
        const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/jpeg' }))
        const canvas = canvasRef.current
        if (canvas && !notingRef.current) {
          if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) { canvas.width = bitmap.width; canvas.height = bitmap.height }
          canvas.getContext('2d')?.drawImage(bitmap, 0, 0)
          setFrame(old => old && old.width === message.width && old.height === message.height ? old : { width: message.width, height: message.height })
        }
        bitmap.close()
      } catch { /* skip a frame that does not decode */ }
    }
    decoding.current = false
  }

  // The stream runs only while the panel is shown, the page is visible, and no note is open on a still frame.
  const streamWidth = Math.min(2560, Math.max(160, Math.ceil(stage.width * devicePixelRatio / 64) * 64))
  const streamHeight = Math.min(2560, Math.max(160, Math.ceil(stage.height * devicePixelRatio / 64) * 64))
  useEffect(() => {
    if (!active || !visible || !tabId || noting || !stage.width) return
    const source = new EventSource(`/mobile/api/browser/stream?tab=${encodeURIComponent(tabId)}&w=${streamWidth}&h=${streamHeight}`)
    source.addEventListener('frame', event => { pending.current = JSON.parse((event as MessageEvent).data); setLive(true); if (!decoding.current) void paint() })
    source.addEventListener('tabs', event => apply(JSON.parse((event as MessageEvent).data)))
    source.addEventListener('closed', () => void load())
    source.onopen = () => setLive(true)
    source.onerror = () => setLive(false) // EventSource connects again by itself
    return () => source.close()
  }, [active, visible, tabId, noting, streamWidth, streamHeight, stage.width > 0]) // eslint-disable-line react-hooks/exhaustive-deps

  // Input: queue events, merge moves and wheel turns, and send one batch per animation frame.
  const queue = useRef<Gesture[]>([])
  const sending = useRef(false)
  const scheduled = useRef(false)
  function push(...events: Gesture[]) {
    if (watchingRef.current) return
    for (const event of events) {
      const last = queue.current[queue.current.length - 1]
      if (last && last.type === event.type && event.type === 'move') queue.current[queue.current.length - 1] = event
      else if (last && last.type === 'wheel' && event.type === 'wheel') { last.dx = Number(last.dx) + Number(event.dx); last.dy = Number(last.dy) + Number(event.dy) }
      else queue.current.push(event)
    }
    if (!scheduled.current) { scheduled.current = true; requestAnimationFrame(() => { scheduled.current = false; void flush() }) }
  }
  async function flush() {
    const target = tabRef.current?.id
    if (sending.current || !queue.current.length || !target) return
    sending.current = true
    const events = queue.current.splice(0, 200)
    try { await api('browser/input', { tab: target, events }) } catch (error) { setStatus(errorText(error)) } finally {
      sending.current = false
      if (queue.current.length) void flush()
    }
  }
  // Canvas point to page CSS pixels.
  function pagePoint(event: { clientX: number; clientY: number }) {
    const canvas = canvasRef.current, size = frameRef.current ?? (tabRef.current ? { width: tabRef.current.width, height: tabRef.current.height } : null)
    if (!canvas || !size) return { x: 0, y: 0 }
    const box = canvas.getBoundingClientRect()
    return { x: Math.round((event.clientX - box.left) / box.width * size.width), y: Math.round((event.clientY - box.top) / box.height * size.height) }
  }
  function cssScale() {
    const canvas = canvasRef.current, size = frameRef.current
    return canvas && size ? size.width / canvas.getBoundingClientRect().width : 1
  }

  const lastDown = useRef({ time: 0, x: 0, y: 0, count: 0 })
  const drag = useRef<{ id: number; x: number; y: number; start: { x: number; y: number }; moved: boolean } | null>(null)
  function onPointerDown(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (!tab) return
    if (event.pointerType === 'mouse') keysRef.current?.focus({ preventScroll: true })
    event.currentTarget.setPointerCapture(event.pointerId)
    const point = pagePoint(event)
    // A finger on a Desktop tab scrolls; a tap clicks.
    if (event.pointerType === 'touch' && !tab.touch) { drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, start: point, moved: false }; return }
    if (event.button > 2) return
    const now = performance.now(), last = lastDown.current
    const count = now - last.time < 500 && Math.abs(point.x - last.x) < 5 && Math.abs(point.y - last.y) < 5 ? Math.min(3, last.count + 1) : 1
    lastDown.current = { time: now, ...point, count }
    push({ type: 'down', ...point, button: BUTTONS[event.button] ?? 'left', buttons: event.buttons, clicks: count, modifiers: modifiers(event) })
  }
  function onPointerMove(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (!tab) return
    const touch = drag.current
    if (touch && touch.id === event.pointerId) {
      const dx = event.clientX - touch.x, dy = event.clientY - touch.y
      if (!touch.moved && Math.hypot(dx, dy) < 8) return
      touch.moved = true
      touch.x = event.clientX; touch.y = event.clientY
      const scale = cssScale()
      push({ type: 'wheel', ...touch.start, dx: -dx * scale, dy: -dy * scale })
      return
    }
    // A phone or tablet tab gets only pressed moves (the server sends them as a finger drag); hover means nothing there.
    if (tab.touch ? !event.buttons : event.pointerType === 'touch') return
    push({ type: 'move', ...pagePoint(event), button: event.buttons & 1 ? 'left' : event.buttons & 2 ? 'right' : 'none', buttons: event.buttons, modifiers: modifiers(event) })
  }
  function onPointerUp(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (!tab) return
    const touch = drag.current
    if (touch && touch.id === event.pointerId) {
      drag.current = null
      if (!touch.moved) push({ type: 'down', ...touch.start, button: 'left', clicks: 1 }, { type: 'up', ...touch.start, button: 'left', clicks: 1 })
      return
    }
    if (event.button > 2) return
    push({ type: 'up', ...pagePoint(event), button: BUTTONS[event.button] ?? 'left', buttons: event.buttons, clicks: lastDown.current.count, modifiers: modifiers(event) })
  }
  // React adds wheel listeners as passive, so the page could not stop the panel from scrolling. Add it here.
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const wheel = (event: WheelEvent) => {
      if (notingRef.current || !tabRef.current) return
      event.preventDefault()
      const step = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? 800 : 1
      push({ type: 'wheel', ...pagePoint(event), dx: event.deltaX * step, dy: event.deltaY * step, modifiers: modifiers(event) })
    }
    canvas.addEventListener('wheel', wheel, { passive: false })
    return () => canvas.removeEventListener('wheel', wheel)
  }, [loaded]) // eslint-disable-line react-hooks/exhaustive-deps

  // Keyboard: a hidden text box takes the keys. Keys with a meaning go as key events; phone keyboards send text.
  const lastEscape = useRef(0)
  const held = useRef(new Set<string>())
  function onKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing || event.key === 'Unidentified' || event.key === 'Process' || ['Shift', 'Control', 'Alt', 'Meta'].includes(event.key)) return
    if (event.key === 'Escape') {
      const now = performance.now()
      if (now - lastEscape.current < 600) { event.preventDefault(); stageRef.current?.focus(); return }
      lastEscape.current = now
    }
    const command = event.ctrlKey || event.metaKey
    if (command && event.key.toLowerCase() === 'v') return // the paste event brings the text
    if (event.key.length !== 1 && !KEYS.has(event.key)) return
    event.preventDefault()
    const name = command ? COMMANDS[event.key.toLowerCase()] : undefined
    held.current.add(event.code + event.key)
    push({ type: 'keydown', key: event.key, code: event.code, modifiers: modifiers(event), ...(name ? { commands: [event.shiftKey && name === 'undo' ? 'redo' : name] } : {}) })
  }
  function onKeyUp(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    if (!held.current.delete(event.code + event.key)) return
    event.preventDefault()
    push({ type: 'keyup', key: event.key, code: event.code, modifiers: modifiers(event) })
  }
  function syncText() {
    const box = keysRef.current
    if (!box) return
    const value = box.value
    if (value.length < SENTINEL.length) push({ type: 'keydown', key: 'Backspace', code: 'Backspace' }, { type: 'keyup', key: 'Backspace', code: 'Backspace' })
    else if (value.length > SENTINEL.length) push({ type: 'text', text: value.startsWith(SENTINEL) ? value.slice(SENTINEL.length) : value })
    box.value = SENTINEL
    box.setSelectionRange(SENTINEL.length, SENTINEL.length)
  }

  // Tabs and navigation.
  async function act<T>(path: string, body: unknown): Promise<T | null> {
    setStatus('')
    try { return await api<T>(path, body) } catch (error) { setStatus(errorText(error)); return null }
  }
  async function newTab(mode: Mode) {
    tabMenu.setOpen(false)
    const row = await act<BrowserTab>('browser/new', { mode })
    if (!row) return
    const fitted = onPhone() ? await act<BrowserTab>('browser/device', { tab: row.id, preset: 'phone' }) : null
    setTabs(list => [...list.filter(item => item.id !== row.id), fitted ?? row])
    choose(row.id)
    requestAnimationFrame(() => urlRef.current?.focus())
  }
  async function closeTab(id: string) {
    const index = tabs.findIndex(item => item.id === id)
    const state = await act<BrowserState>('browser/close', { tab: id })
    if (!state) return
    apply(state)
    if (id === tab?.id) { const next = state.tabs[Math.min(index, state.tabs.length - 1)]; if (next) choose(next.id) }
  }
  async function navigate(text: string) {
    if (!tab || !text.trim()) return
    const row = await act<BrowserTab & { error?: string }>('browser/navigate', { tab: tab.id, url: text.trim() })
    if (!row) return
    setTabs(list => list.map(item => item.id === row.id ? { ...item, url: row.url } : item))
    if (row.error) setStatus(`Could not open ${row.url}: ${row.error.replace(/^net::/, '')}`)
    setDraft(null)
    urlRef.current?.blur()
  }
  async function sendDevice(body: Record<string, unknown>) {
    const row = await act<BrowserTab>('browser/device', body)
    if (row) { setTabs(list => list.map(item => item.id === row.id ? row : item)); setFrame(null) }
    return row
  }
  async function setDevice(preset: string) {
    deviceMenu.setOpen(false)
    if (!tab) return
    await sendDevice(preset === 'desktop' ? { tab: tab.id, preset, width: Math.max(320, stage.width), height: Math.max(240, stage.height) } : { tab: tab.id, preset })
  }
  // Take over: a device request takes the viewport from the agent. Stop: the server puts back the agent's own viewport.
  async function handOver(toUser: boolean) {
    if (!tab || handing) return
    setHanding(true)
    try { await sendDevice(!toUser ? { tab: tab.id, preset: 'agent' } : onPhone() ? { tab: tab.id, preset: 'phone' } : { tab: tab.id, preset: 'desktop', width: Math.max(320, stage.width), height: Math.max(240, stage.height) }) } finally { setHanding(false) }
  }

  // Notes: draw on a still copy of the frame, then hand the PNG to this pane's composer.
  function startNotes() {
    if (!canvasRef.current?.width) return
    setShapes([]); setLabel(null); setTool('pen'); setStatus(''); setNoting(true)
  }
  function stopNotes() { setNoting(false); setShapes([]); setLabel(null) }
  const drawing = useRef<Shape | null>(null)
  function markPoint(event: { clientX: number; clientY: number }) {
    const marks = marksRef.current!
    const box = marks.getBoundingClientRect()
    return { x: (event.clientX - box.left) * marks.width / box.width, y: (event.clientY - box.top) * marks.height / box.height, left: event.clientX - box.left, top: event.clientY - box.top }
  }
  function render(list: Shape[]) {
    const marks = marksRef.current
    const context = marks?.getContext('2d')
    if (!marks || !context) return
    const unit = marks.width / Math.max(1, marks.getBoundingClientRect().width)
    context.clearRect(0, 0, marks.width, marks.height)
    context.lineCap = 'round'; context.lineJoin = 'round'
    for (const shape of list) {
      context.strokeStyle = MARK; context.fillStyle = MARK; context.lineWidth = 3 * unit
      if (shape.kind === 'pen') {
        context.beginPath()
        for (let index = 0; index < shape.points.length; index += 2) context[index ? 'lineTo' : 'moveTo'](shape.points[index], shape.points[index + 1])
        context.stroke()
      } else if (shape.kind === 'rect') {
        context.strokeRect(shape.x, shape.y, shape.w, shape.h)
      } else if (shape.kind === 'point') {
        context.beginPath(); context.arc(shape.x, shape.y, 14 * unit, 0, Math.PI * 2); context.stroke()
        context.beginPath(); context.arc(shape.x, shape.y, 4 * unit, 0, Math.PI * 2); context.fill()
      } else {
        context.font = `600 ${16 * unit}px system-ui, sans-serif`; context.textBaseline = 'middle'
        context.lineWidth = 4 * unit; context.strokeStyle = '#fff'; context.strokeText(shape.text, shape.x, shape.y)
        context.fillText(shape.text, shape.x, shape.y)
      }
    }
  }
  useEffect(() => {
    const canvas = canvasRef.current, marks = marksRef.current
    if (!noting || !canvas || !marks) return
    if (marks.width !== canvas.width || marks.height !== canvas.height) { marks.width = canvas.width; marks.height = canvas.height }
    render(drawing.current ? [...shapes, drawing.current] : shapes)
  }, [noting, shapes])
  function onMarkDown(event: ReactPointerEvent<HTMLCanvasElement>) {
    const point = markPoint(event)
    if (label) commitLabel()
    if (tool === 'point') { setShapes(list => [...list, { kind: 'point', x: point.x, y: point.y }]); return }
    if (tool === 'text') { setLabel({ ...point, text: '' }); return }
    event.currentTarget.setPointerCapture(event.pointerId)
    drawing.current = tool === 'pen' ? { kind: 'pen', points: [point.x, point.y] } : { kind: 'rect', x: point.x, y: point.y, w: 0, h: 0 }
  }
  function onMarkMove(event: ReactPointerEvent<HTMLCanvasElement>) {
    const shape = drawing.current
    if (!shape) return
    const point = markPoint(event)
    if (shape.kind === 'pen') shape.points.push(point.x, point.y)
    else if (shape.kind === 'rect') { shape.w = point.x - shape.x; shape.h = point.y - shape.y }
    render([...shapes, shape])
  }
  function onMarkUp() {
    const shape = drawing.current
    drawing.current = null
    if (shape && (shape.kind !== 'rect' || (Math.abs(shape.w) > 2 && Math.abs(shape.h) > 2))) setShapes(list => [...list, shape])
  }
  function commitLabel() {
    if (label?.text.trim()) setShapes(list => [...list, { kind: 'text', x: label.x, y: label.y, text: label.text.trim() }])
    setLabel(null)
  }
  async function sendToChat() {
    const canvas = canvasRef.current, marks = marksRef.current
    if (!canvas || !marks) return
    const list = label?.text.trim() ? [...shapes, { kind: 'text' as const, x: label.x, y: label.y, text: label.text.trim() }] : shapes
    render(list)
    const out = document.createElement('canvas')
    out.width = canvas.width; out.height = canvas.height
    const context = out.getContext('2d')!
    context.drawImage(canvas, 0, 0); context.drawImage(marks, 0, 0)
    const blob = await new Promise<Blob | null>(resolve => out.toBlob(resolve, 'image/png'))
    if (!blob) { setStatus('Could not make the image.'); return }
    let host = 'page'
    try { host = new URL(tab?.url ?? '').host.replace(/[^\w.-]+/g, '-') || 'page' } catch { /* about:blank */ }
    const file = new File([blob], `browser-${host}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.png`, { type: 'image/png' })
    const taken = !window.dispatchEvent(new CustomEvent(ATTACH_EVENT, { cancelable: true, detail: { pane: pane.id, files: [file] } }))
    if (!taken) { setStatus('This pane does not take files. Choose a Claude or Codex pane, then send again.'); return }
    stopNotes()
    setStatus('Image added to the message box.')
  }

  // Fit the frame (or the tab's viewport before the first frame) inside the stage.
  const size = frame ?? (tab ? { width: tab.width, height: tab.height } : null)
  const scale = size && stage.width && stage.height ? Math.min(stage.width / size.width, stage.height / size.height) : 0
  const view = size && scale ? { width: Math.floor(size.width * scale), height: Math.floor(size.height * scale) } : { width: 0, height: 0 }
  const presetLabel = presets.find(item => item.id === tab?.preset)?.label ?? 'Desktop'

  if (!loaded) return <div className="browser-panel"><p className="browser-empty">{active ? 'Starting the browser…' : ''}</p></div>
  if (failure && !tabs.length) return <div className="browser-panel"><div className="browser-empty"><p>{failure}</p><button type="button" className="browser-text-button" onClick={() => { setLoaded(false); void load() }}>Try again</button></div></div>

  return <div className="browser-panel" onKeyDown={event => { if (noting && event.key === 'Escape' && !event.defaultPrevented) stopNotes() }}>
    <div className="browser-tabs" role="tablist" aria-label="Browser tabs">
      {tabs.map(item => {
        const name = tabName(item)
        return <div key={item.id} className={item.id === tab?.id ? 'browser-tab active' : 'browser-tab'} title={item.mode === 'saved' ? `${name} · ${savedNote(host)}` : item.mode === 'agent' ? `${name} · Agent` : `${name} · Private`}>
          <button type="button" role="tab" aria-selected={item.id === tab?.id} className="browser-tab-main" onClick={() => choose(item.id)}>
            {item.mode === 'saved' ? <KeyRound size={14} aria-label="Saved" /> : item.mode === 'agent' ? <Bot size={14} aria-label="Agent" /> : <HatGlasses size={14} aria-label="Private" />}
            <span>{name}</span>
          </button>
          <button type="button" className="browser-tab-close" aria-label={`Close ${name}`} onClick={() => void closeTab(item.id)}><X size={14} /></button>
        </div>
      })}
      <div className="browser-menu-anchor" ref={tabMenu.ref} onKeyDown={tabMenu.onKeyDown}>
        <button type="button" className="browser-icon" aria-label="New tab" aria-haspopup="menu" aria-expanded={tabMenu.open} onClick={() => tabMenu.setOpen(!tabMenu.open)}><Plus size={17} /></button>
        {tabMenu.open && <div className="browser-menu" role="menu">
          <button type="button" role="menuitem" autoFocus onClick={() => void newTab('private')}><HatGlasses size={16} /><span>New private tab<small>Logins go away when the tab closes</small></span></button>
          <button type="button" role="menuitem" onClick={() => void newTab('saved')}><KeyRound size={16} /><span>New saved tab<small>{savedNote(host)}</small></span></button>
        </div>}
      </div>
    </div>

    {noting ? <div className="browser-bar browser-notes" role="toolbar" aria-label="Notes">
      {TOOLS.map(item => <button key={item.id} type="button" className="browser-icon" aria-label={item.label} title={item.label} aria-pressed={tool === item.id} onClick={() => { commitLabel(); setTool(item.id) }}><item.icon size={17} /></button>)}
      <span className="browser-divider" />
      <button type="button" className="browser-icon" aria-label="Clear notes" title="Clear" disabled={!shapes.length && !label} onClick={() => { setShapes([]); setLabel(null) }}><Trash2 size={17} /></button>
      <span className="browser-grow" />
      <button type="button" className="browser-icon" aria-label="Close notes" title="Close" onClick={stopNotes}><X size={17} /></button>
      <button type="button" className="browser-send" onClick={() => void sendToChat()}><Send size={16} /><span>Send to chat</span></button>
    </div> : <form className="browser-bar" onSubmit={event => { event.preventDefault(); if (!watching) void navigate(draft ?? '') }}>
      <button type="button" className="browser-icon" aria-label="Back" disabled={!nav.back || watching} onClick={() => tab && void act('browser/history', { tab: tab.id, action: 'back' })}><ArrowLeft size={17} /></button>
      <button type="button" className="browser-icon" aria-label="Forward" disabled={!nav.forward || watching} onClick={() => tab && void act('browser/history', { tab: tab.id, action: 'forward' })}><ArrowRight size={17} /></button>
      <button type="button" className="browser-icon" aria-label="Reload" disabled={!tab || watching} onClick={() => tab && void act('browser/history', { tab: tab.id, action: 'reload' })}><RotateCw size={16} /></button>
      <input ref={urlRef} className="browser-url" aria-label="Address" readOnly={watching} placeholder="Address or port, like 8765" spellCheck={false} autoCapitalize="off" autoCorrect="off" enterKeyHint="go" inputMode="url"
        value={draft ?? shown(tab?.url ?? '')} onFocus={event => { setDraft(shown(tab?.url ?? '')); event.currentTarget.select() }} onBlur={() => setDraft(null)} onChange={event => setDraft(event.target.value)}
        onKeyDown={event => { if (event.key === 'Escape') { setDraft(null); event.currentTarget.blur() } }} />
      <div className="browser-menu-anchor" ref={deviceMenu.ref} onKeyDown={deviceMenu.onKeyDown}>
        <button type="button" className="browser-icon" aria-label={`Device: ${presetLabel}`} title={`Device: ${presetLabel}`} aria-haspopup="menu" aria-expanded={deviceMenu.open} disabled={!tab || watching} onClick={() => deviceMenu.setOpen(!deviceMenu.open)}><DeviceIcon preset={tab?.preset ?? 'desktop'} /></button>
        {deviceMenu.open && <div className="browser-menu end" role="menu">
          {presets.map(item => <button key={item.id} type="button" role="menuitemradio" aria-checked={item.id === tab?.preset} autoFocus={item.id === tab?.preset} onClick={() => void setDevice(item.id)}>
            <DeviceIcon preset={item.id} /><span>{item.label}<small>{item.id === 'desktop' ? 'Fits the panel' : `${item.width} × ${item.height}`}</small></span>{item.id === tab?.preset && <Check size={16} />}
          </button>)}
        </div>}
      </div>
      <button type="button" className="browser-icon browser-touch-only" aria-label="Type into the page" disabled={watching} onClick={() => keysRef.current?.focus()}><Keyboard size={17} /></button>
      <button type="button" className="browser-icon" aria-label="Add notes to this frame" title="Notes" disabled={!frame} onClick={startNotes}><PenLine size={17} /></button>
    </form>}

    {tab?.mode === 'saved' && <p className="browser-saved"><KeyRound size={13} />{savedNote(host)}</p>}
    {tab?.mode === 'agent' && <div className="browser-agent" role="note">
      <Bot size={14} aria-hidden="true" />
      <span>{watching ? `${owner ?? 'An agent'} uses this tab. You watch.` : `You control this tab. ${owner ?? 'The agent'} can still act.`}</span>
      <button type="button" className="browser-text-button" aria-pressed={!watching} disabled={handing} onClick={() => void handOver(watching)}>{handing ? '…' : watching ? 'Take over' : 'Give back'}</button>
    </div>}

    <div className={typing ? 'browser-stage typing' : 'browser-stage'} ref={stageRef} tabIndex={-1} aria-label="Page">
      <div className="browser-screen" style={view}>
        <canvas ref={canvasRef} className="browser-canvas" aria-hidden="true" style={view}
          onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={() => { drag.current = null }} onContextMenu={event => event.preventDefault()}
          onMouseDown={event => event.preventDefault() /* keep focus in the keys box, not the stage */} />
        {noting && <canvas ref={marksRef} className={`browser-marks tool-${tool}`} aria-label="Draw notes on the frame" style={view} onPointerDown={onMarkDown} onPointerMove={onMarkMove} onPointerUp={onMarkUp} onPointerCancel={onMarkUp} />}
        {noting && label && <input className="browser-label" style={{ left: label.left, top: label.top }} aria-label="Note text" autoFocus value={label.text}
          onChange={event => setLabel({ ...label, text: event.target.value })} onBlur={commitLabel}
          onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); commitLabel() } else if (event.key === 'Escape') { event.stopPropagation(); setLabel(null) } }} />}
      </div>
      {!frame && tab && <p className="browser-wait">{live ? 'Loading the page…' : 'Connecting…'}</p>}
      <textarea ref={keysRef} className="browser-keys" aria-label="Keys for the page. Press Escape twice to leave." defaultValue={SENTINEL} autoCapitalize="off" autoCorrect="off" spellCheck={false}
        onFocus={() => setTyping(true)} onBlur={() => setTyping(false)} onKeyDown={onKeyDown} onKeyUp={onKeyUp}
        onInput={event => { if (!(event.nativeEvent as InputEvent).isComposing) syncText() }} onCompositionEnd={syncText}
        onPaste={event => { event.preventDefault(); const text = event.clipboardData.getData('text/plain'); if (text) push({ type: 'text', text: text.slice(0, 20000) }) }} />
    </div>
    <p className="browser-status" role="status">{status || (typing ? 'Keys go to the page. Press Esc twice to leave.' : !live && frame ? 'Connecting again…' : '')}</p>
  </div>
}
