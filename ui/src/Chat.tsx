import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ArrowDown, ArrowLeftRight, Blocks, GitFork, Check, ChevronDown, ChevronRight, Clock3, Columns2, CornerDownRight, Gauge, Info, Layers, Maximize2, Menu, MessageSquare, Minimize2, MoreHorizontal, PanelTop, Pencil, RefreshCw, RotateCcw, Rows2, Search, Settings2, ShieldAlert, SquareTerminal, Target, Terminal, WifiOff, X } from 'lucide-react'
import { AssistantRuntimeProvider, ThreadPrimitive, useExternalStoreRuntime } from '@assistant-ui/react'
import { ApiError, canFork, creditsLabel, errorText, usageLabel } from './api'
import type { Api, Approval, ChatData, ChatMessage, Choice, Footer, Pane } from './api'
import { usePolling } from './hooks'
import type { Anchor } from './hooks'
import { AgentIcon, PlanUsage, agentNames, chatTitle } from './Sidebar'
import { profileName } from './provider'
import { FoundryTag } from './CodexProvider'
import type { Session } from './Sidebar'
import { ModelSheet, MuteItem, Tools, Usage } from './Sheets'
import { ImageViewer } from './ImageViewer'
import type { Media } from './ImageViewer'
import { MediaOpener } from './MediaLink'
import type { Kind } from './MediaLink'
import { TextViewer } from './TextViewer'
import { speak, stopSpeaking } from './speech'
import type { TextFile } from './TextViewer'
import type { ToolSummary } from './api'
import { StatusStack } from './StatusStack'
import { PaneTabs } from './PaneTabs'
import { Elapsed, NoticeStack, formatElapsed } from './Notices'
import type { NoticeVariant } from './Notices'
import { State } from './NewSession'
import { Linked, Message, convertMessage } from './Messages'
import { Composer } from './Composer'
import { ChoiceDock, HeadsUp, taskLabel, SessionInfo, LiveSheet, SearchSheet } from './Dock'
import { LiveTerminal } from './Terminal'
import { WORKSPACE_QUERY } from './workspace'
import { useHost } from './host'


interface ChatProps { liveReplyPreview: boolean; pane: Pane; session: Session | undefined; api: Api; onMenu: () => void; onUpdate: () => void; draft: string; setDraft: (text: string) => void; onOpenPane: (pane: Pane) => void; onSessionActions: (at?: Anchor) => void; onPaneActions: (pane: string, at?: Anchor) => void; onSplit: (direction: 'right' | 'down') => void; onFork: () => void; onReorder: (ids: string[]) => void; micOk: boolean; chime: boolean; readAloud: boolean; handsFree: boolean; slot?: SlotControls; running?: number }
/** Desktop workspace controls for a Chat shown in a slot next to other slots. */
export interface SlotControls { index: number; zoomed: boolean; onZoom: () => void; onClose?: () => void; onPick?: () => void; onDragStart?: (event: React.DragEvent) => void }
let swipeDirection: '' | 'next' | 'previous' = ''
// Where each pane was scrolled, so switching panes and coming back lands at the same place.
const scrollByPane = new Map<string, { top: number; limit: number; bottom: boolean }>()
// Messages sent while the agent works wait in its input queue and reach the transcript only when it takes them.
// Keep them here (per pane, so a pane switch keeps them) and show them as "Queued" until the transcript has them.
// "⏵⏵ auto mode on · 2 shells" → "Auto"; "⏸ plan mode on" → "Plan"; "accept edits on" → "Accept edits".
// Claude's permission modes. Same keys as footer.mode_key on the server.
const MODES: Record<string, string> = { default: 'Manual', accept: 'Accept edits', plan: 'Plan', auto: 'Auto' }
const modeKey = (mode?: string | null) => (['accept', 'plan', 'auto', 'bypass'].find(key => (mode ?? '').toLowerCase().includes(key)) ?? 'default')
// What the terminal is waiting for, as a drawer above the message box: the question, then each real option.
interface Pending { id: number; text: string; key: string; seen: number; at: number; thread?: string | null; failed?: string; offline?: boolean }  // failed: why the send failed; offline: no answer from the server at all
// Queued bubbles survive a reload: Claude already holds the message, so the bubble should stay until it shows in the chat (30 minutes at most).
const PENDING_KEY = 'tether.pending'
const pendingByPane = new Map<string, Pending[]>((() => {
  try {
    const saved = JSON.parse(localStorage.getItem(PENDING_KEY) || '{}') as Record<string, Pending[]>
    return Object.entries(saved).map(([pane, list]) => [pane, list.filter(item => Date.now() - item.at < 1800000)] as [string, Pending[]]).filter(([, list]) => list.length)
  } catch { return [] }
})())
function savePending() {
  try { localStorage.setItem(PENDING_KEY, JSON.stringify(Object.fromEntries([...pendingByPane].filter(([, list]) => list.length)))) } catch { /* storage full or blocked: keep them in memory */ }
}
const squash = (text: string) => text.replace(/\s+/g, ' ').trim()
const matches = (messages: ChatMessage[], key: string) => messages.filter(item => item.role === 'user' && squash(item.text).includes(key)).length
// Panes shown as a live terminal; kept for the visit so switching panes and back keeps the mode.
const terminalPanes = new Set<string>()

// ttyd's page exposes its xterm.js terminal as window.term. Only the parts used here.
const PREVIEW_HOLD_MS = 1600  // longer than one 1.5 s poll, so one stray sample in polling mode stays hidden too
function useSteadyPreview(text: string | undefined) {
  const since = useRef(0)  // when the current run of preview text started; 0 = no preview
  const [, wake] = useState(0)
  if (!text) since.current = 0
  else if (!since.current) since.current = Date.now()
  const wait = since.current ? PREVIEW_HOLD_MS - (Date.now() - since.current) : 0
  useEffect(() => {
    if (wait <= 0) return
    const timer = window.setTimeout(() => wake(n => n + 1), wait)
    return () => window.clearTimeout(timer)
  }, [wait > 0, text])  // eslint-disable-line react-hooks/exhaustive-deps
  return text && wait <= 0 ? text : undefined
}

export function Chat({ liveReplyPreview, pane, session, api, onMenu, onUpdate, draft, setDraft, onOpenPane, onSessionActions, onPaneActions, onSplit, onFork, onReorder, micOk, chime, readAloud, handsFree, slot, running = 0 }: ChatProps) {
  const host = useHost()
  const panelRef = useRef<HTMLElement>(null)
  const [limit, setLimit] = useState(() => scrollByPane.get(pane.id)?.limit ?? 40)
  const chatQuery = `session=${encodeURIComponent(pane.id)}&limit=${limit}&preview=${liveReplyPreview ? 1 : 0}${pane.agent === 'shell' ? '&mode=shell' : ''}`
  const fetchChat = useCallback((signal: AbortSignal) => api<ChatData>(`chat?${chatQuery}${document.visibilityState === 'visible' ? '&watch=1' : ''}`, undefined, signal), [api, chatQuery])
  const { data, error, loading, refresh } = usePolling(fetchChat, 1500, `stream?${chatQuery}`)
  const preview = useSteadyPreview(liveReplyPreview ? data?.preview?.text : undefined)
  // Connection state for the banner: the browser's offline flag, plus failed chat reads while a chat is already on screen.
  const [offline, setOffline] = useState(() => !navigator.onLine)
  const wasDown = useRef(false)
  useEffect(() => {
    const up = () => { setOffline(false); void refresh() }, down = () => setOffline(true)
    window.addEventListener('online', up); window.addEventListener('offline', down)
    return () => { window.removeEventListener('online', up); window.removeEventListener('offline', down) }
  }, [refresh])
  const [sendError, setSendError] = useState('')
  const [draftBlocked, setDraftBlocked] = useState(false)
  const [toast, setToast] = useState('')
  useEffect(() => {
    const down = offline || (!!error && !!data)
    if (down) wasDown.current = true
    else if (wasDown.current && data) { wasDown.current = false; setToast('Reconnected.') }
  }, [offline, error, data])
  const [keysOpen, setKeysOpen] = useState(false)
  const [tools, setTools] = useState(false)
  const [liveSheet, setLiveSheet] = useState<'goal' | 'tasks' | null>(null)
  const [searching, setSearching] = useState(false)
  const target = useRef<string | null>(null)
  const [acting, setActing] = useState(false)
  const [term, setTermState] = useState(() => terminalPanes.has(pane.id))
  // The chat box unmounts while the terminal shows. On the way back, go to the saved place (or the bottom) again.
  const setTerm = (on: boolean) => { if (on) { terminalPanes.add(pane.id); restore.current = scrollByPane.get(pane.id) ?? null } else terminalPanes.delete(pane.id); setTermState(on) }
  const [jump, setJump] = useState(false)
  const viewport = useRef<HTMLDivElement>(null)
  const swipe = useRef<{ x: number; y: number; at: number } | null>(null)
  const panes = session?.panes ?? []
  // Swipe left or right on the chat to go to the next or previous pane. A swipe that starts on something that scrolls
  // sideways (a wide table or code block) scrolls it instead, and a swipe from the screen edge stays Android's back gesture.
  const swipeStart = (event: React.TouchEvent) => {
    swipe.current = null
    hold.current = null
    if (event.touches.length !== 1) return
    const touch = event.touches[0]
    pull.current = viewport.current && viewport.current.scrollTop <= 0 ? touch.clientY : null
    if (touch.clientX < 28 || touch.clientX > window.innerWidth - 28) return
    for (let node = event.target as HTMLElement | null; node && node !== event.currentTarget; node = node.parentElement) {
      if (node.scrollWidth > node.clientWidth + 1 && /auto|scroll/.test(getComputedStyle(node).overflowX)) return
    }
    if (window.getSelection()?.toString()) return
    swipe.current = { x: touch.clientX, y: touch.clientY, at: Date.now() }
  }
  // Pull down at the top of the chat to load earlier messages.
  const pull = useRef<number | null>(null)
  const [pulled, setPulled] = useState(0)
  const pullMove = (event: React.TouchEvent) => {
    if (pull.current === null || !data?.earlier) return
    const distance = event.touches[0].clientY - pull.current
    if (viewport.current && viewport.current.scrollTop > 0) { pull.current = null; setPulled(0); return }
    setPulled(Math.max(0, Math.min(110, distance)))
  }
  const pullEnd = () => {
    if (pull.current !== null && pulled >= 70) loadEarlier()
    pull.current = null; setPulled(0)
  }
  const swipeEnd = (event: React.TouchEvent) => {
    pullEnd()
    const start = swipe.current
    swipe.current = null
    if (!start || Date.now() - start.at > 700) return
    const touch = event.changedTouches[0]
    const dx = touch.clientX - start.x, dy = touch.clientY - start.y
    if (Math.abs(dx) < 70 || Math.abs(dx) < Math.abs(dy) * 2) return
    const index = panes.findIndex(item => item.id === pane.id)
    const next = panes[index + (dx < 0 ? 1 : -1)]
    if (!next) { if (dx > 0 && index <= 0) onMenu(); return }  // right swipe on the first pane opens the sidebar
    swipeDirection = dx < 0 ? 'next' : 'previous'
    onOpenPane(next)
  }
  const [arrival] = useState(() => { const value = swipeDirection; swipeDirection = ''; return value })
  const tabs = useRef<HTMLDivElement>(null)
  useEffect(() => { tabs.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ inline: 'nearest', block: 'nearest' }) }, [pane.id])
  const content = useRef<HTMLDivElement>(null)
  const restore = useRef(scrollByPane.get(pane.id) ?? null)
  const sticky = useRef(restore.current?.bottom ?? true)
  const hold = useRef<{ top: number; until: number } | null>(null)
  const anchor = useRef<{ height: number; top: number } | null>(null)
  const keysRef = useRef<HTMLDivElement>(null)
  // Menus opened from ⋯ hang below it, right-aligned (a popover is 288px wide).
  const menuAnchor = (): Anchor | undefined => { const box = keysRef.current?.getBoundingClientRect(); return box && { x: box.right - 288, y: box.bottom + 4 } }
  const state = data?.state ?? pane.state
  const writable = data?.writable ?? false
  const activePane = { ...pane, agent: data?.agent ?? pane.agent }
  // The name the tab shows: the pane's own name, else the session's (a bare number is not a name).
  const claudeName = pane.pane_name || (session && !/^\d+$/.test(session.name) ? session.name : '')
  const messages = useMemo(() => data?.messages ?? [], [data?.messages])
  const sentPrompts = useMemo(() => messages.filter(message => message.role === 'user' && !message.kind).map(message => message.text), [messages])
  // Read a new reply aloud when its turn ends. Replies already there when the chat opens are not read.
  // Hands-free: when the reading ends, ask the composer to start the mic.
  const [listenRequest, setListenRequest] = useState(0)
  const lastReply = useMemo(() => [...messages].reverse().find(message => message.role === 'assistant' && !message.kind), [messages])
  const readUpTo = useRef<string | null | undefined>(undefined)
  useEffect(() => {
    if (!data) return
    if (readUpTo.current === undefined || !readAloud) { readUpTo.current = lastReply?.id ?? null; return }
    if (state === 'Working' || !lastReply || lastReply.id === readUpTo.current) return
    readUpTo.current = lastReply.id
    if (document.visibilityState !== 'visible') return
    speak(lastReply.text, lastReply.text, handsFree && micOk && writable ? () => setListenRequest(count => count + 1) : undefined)
  }, [data, state, lastReply, readAloud, handsFree, micOk, writable])
  useEffect(() => () => stopSpeaking(), [])
  const [pending, setPendingState] = useState<Pending[]>(() => pendingByPane.get(pane.id) ?? [])
  const setPending = (update: (list: Pending[]) => Pending[]) => setPendingState(list => { const next = update(list); pendingByPane.set(pane.id, next); savePending(); return next })
  const [recheck, setRecheck] = useState(0)
  useEffect(() => {
    if (!data) return
    // Claude writes no chat record for some slash commands (/usage opens a panel). Once the pane is not working, it ran them.
    const ran = (item: Pending) => item.text.trim().startsWith('/') && !item.failed && data.state !== 'Working' && Date.now() - item.at > 3000
    // /clear (and a new chat) starts a new transcript: the count taken in the old one does not apply there.
    const seen = (item: Pending) => item.thread !== undefined && item.thread !== (data.thread ?? null) ? 0 : item.seen
    setPending(list => { const left = list.filter(item => matches(data.messages, item.key) <= seen(item) && Date.now() - item.at < 1800000 && !ran(item)); return left.length === list.length ? list : left })
    // An idle pane sends no new data, so check a slash command again once its 3 s have passed.
    const slash = pending.filter(item => item.text.trim().startsWith('/') && !item.failed).map(item => item.at + 3100 - Date.now())
    if (!slash.length) return
    const timer = window.setTimeout(() => setRecheck(n => n + 1), Math.max(100, Math.min(...slash)))
    return () => window.clearTimeout(timer)
  }, [data, recheck, pending.length]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!toast) return
    const timer = setTimeout(() => setToast(''), 6000)
    return () => clearTimeout(timer)
  }, [toast])
  useEffect(() => {
    if (!keysOpen) return
    const dismiss = (event: MouseEvent) => { if (!keysRef.current?.contains(event.target as Node)) setKeysOpen(false) }
    document.addEventListener('click', dismiss)
    return () => document.removeEventListener('click', dismiss)
  }, [keysOpen])
  useLayoutEffect(() => {
    const view = viewport.current
    if (!view) return
    if (restore.current && data) {
      // Messages render a moment after the data arrives; keep the saved place while the content grows.
      if (!restore.current.bottom) { const top = restore.current.top; hold.current = { top, until: Date.now() + 2000 }; view.scrollTop = top; setJump(true) }
      else view.scrollTop = view.scrollHeight
      restore.current = null
    } else if (anchor.current && data?.limit === limit) {
      view.scrollTop = anchor.current.top + view.scrollHeight - anchor.current.height
      anchor.current = null
    } else if (sticky.current && !anchor.current) view.scrollTop = view.scrollHeight
  }, [data, limit, term])
  // After a search pick: once the message is in the page, center it and flash it.
  useEffect(() => {
    const id = target.current, view = viewport.current
    if (!id || !view) return
    const timer = setTimeout(() => {
      const element = view.querySelector<HTMLElement>(`[data-msg-id="${CSS.escape(id)}"]`)
      if (!element) return
      target.current = null; sticky.current = false; setJump(true)
      element.scrollIntoView({ block: element.offsetHeight > view.clientHeight * .6 ? 'start' : 'center' })  // a tall message starts at the top
      if (element.offsetHeight > view.clientHeight * .6) view.scrollTop -= 16
      hold.current = { top: view.scrollTop, until: Date.now() + 1500 }
      element.classList.add('search-hit')
      setTimeout(() => element.classList.remove('search-hit'), 2400)
    }, 80)
    return () => clearTimeout(timer)
  }, [data])
  useEffect(() => {
    const observer = new ResizeObserver(() => {
      if (hold.current && viewport.current && Date.now() < hold.current.until) viewport.current.scrollTop = hold.current.top
      else if (sticky.current && viewport.current && !anchor.current) viewport.current.scrollTop = viewport.current.scrollHeight
    })
    if (content.current) observer.observe(content.current)
    return () => observer.disconnect()
  }, [])
  const lastFailure = useRef<unknown>(null)
  async function write(path: string, body: object) {
    setSendError(''); setDraftBlocked(false); lastFailure.current = null; cardError.current = false
    try { await api(path, body); await refresh(); onUpdate(); return true }
    catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await refresh(); setToast('The thread changed. Review the chat, then try again.')
      } else { lastFailure.current = error; setSendError(errorText(error)); setDraftBlocked(error instanceof ApiError && error.draft) }
      return false
    }
  }
  // Claude: Up pulls every queued message back into its input; Tether clears that input and returns the text to the message box.
  // Codex has no queue to edit, so the × only hides the bubble there.
  async function unqueue(id: number) {
    if (activePane.agent !== 'claude') { setPending(list => list.filter(other => other.id !== id)); return }
    try {
      const { text } = await api<{ text: string }>('unqueue', { session: pane.id, thread: data?.thread ?? null })
      setPending(list => list.filter(other => other.failed))
      setDraft(draft.trim() ? `${draft.trimEnd()}\n${text}` : text)
      setToast('Cancelled. The queued text is back in your message box.')
    } catch (error) {
      setPending(list => list.filter(other => other.id !== id))
      setToast(`Not cancelled: ${errorText(error)}`)
    }
  }
  // While Claude asks a question, the message box answers it through its own-answer row (checked on the server).
  const ownAnswer = !data?.approval && data?.attention?.reason === 'menu' ? data.attention.options?.find(option => option.free) : undefined
  async function send(text: string, retryOf?: Pending) {
    // Claude shows these as a terminal panel that a phone sees only a slice of. Show Tether's own sheet instead, as T3 Code does.
    if (!retryOf && activePane.agent === 'claude' && /^\/(usage|cost|stats|status)$/.test(text.trim())) { setPlanSheet(true); return true }
    if (ownAnswer && !retryOf) return write('menu', { session: pane.id, thread: data?.thread ?? null, choice: ownAnswer.id, answer: text })
    const key = squash(text).slice(0, 80)
    const seen = retryOf?.seen ?? matches(messages, key)
    const accepted = await write('message', { session: pane.id, thread: data?.thread ?? null, message: text, ...(activePane.agent === 'shell' ? { mode: 'shell' } : {}) })
    const failure = lastFailure.current
    // No answer from the server (offline, timeout, server down): keep the message as a "Not sent" bubble with Retry, and clear the box.
    const lost = !accepted && !!failure && (!(failure instanceof ApiError) || (failure.status >= 500 && !failure.draft))
    if (lost) setSendError('')
    if ((accepted || lost) && activePane.agent !== 'shell' && key) {
      const item = { id: retryOf?.id ?? Date.now(), text, key, seen, thread: retryOf?.thread ?? data?.thread ?? null, at: retryOf?.at ?? Date.now(), ...(lost ? { failed: errorText(failure), offline: !(failure instanceof ApiError) } : {}) }
      setPending(list => retryOf ? list.map(other => other.id === retryOf.id ? item : other) : [...list, item])
    }
    if (accepted || lost) { sticky.current = true; setJump(false); hold.current = null; requestAnimationFrame(() => requestAnimationFrame(() => viewport.current?.scrollTo({ top: viewport.current.scrollHeight }))) }  // after the bubble renders
    return accepted || (lost && activePane.agent !== 'shell')
  }
  // Retry a "Not sent" bubble. First reload the chat: if the message did arrive after all, the bubble just becomes a normal one.
  const [retrying, setRetrying] = useState<number | null>(null)
  async function retry(item: Pending) {
    if (retrying !== null) return
    setRetrying(item.id)
    try {
      const fresh = await api<ChatData>(`chat?session=${encodeURIComponent(pane.id)}&limit=${limit}`).catch(() => null)
      if (fresh && Array.isArray(fresh.messages) && matches(fresh.messages as ChatMessage[], item.key) > item.seen) {
        setPending(list => list.filter(other => other.id !== item.id)); void refresh(); return
      }
      await send(item.text, item)
    } finally { setRetrying(null) }
  }
  function editFailed(item: Pending) {
    setPending(list => list.filter(other => other.id !== item.id))
    setDraft(draft.trim() ? `${draft.trimEnd()}\n${item.text}` : item.text)
  }
  function loadEarlier() {
    if (!data?.earlier || data.limit !== limit) return
    const view = viewport.current
    if (view) anchor.current = { height: view.scrollHeight, top: view.scrollTop }
    setLimit(value => value + 40)
  }
  // Claude's bottom strip, read every 5 seconds while this page is visible.
  const [claudeFooter, setFooter] = useState<Footer>({})
  const footer = activePane.agent === 'codex' ? data?.footer ?? {} : claudeFooter
  const [usageSheet, setUsageSheet] = useState(false)
  const [planSheet, setPlanSheet] = useState(false)
  const [modelSheet, setModelSheet] = useState(false)
  const [modeMenu, setModeMenu] = useState(false)
  const [modeBusy, setModeBusy] = useState('')
  const [info, setInfo] = useState(false)
  // A media path the agent named in a reply. Ask the server first, so a refused or missing file shows its reason, not a broken image.
  const [opened, setOpened] = useState<Media | null>(null)
  const [textFile, setTextFile] = useState<TextFile | null>(null)
  const openMedia = useCallback(async (path: string, kind: Kind, line?: number) => {
    const url = `/mobile/api/file?session=${encodeURIComponent(activePane.id)}&path=${encodeURIComponent(path)}`
    const name = path.split('/').pop() || path
    if (kind === 'text') { setTextFile({ path, url, line }); return }
    try {
      const check = await fetch(url, { headers: { Range: 'bytes=0-0' } })
      if (!check.ok) { const body = await check.json().catch(() => null) as { error?: string } | null; setOpened({ name, url, error: body?.error ?? 'Tether cannot open this file.' }); return }
      void check.body?.cancel()
    } catch { setOpened({ name, url, error: `Cannot reach ${host}.` }); return }
    if (kind === 'pdf') window.open(url, '_blank', 'noopener')
    else setOpened({ name: path, url, kind })
  }, [activePane.id, host])
  // Desktop: "Show in Files" opens the explorer at the file's folder with the file selected. A relative path is in the pane's folder.
  const showInFiles = (path: string | null, close: () => void) => {
    if (!path || typeof matchMedia !== 'function' || !matchMedia(WORKSPACE_QUERY).matches) return undefined
    const full = /^[~/]/.test(path) ? path : `${(activePane.dir || '~').replace(/\/$/, '')}/${path.replace(/^\.\//, '')}`
    return () => { close(); window.dispatchEvent(new CustomEvent('tether:open-files', { detail: { path: full.slice(0, full.lastIndexOf('/')) || '/', select: full } })) }
  }
  const readFooter = useRef<() => void>(() => {})
  useEffect(() => {
    if (activePane.agent !== 'claude') return
    let live = true
    const read = () => { if (document.visibilityState === 'visible') api<Footer>(`footer?session=${encodeURIComponent(pane.id)}`).then(result => { if (live) setFooter(result) }, () => {}) }
    readFooter.current = read
    read()
    const timer = setInterval(read, 5000)
    return () => { live = false; clearInterval(timer) }
  }, [api, pane.id, activePane.agent, state])
  const model = activePane.agent === 'codex' ? footer.model : footer.status?.split('|')[0].trim()
  const context = footer.context ? String(Math.round(footer.context.percent)) : /ctx\s+(\d+)%/.exec(footer.status ?? '')?.[1]
  const currentMode = modeBusy || modeKey(footer.mode)
  async function chooseMode(target: string) {
    setModeMenu(false)
    if (target === modeKey(footer.mode)) return
    setModeBusy(target)
    try { await api('mode', { session: pane.id, thread: data?.thread ?? null, mode: target }) } catch (failure) { setToast(`Mode not changed: ${errorText(failure)}`) }
    readFooter.current(); setModeBusy('')
  }
  const background = (footer.agents ?? []).filter(agent => !agent.main)
  // A card key that Claude did not take leaves its error under the message box. Clear it when the card changes or closes.
  const cardError = useRef(false)
  const card = footer.heads_up ? `${footer.heads_up.state}|${footer.heads_up.text}` : ''
  useEffect(() => { if (cardError.current) { cardError.current = false; setSendError(''); setDraftBlocked(false) } }, [card, pane.id])
  async function sendKey(key: string, card = false) {
    if (acting) return false
    setActing(true); setKeysOpen(false)
    try { return await write('key', { session: pane.id, thread: data?.thread ?? null, key, ...(card ? { card: true } : {}) }) } finally { setActing(false) }
  }
  // Clears the text in the agent's own input box (for example digits that a card did not take).
  async function clearAgentInput() {
    if (acting) return
    setActing(true)
    try { if (await write('clear-draft', { session: pane.id, thread: data?.thread ?? null })) setToast('Cleared the input box in the terminal.') } finally { setActing(false) }
  }
  // An approval goes through the checked route (it confirms the cursor before Enter); a plain menu takes the option's number key.
  async function choose(kind: 'approval' | 'menu', option: Choice, approval: Approval | null, feedback?: string) {
    if (acting) return
    setActing(true)
    try {
      if (kind === 'approval' && approval?.id) await write('approval', { session: pane.id, thread: data?.thread ?? null, approval: approval.id, choice: option.id, feedback })
      // Arrows plus Enter, checked on the screen: a digit key types text when the cursor sits on the own-answer row.
      else await write('menu', { session: pane.id, thread: data?.thread ?? null, choice: option.id, label: option.text })
    } finally { setActing(false) }
  }
  async function start(agent: 'claude' | 'codex') {
    if (acting) return
    setActing(true)
    try { await write('start', { session: pane.id, thread: data?.thread ?? null, agent }) } finally { setActing(false) }
  }
  // Copy, read aloud and share belong to a finished reply: only the last text of each turn gets them, and not while the agent still works.
  const turnEnds = useMemo(() => {
    const ends = new Set<string>()
    let open = state === 'Working'
    for (let at = messages.length - 1; at >= 0; at--) {
      const item = messages[at]
      if (item.role === 'user' && !item.kind) { open = false; continue }
      if (item.kind === 'notice' || item.kind === 'terminal') continue
      if (!open && item.role === 'assistant' && !item.kind) ends.add(item.id)
      open = true
    }
    return ends
  }, [messages, state])
  const runtime = useExternalStoreRuntime({ messages, convertMessage, isRunning: state === 'Working', onNew: async message => { await send(message.content.filter(part => part.type === 'text').map(part => part.text).join('\n')) } })
  // When this phone first saw the pane working. Claude prints its own timer in the spinner detail.
  const [workingSince, setWorkingSince] = useState<{ pane: string; at: number } | null>(null)
  useEffect(() => { setWorkingSince(current => state !== 'Working' ? null : current?.pane === pane.id ? current : { pane: pane.id, at: Date.now() }) }, [state, pane.id])
  // A turn this page watched from its start ends: "Done in 2m 14s" with a check, for a few seconds. Short turns (under 10 s) say nothing.
  const [doneIn, setDoneIn] = useState<{ pane: string; seconds: number } | null>(null)
  const lastRun = useRef<{ pane: string; at: number; ended?: number } | null>(null)
  useEffect(() => {
    if (workingSince) { lastRun.current = { ...workingSince }; setDoneIn(null); return }
    const run = lastRun.current
    if (!run || run.pane !== pane.id) { setDoneIn(null); return }
    run.ended ??= Date.now()
    const left = 6000 - (Date.now() - run.ended)
    if (left <= 0 || run.ended - run.at < 10000) return
    setDoneIn({ pane: pane.id, seconds: (run.ended - run.at) / 1000 })
    const timer = window.setTimeout(() => setDoneIn(null), left)
    return () => window.clearTimeout(timer)
  }, [workingSince, pane.id])
  function startWith(text: string) {
    setDraft(text)
    requestAnimationFrame(() => { const box = panelRef.current?.querySelector<HTMLTextAreaElement>('.composer textarea'); if (box) { box.focus(); box.setSelectionRange(text.length, text.length) } })
  }
  // A hidden notice stays hidden while the same text shows in this pane; a new notice shows again.
  const [hiddenNotices, setHiddenNotices] = useState<string[]>([])
  const visibleNotices = (footer.notices ?? []).filter(note => !hiddenNotices.includes(note))
  const hideNotice = (note: string) => setHiddenNotices(current => [...current.filter(item => (footer.notices ?? []).includes(item)), note])
  return <AssistantRuntimeProvider runtime={runtime}><MediaOpener.Provider value={openMedia}><section ref={panelRef} className="chat-panel" aria-label={`Chat with ${pane.label}`}>
    <header className="chat-header" draggable={!!slot?.onDragStart} onDragStart={slot?.onDragStart}><button className="icon-button mobile-menu" onClick={onMenu} aria-label={running ? `Open sessions. ${running} agents running or waiting` : 'Open sessions'}><Menu />{running > 0 && <span className="menu-count" aria-hidden="true">{running}</span>}</button>{slot && <kbd className="slot-badge" title={`Focus this slot: Alt+${slot.index + 1}`}>⌥{slot.index + 1}</kbd>}<button type="button" className="chat-title" aria-label="Session details" onClick={() => setInfo(true)}><h1>{chatTitle(pane, session)}</h1><div><AgentIcon agent={activePane.agent} icon={activePane.pane_icon} /><span className="agent-word">{agentNames[activePane.agent]}</span>{activePane.agent === 'codex' && pane.codex_profile && <span className="provider-badge" title={`Runs on codex -p ${pane.codex_profile}`}>{profileName(pane.codex_profile)}</span>}<span className="header-separator agent-word">·</span><State state={state} />{context && <><span className="header-separator">·</span><span className={Number(context) >= 80 ? 'context-high' : ''}>{context}%<span className="context-word"> context</span></span></>}</div></button>
      <button type="button" className="icon-button search-toggle" aria-label="Search this chat" title="Search this chat" disabled={!data} onClick={() => setSearching(true)}><Search size={20} /></button>
      <button type="button" className={term ? 'icon-button terminal-toggle active' : 'icon-button terminal-toggle'} aria-label={term ? 'Back to chat' : 'Open terminal'} aria-pressed={term} title={term ? 'Back to chat' : 'Open terminal'} onClick={() => setTerm(!term)}>{term ? <MessageSquare size={20} /> : <SquareTerminal size={20} />}</button><div className="keys-container" ref={keysRef} onKeyDown={event => { if (event.key === 'Escape') { setKeysOpen(false); keysRef.current?.querySelector('button')?.focus() } }}><button className="icon-button" aria-label="Session, pane and keys" aria-expanded={keysOpen} disabled={!data} onClick={() => setKeysOpen(!keysOpen)}><MoreHorizontal /></button>{keysOpen && <div className="keys-menu" aria-label="Session and keys"><button onClick={() => { setKeysOpen(false); onSessionActions(menuAnchor()) }}><Settings2 size={16} />Session options</button><button onClick={() => { setKeysOpen(false); onPaneActions(activePane.id, menuAnchor()) }}><PanelTop size={16} />Pane options</button><MuteItem api={api} pane={activePane.id} />{activePane.agent === 'claude' && data && data.claude_name === null && claudeName && <button disabled={!writable || acting} onClick={() => { setKeysOpen(false); void send(`/rename ${claudeName}`) }}><Pencil size={16} />Name Claude “{claudeName}”</button>}{activePane.agent !== 'shell' && <button onClick={() => { setKeysOpen(false); setTools(true) }}><Blocks size={16} />Skills, plugins, MCP</button>}<button onClick={() => { setKeysOpen(false); onSplit('right') }}><Columns2 size={16} />New pane right<kbd>%</kbd></button><button onClick={() => { setKeysOpen(false); onSplit('down') }}><Rows2 size={16} />New pane below<kbd>"</kbd></button>{canFork(activePane) && <button onClick={() => { setKeysOpen(false); onFork() }}><GitFork size={16} />New chat from this</button>}<span>Send a key</span>{['Escape', 'C-c', 'Enter', 'Up', 'Down', 'Tab', 'y', 'n'].map(key => <button key={key} disabled={!writable || acting} onClick={() => void sendKey(key)}>{key === 'Escape' ? 'Esc' : key === 'C-c' ? 'Ctrl+C' : key}<kbd>{key === 'Up' ? '↑' : key === 'Down' ? '↓' : key === 'Enter' ? '↵' : key === 'Tab' ? '⇥' : key === 'C-c' ? '^C' : key === 'Escape' ? 'esc' : key}</kbd></button>)}</div>}</div>
      {slot?.onPick && <button type="button" className="icon-button slot-button" aria-label={`Change what slot ${slot.index + 1} shows`} title="Show another pane or session here" onClick={slot.onPick}><ArrowLeftRight size={16} /></button>}
      {slot && <button type="button" className="icon-button slot-button" aria-label={slot.zoomed ? 'Show all slots (Alt+Z)' : 'Zoom this slot (Alt+Z)'} title={slot.zoomed ? 'Show all slots (Alt+Z)' : 'Zoom this slot (Alt+Z)'} onClick={slot.onZoom}>{slot.zoomed ? <Minimize2 size={16} /> : <Maximize2 size={16} />}</button>}
      {slot?.onClose && <button type="button" className="icon-button slot-button" aria-label="Close this slot (Alt+X)" title="Close this slot (Alt+X)" onClick={slot.onClose}><X size={16} /></button>}
    </header>
    {session && session.panes.length > 1 && <PaneTabs ref={tabs} session={session} pane={pane} onOpenPane={onOpenPane} onPaneActions={onPaneActions} onSplit={onSplit} onReorder={onReorder} />}
    {term ? <LiveTerminal paneId={activePane.id} onChat={() => setTerm(false)} /> : <>
    {(offline || (error && data)) && <div className="reconnect-banner" role="status"><WifiOff size={15} /><span>{offline ? 'You are offline. Tether reconnects when the network is back.' : `Cannot reach ${host}. Reconnecting…`}</span><button type="button" onClick={() => void refresh()}>Retry now</button></div>}
    <ThreadPrimitive.Root className={arrival ? `thread arrive-${arrival}` : 'thread'}><div className="chat-scroll" ref={viewport} onTouchStart={swipeStart} onTouchMove={pullMove} onTouchEnd={swipeEnd} onTouchCancel={() => { swipe.current = null; pull.current = null; setPulled(0) }} onScroll={() => {
      const view = viewport.current
      if (view && hold.current && Date.now() < hold.current.until && Math.abs(view.scrollTop - hold.current.top) > 2) return
      if (view) { hold.current = null; sticky.current = view.scrollHeight - view.scrollTop - view.clientHeight < 90; setJump(!sticky.current); scrollByPane.set(pane.id, { top: view.scrollTop, limit, bottom: sticky.current }) }
    }}><div className="messages" ref={content}>
      {error && !data && <div className="inline-error" role="alert">{error}<button onClick={() => void refresh()}>Retry</button></div>}
      {loading && <p className="empty-note" role="status">Loading conversation…</p>}
      {pulled > 0 && <p className="pull-note" style={{ height: pulled }} role="status">{pulled >= 70 ? 'Release to load earlier' : 'Pull to load earlier'}</p>}
      {!!data?.earlier && <button className="load-earlier" disabled={data.limit !== limit} onClick={loadEarlier}>{data.limit !== limit ? 'Loading…' : `Load earlier · ${data.earlier}`}</button>}
      {data && !messages.length && <div className="chat-empty"><Terminal size={27} strokeWidth={1.4} /><h2>{activePane.agent === 'shell' ? 'Shell' : 'No messages yet'}</h2><p>{activePane.agent === 'shell' ? 'Start an agent, or type a command below.' : `Send a message to ${pane.label} to get started.`}</p>
        {activePane.agent !== 'shell' && writable && <div className="starter-actions" aria-label="Ways to start">
          <button type="button" onClick={() => startWith('Explain what this project does and how its files are laid out.')}><MessageSquare size={16} />Explain this project</button>
          <button type="button" onClick={() => startWith('!git status --short --branch')}><Terminal size={16} />Check git status</button>
          <button type="button" onClick={() => startWith('/')}><Blocks size={16} />Commands & skills</button>
        </div>}</div>}
      {data && activePane.agent === 'shell' && <div className="start-options"><button disabled={!writable || acting} onClick={() => void start('claude')}>Start Claude<ChevronRight size={16} /></button><button disabled={!writable || acting} onClick={() => void start('codex')}><span>Start Codex<FoundryTag api={api} /></span><ChevronRight size={16} /></button></div>}
      <ThreadPrimitive.Messages>{({ message }) => <Message api={api} session={activePane.id} actions={turnEnds.has(message.id)} tools={message.metadata.custom.tools as ToolSummary[] | undefined} id={message.id} role={message.role} kind={message.metadata.custom.kind} title={message.metadata.custom.title ? String(message.metadata.custom.title) : undefined} count={message.metadata.custom.count} text={String(message.metadata.custom.text ?? '')} time={String(message.metadata.custom.time ?? '')} />}</ThreadPrimitive.Messages>
      {state === 'Working' && preview && <div className="assistant-message reply-preview"><small>typing</small><div>{preview}</div></div>}
    </div></div>{jump && <button className="jump-button icon-button" aria-label="Jump to latest message" onClick={() => { sticky.current = true; setJump(false); viewport.current?.scrollTo({ top: viewport.current.scrollHeight }) }}><ArrowDown size={20} /></button>}</ThreadPrimitive.Root>
    {searching && <SearchSheet api={api} pane={activePane} shell={activePane.agent === 'shell'} onClose={() => setSearching(false)} onPick={(match, maxLimit) => {
      setSearching(false); setTerm(false); target.current = match.id; sticky.current = false; anchor.current = null
      const need = Math.min(maxLimit, Math.max(limit, match.from_end + 10))
      if (need !== limit) setLimit(need); else void refresh()
    }} />}
    {liveSheet && <LiveSheet api={api} pane={activePane} show={liveSheet} onClose={() => setLiveSheet(null)} onTerminal={() => setTerm(true)} />}
    {tools && <Tools api={api} pane={activePane} onClose={() => setTools(false)} onPick={text => { setDraft(text); setTools(false) }} />}
    <StatusStack priorityKey={data?.approval ? `approval:${(data.approval as Approval).id}` : data?.attention ? `attention:${data.attention.id}` : data?.plan ? `plan:${data.plan.id}` : footer.heads_up ? 'heads' : ''} chips={(footer.goal || !!footer.tasks?.length || (footer.suggestion && !draft.trim() && state !== 'Working')) ? <>{(footer.goal || !!footer.tasks?.length) ? <div className="live-chips">{footer.goal && <button type="button" className="live-chip" aria-haspopup="dialog" onClick={() => setLiveSheet('goal')}><Target size={14} />Goal active{footer.goal !== 'active' ? ` · ${footer.goal}` : ''}<ChevronRight size={13} /></button>}{footer.tasks?.map(task => <button type="button" key={task} className="live-chip" aria-haspopup="dialog" onClick={() => setLiveSheet('tasks')}><Layers size={14} />{taskLabel(task)}<ChevronRight size={13} /></button>)}</div> : null}{(footer.suggestion && !draft.trim() && state !== 'Working' ? <button type="button" className="prompt-suggestion" title="Use this suggested prompt" onClick={() => setDraft(footer.suggestion ?? '')}><CornerDownRight size={15} /><span>{footer.suggestion}</span></button> : null)}</> : null} items={[
      { id: 'action', label: data?.plan ? 'Review plan' : data?.approval ? `${agentNames[activePane.agent]} needs permission` : data?.attention?.reason === 'panel' ? `${agentNames[activePane.agent]} panel open` : `${agentNames[activePane.agent]} needs attention`, active: !!(data?.approval || data?.attention || data?.plan), content: <>{data && (data.approval != null || data.attention || data.plan) && <ChoiceDock key={(data.approval as Approval | null)?.id ?? data.attention?.id ?? 'choice'} approval={data.approval as Approval | null} attention={data.attention ?? null} plan={data.plan} agent={agentNames[activePane.agent]} disabled={!writable || acting} onChoose={choose} onKey={key => void sendKey(key)} onType={text => write('type', { session: activePane.id, thread: data?.thread ?? null, text, enter: true })} />}</> },
      { id: 'heads', label: '✦ Heads up', active: !!footer.heads_up, content: <>{footer.heads_up && <HeadsUp card={footer.heads_up} disabled={!writable || acting} onKey={key => sendKey(key, true).then(ok => { readFooter.current(); setTimeout(() => readFooter.current(), 700); if (!ok) { cardError.current = true; throw new Error('not taken') } })} onChat={note => { setDraft(note); requestAnimationFrame(() => { const box = panelRef.current?.querySelector<HTMLTextAreaElement>('.composer textarea'); box?.focus(); box?.setSelectionRange(box.value.length, box.value.length) }) }} />}</> },
      { id: 'agents', label: `${background.length} agents running`, active: background.length > 0, content: <>{background.length > 0 && <div className="agents-strip"><ul>{background.map((agent, index) => <li key={index}><strong>{agent.name}</strong><span>{agent.task}</span><small>{agent.detail}</small></li>)}</ul></div>}</> },
      { id: 'queue', label: `${pending.length} queued${pending.some(item => item.failed) ? ' · Not sent' : ''}`, active: pending.length > 0, content: <>      {pending.map(item => item.failed ? <div key={item.id} className="user-message pending-message failed-message"><span>{item.text}</span><small role="alert">{item.offline ? <><WifiOff size={12} />Not sent</> : <span className="failed-reason">Not sent: {item.failed}</span>}
        <button type="button" className="failed-action" disabled={retrying !== null || !writable} onClick={() => void retry(item)}><RotateCcw size={13} />{retrying === item.id ? 'Sending…' : 'Retry'}</button>
        <button type="button" className="failed-action" onClick={() => editFailed(item)}><Pencil size={13} />Edit</button>
        <button type="button" aria-label="Delete this unsent message" onClick={() => setPending(list => list.filter(other => other.id !== item.id))}><X size={12} /></button></small></div>
      : <div key={item.id} className="user-message pending-message"><span>{item.text}</span><small><Clock3 size={12} />{state === 'Working' ? 'Queued' : 'Sending…'}<button type="button" aria-label={activePane.agent === 'claude' ? 'Cancel this queued message' : 'Hide this queued message'} onClick={() => void unqueue(item.id)}><X size={12} /></button></small></div>)}</> },
      { id: 'update', label: 'Update ready', active: !!footer.update, content: <>{footer.update && <div className="cli-update" role="status"><RefreshCw size={16} /><span><strong>Claude Code update ready</strong><small>{footer.update.replace(/^[^\w/]+\s*/, '')}</small></span><button type="button" className="secondary" disabled={!writable || state === 'Working'} onClick={() => void send('/restart')}>Restart</button></div>}</> },
    ]} />
    {/* Notice cards and the work line, after T3 Code's banner stack and status line (see Notices.tsx). */}
    <NoticeStack items={[
      ...(data && !writable ? [{ id: 'read-only', group: 'environment' as const, variant: 'info' as const, icon: <Info size={15} />, title: 'This pane is read-only.' }] : []),
      ...(toast ? [{ id: 'toast', icon: <Info size={15} />, title: toast, live: true }] : []),
      ...visibleNotices.map(note => ({ id: `notice:${note}`, variant: (/\b(error|failed|denied|limit)\b/i.test(note) ? 'warning' : 'default') as NoticeVariant, icon: /\b(error|failed|denied|limit)\b/i.test(note) ? <ShieldAlert size={15} /> : <Info size={15} />, title: <Linked text={note} />, dismissLabel: `Hide notice: ${note}`, onDismiss: () => hideNotice(note) })),
    ]} />
    {state !== 'Working' && doneIn?.pane === pane.id && <div className="working-line done-line" role="status"><span className="done-check"><Check size={12} strokeWidth={3} /></span><span><strong>Done in {formatElapsed(doneIn.seconds)}</strong></span></div>}
    {state === 'Working' && <div className="working-line" role="status"><span className="state-dot working" /><span><span className="working-head"><strong>{footer.spinner?.verb ?? 'Working…'}</strong>{workingSince && !footer.spinner?.detail && <Elapsed since={workingSince.at} />}</span>{footer.spinner && <small>{footer.spinner.detail}</small>}{footer.running && <span className="running-tool"><Terminal size={13} /><span>{footer.running.title}<code>{footer.running.command}</code></span><small>{footer.running.elapsed}</small></span>}{footer.denied && <span className="running-tool denied-tool"><ShieldAlert size={13} /><span>{footer.denied.text}{footer.denied.reason && <small>{footer.denied.reason}</small>}</span></span>}</span></div>}
    {info && <SessionInfo pane={activePane} footer={footer} mode={MODES[currentMode]} onClose={() => setInfo(false)} />}
    {usageSheet && <Usage footer={footer} onClose={() => setUsageSheet(false)} />}
    {planSheet && <PlanUsage api={api} onClose={() => setPlanSheet(false)} />}
    {activePane.agent === 'codex' && (footer.context || !!footer.windows?.length || creditsLabel(footer.credits)) && <div className="footer-row codex-usage">
      {footer.context && <button type="button" className="pill" aria-haspopup="dialog" onClick={() => setUsageSheet(true)}>Context {context}% used</button>}
      {footer.windows?.map(window => <button type="button" className="pill" key={window.label} aria-haspopup="dialog" onClick={() => setUsageSheet(true)}>{usageLabel(window.label)} {window.percent}% used</button>)}
      {creditsLabel(footer.credits) && <button type="button" className="pill" aria-haspopup="dialog" onClick={() => setUsageSheet(true)}>Credits {creditsLabel(footer.credits)}</button>}
    </div>}
    {modelSheet && <ModelSheet api={api} pane={activePane} thread={data?.thread ?? null} currentLabel={model} currentEffort={footer.effort} onClose={() => setModelSheet(false)} onDone={message => { setModelSheet(false); setToast(message); readFooter.current() }} />}
    <Composer options={activePane.agent === 'shell' ? undefined : <span className="composer-pills">
      <button type="button" className="pill" aria-label="Model and effort" disabled={!writable || state === 'Working'} onClick={() => setModelSheet(true)}><span className="pill-text">{model || 'Model'}</span>{footer.effort && <span className="pill-effort">· {footer.effort}</span>}<ChevronDown size={13} /></button>
      {activePane.agent === 'claude' && footer.status && <span className="mode-container"><button type="button" className="pill" aria-label={`Mode: ${MODES[currentMode]}. Change mode`} aria-expanded={modeMenu} disabled={!writable || !!modeBusy} onClick={() => setModeMenu(!modeMenu)}><Gauge size={13} />{modeBusy ? `${MODES[modeBusy]}…` : MODES[currentMode]}</button>
        {modeMenu && <div className="attach-menu mode-menu" role="menu">{Object.entries(MODES).map(([key, label]) => <button key={key} role="menuitemradio" aria-checked={key === currentMode} type="button" onClick={() => void chooseMode(key)}>{label}{key === currentMode && <Check size={16} />}</button>)}</div>}</span>}
    </span>} api={api} pane={activePane} thread={data?.thread ?? null} disabled={!data || !writable} onSend={send} error={sendError} errorAction={draftBlocked ? <button type="button" disabled={acting} onClick={() => void clearAgentInput()}>Clear {agentNames[activePane.agent]}’s input</button> : null} draft={draft} setDraft={setDraft} working={state === 'Working'} onStop={() => void sendKey(activePane.agent === 'shell' ? 'C-c' : 'Escape')} micOk={micOk} chime={chime} handsFree={handsFree && readAloud} listenRequest={listenRequest} sent={sentPrompts} onTools={() => setTools(true)} hint={ownAnswer ? `Answer ${agentNames[activePane.agent]}’s question` : undefined} />
    </>}
    <ImageViewer image={opened} close={() => setOpened(null)} onShow={showInFiles(opened && !opened.error ? opened.name : null, () => setOpened(null))} />
    <TextViewer file={textFile} close={() => setTextFile(null)} onShow={showInFiles(textFile?.path ?? null, () => setTextFile(null))} />
  </section></MediaOpener.Provider></AssistantRuntimeProvider>
}
