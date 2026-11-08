import { useCallback, useEffect, useRef, useState } from 'react'
import { request } from './api'
import type { GroupBy, SessionGroup, SortBy } from './Organize'
import { DEFAULT_LOOK } from './layouts'
import type { Look, SavedLook } from './layouts'

// seen: when you last looked at each pane (seconds), so a turn that ended later shows as Done.
// SYNCED choices are shared by every device through the server (/mobile/api/prefs). The rest stay in this browser:
// the open pane, the last pane per session, text size and expanded rows depend on the device.
// Sessions are keyed by their tmux id ("$6"), so a rename keeps pins and order.
export interface Prefs {
  autoResume: boolean
  pinned: string[]
  sidePins: SidePin[]
  order: string[]
  collapsed: Record<string, boolean>
  expanded: Record<string, boolean>
  selected: string | null
  lastPane: Record<string, string>
  liveReplyPreview: boolean
  voiceOff: boolean
  // Play a short chime when the mic is ready to hear you.
  voiceSound: boolean
  // Read each new reply aloud when the turn ends (this device only).
  readAloud: boolean
  // After a reply is read, start the mic; a pause in speech sends the message (this device only).
  handsFree: boolean
  // The floating agents bubble (this device only; its place is kept in its own key).
  agentBubble: boolean
  theme: 'system' | 'light' | 'dark'
  scheme: string
  textSize: 'small' | 'default' | 'large'
  showPaths: boolean
  /** Use tmux words in the UI (pane, split, Ctrl-b keys) instead of Tether's (tab). */
  tmuxWords: boolean
  sort: SortBy
  // Sidebar colors: tmux session id ("$6") or pane id ("%12") -> color name.
  colors: Record<string, string>
  // Your own session groups, in display order, and the group of each session (tmux id -> group id).
  groups: SessionGroup[]
  groupOf: Record<string, string>
  groupBy: GroupBy
  seen: Record<string, number>
  // Tether's own order of the pane tabs, per session id. tmux pane order does not change.
  paneOrder: Record<string, string[]>
  // Unsent text per pane. at: when it last changed (seconds); the newest edit wins across devices. '' = cleared.
  drafts: Record<string, Draft>
  // Archived sessions: tmux id -> name when archived. Both must match, so a new session that reuses an old id stays out.
  archived: Record<string, string>
  // Archived tabs: pane id -> thread and dir when archived. Match dir and known threads, so a reused id stays out.
  archivedTabs: Record<string, ArchivedTab>
  // Desktop workspace layout, shared by every desktop. The phone always shows one pane and ignores it.
  // desktop: the sidebar and inbox switches, shared by all sessions (and, before per-session layouts, the one layout).
  // desks: each tmux session's own layout, by session id. active: the session whose layout shows.
  /** The host menu's other Tether servers (synced). url: the server's origin, for example https://mac.example.com. */
  hosts: Host[]
  layout: { desktop?: DesktopLayout; desks?: Record<string, Partial<DesktopLayout>>; active?: string }
  /** Settings › Layout, Style and Backdrop (this device; layouts.ts). myLook: the look saved before a layout replaced it. */
  look: Look
  myLook: SavedLook | null
}
export interface Host { name: string; url: string }
export interface DesktopLayout { count: 1 | 2 | 3 | 4; panes: string[]; focus: number; cols: Record<string, number[]>; row: number; inbox: boolean; sidebar: boolean; /** With 2 slots: one above the other. */ stack?: boolean; /** Panes of other sessions that the user placed in this layout. */ guests?: string[]
  /** The right-hand panel column. Absent in older prefs: no panels open. */ panels?: PanelLayout }
/** Panels in the workspace's right-hand column (panels.ts). terminal: a separate shell; chat: a side chat; tasks and plan follow the focused pane. */
export type PanelKind = 'terminal' | 'changes' | 'browser' | 'files' | 'chat' | 'tasks' | 'plan'
/** One open panel. pane: the shell (terminal) or the chat (chat) it shows. fresh: a + asked for another shell, not the folder's usual one.
 * folder: a terminal opened with the Files screen's "Terminal here" (~/...), instead of the focused pane's folder. */
export interface PanelItem { id: string; kind: PanelKind; pane?: string; fresh?: boolean; folder?: string }
/** One or two columns of panels, each a vertical stack. rows: the boundaries (0..1) between its panels. width: the column's share of the workspace. split: the boundary between two columns. */
export interface PanelLayout { cols: { items: PanelItem[]; rows?: number[] }[]; width?: number; split?: number }
export interface Draft { text: string; at: number }
export interface ArchivedTab { thread: string | null; dir: string }
export interface SidePin { pane: string; note?: string }
const KEY = 'tmux-chat.prefs.v1'
const CLEARED_KEEP = 2 * 86400  // Match CLEARED_KEEP in server/prefs_sync.py (seconds).
const DEFAULTS: Prefs = { autoResume: true, pinned: [], sidePins: [], order: [], collapsed: { past: true }, expanded: {}, selected: null, lastPane: {}, voiceOff: false, voiceSound: true, readAloud: false, handsFree: false, agentBubble: true, liveReplyPreview: true, theme: 'system', scheme: 'claude', textSize: 'default', showPaths: false, tmuxWords: false, sort: 'smart', colors: {}, groups: [], groupOf: {}, groupBy: 'agent', seen: {}, paneOrder: {}, drafts: {}, archived: {}, archivedTabs: {}, hosts: [], layout: {}, look: DEFAULT_LOOK, myLook: null }

function keptDrafts(drafts: Record<string, Draft>) {
  const now = Date.now() / 1000
  return Object.fromEntries(Object.entries(drafts).filter(([, draft]) => draft.text || now - draft.at < CLEARED_KEEP))
}

function load(): Prefs {
  let prefs = DEFAULTS
  try { prefs = { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) ?? '{}') as Partial<Prefs> } } catch { /* keep the defaults */ }
  // Drafts used to live in their own key, as plain text per pane.
  try {
    const old = JSON.parse(localStorage.getItem('tether.drafts') ?? 'null') as Record<string, string> | null
    if (old) {
      const at = Date.now() / 1000
      const moved = Object.fromEntries(Object.entries(old).filter(([pane, text]) => text && !prefs.drafts[pane]).map(([pane, text]) => [pane, { text, at }]))
      prefs = { ...prefs, drafts: { ...moved, ...prefs.drafts } }
      localStorage.setItem(KEY, JSON.stringify(prefs))
      localStorage.removeItem('tether.drafts')
    }
  } catch { /* storage blocked */ }
  return { ...prefs, drafts: keptDrafts(prefs.drafts) }
}

const SYNCED = ['pinned', 'sidePins', 'order', 'paneOrder', 'sort', 'colors', 'groups', 'groupOf', 'groupBy', 'seen', 'collapsed', 'showPaths', 'tmuxWords', 'liveReplyPreview', 'voiceOff', 'voiceSound', 'theme', 'scheme', 'drafts', 'archived', 'archivedTabs', 'hosts', 'layout', 'autoResume'] as const
type Shared = Partial<Pick<Prefs, typeof SYNCED[number]>>
interface Stored { prefs: Shared; version: number }
const PULL_MS = 20000

function store(prefs: Prefs) {
  try { localStorage.setItem(KEY, JSON.stringify(prefs)) } catch { /* private mode: keep it for this visit only */ }
}
const SYNCED_KEY = 'tmux-chat.prefs.synced'
const firstSyncDone = () => { try { return localStorage.getItem(SYNCED_KEY) === '1' } catch { return false } }
const union = (first: string[], second: string[]) => [...first, ...second.filter(item => !first.includes(item))]
// Shared copy first; this device's pins, order and tab orders are added. Other choices take the shared value.
function joined(shared: Shared, local: Prefs): Shared {
  return { sidePins: [...(shared.sidePins ?? []), ...local.sidePins.filter(pin => !shared.sidePins?.some(item => item.pane === pin.pane))],
    pinned: union(shared.pinned ?? [], local.pinned), order: union(shared.order ?? [], local.order),
    paneOrder: { ...local.paneOrder, ...shared.paneOrder }, seen: local.seen, drafts: local.drafts }
}
const pick = (prefs: Prefs) => Object.fromEntries(SYNCED.map(key => [key, prefs[key]])) as Shared

export function usePrefs() {
  const [prefs, setPrefs] = useState<Prefs>(load)
  const latest = useRef(prefs)
  const version = useRef(-1)  // the server copy this device has; -1 = not read yet
  const pending = useRef<Shared>({})  // changed here, not saved yet
  const busy = useRef(false)
  const timer = useRef(0)
  const flushRef = useRef<() => Promise<void>>(async () => {})

  // Take the server copy, but keep keys this device changed and has not saved yet.
  const apply = useCallback((stored: Stored) => {
    version.current = stored.version
    const incoming = Object.fromEntries(Object.entries(stored.prefs).filter(([key]) => !(key in pending.current))) as Shared
    // Drafts: keep the newer copy per pane. A local edit the server has not seen (say, a reload right after typing) goes back up.
    if (incoming.drafts) {
      const local = keptDrafts(latest.current.drafts)
      const merged = { ...incoming.drafts }
      const newer = Object.entries(local).filter(([pane, draft]) =>
        draft.at > (incoming.drafts?.[pane]?.at ?? 0) && (draft.text || pane in incoming.drafts!))
      for (const [pane, draft] of newer) merged[pane] = draft
      if (newer.length > 0) { pending.current.drafts = merged; window.clearTimeout(timer.current); timer.current = window.setTimeout(() => void flushRef.current(), 400) }
      incoming.drafts = merged
    }
    if (!Object.keys(incoming).length) return
    setPrefs(current => {
      const next = { ...current, ...incoming }
      if (JSON.stringify(pick(next)) === JSON.stringify(pick(current))) return current
      store(next)
      latest.current = next
      return next
    })
  }, [])

  const flush = useCallback(async () => {
    if (busy.current || !Object.keys(pending.current).length) return
    busy.current = true
    const changes = pending.current
    pending.current = {}
    if (changes.drafts) changes.drafts = keptDrafts(changes.drafts)
    try { apply(await request<Stored>('prefs', { changes })) } catch { pending.current = { ...changes, ...pending.current } /* offline: try again on the next change or pull */ }
    busy.current = false
    if (Object.keys(pending.current).length) timer.current = window.setTimeout(flush, 2000)
  }, [apply])
  flushRef.current = flush

  const pull = useCallback(async () => {
    if (busy.current) return
    if (Object.keys(pending.current).length) { void flush(); return }
    try {
      const stored = await request<Stored>('prefs')
      if (busy.current || Object.keys(pending.current).length) return
      // A device's first sync adds its own choices to the shared copy, so nothing set before sync is lost.
      if (!firstSyncDone()) {
        pending.current = stored.version === 0 ? pick(latest.current) : joined(stored.prefs, latest.current)
        try { localStorage.setItem(SYNCED_KEY, '1') } catch { /* private mode: merge again next visit, which is harmless */ }
        void flush()
        return
      }
      if (stored.version !== version.current) apply(stored)
    } catch { /* offline: keep this device's copy */ }
  }, [apply, flush])

  useEffect(() => {
    void pull()
    const visible = () => { if (document.visibilityState === 'visible') void pull() }
    const every = window.setInterval(visible, PULL_MS)
    document.addEventListener('visibilitychange', visible)
    window.addEventListener('focus', visible)
    return () => { window.clearInterval(every); window.clearTimeout(timer.current); document.removeEventListener('visibilitychange', visible); window.removeEventListener('focus', visible) }
  }, [pull])

  const update = useCallback((change: (current: Prefs) => Partial<Prefs>) => {
    setPrefs(current => {
      const next = { ...current, ...change(current) }
      store(next)
      latest.current = next
      for (const key of SYNCED) if (JSON.stringify(next[key]) !== JSON.stringify(current[key])) (pending.current as Record<string, unknown>)[key] = next[key]
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(flush, 400)
      return next
    })
  }, [flush])
  return [prefs, update] as const
}
