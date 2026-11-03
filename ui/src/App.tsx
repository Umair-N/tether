import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { LayoutDashboard, Menu, Plus, SquareSplitHorizontal, SquareSplitVertical, Terminal } from 'lucide-react'
import { ApiError, canFork, errorText, request } from './api'
import type { Api, Pane, PastChat, SessionsData } from './api'
import { useModal, usePolling } from './hooks'
import type { Anchor } from './hooks'
import { usePrefs } from './prefs'
import { applyScheme } from './schemes'
import { PlanUsage, Sidebar, groupSessions, isArchived, isDone, needsYou, paneTitle, runningPanes, sortByPrefs, toggleArchive } from './Sidebar'
import type { Session } from './Sidebar'
import { PaneActions, ResumeChat, SessionActions, Settings } from './Sheets'
import type { Place } from './Sheets'
import { ProjectSheet, useProjectMarks } from './Project'
import { orderPanes } from './PaneTabs'
import { NewSession } from './NewSession'
import { Chat } from './Chat'
import type { SlotControls } from './Chat'
import { SlotPicker, Workspace } from './Workspace'
import { Inbox, InboxButton } from './Inbox'
import { inboxItems } from './needs'
import { DRAG_SLOT, WORKSPACE_QUERY, addPanel, addSlot, deskFor, deskKeyFor, hasPanel, newPanelId, panelCols, placePane, removePanel, withDesk, withPanels, resolveSlots, splitSlot, storeSlots, swapSlots } from './workspace'
import { PanelColumn, PanelTools } from './PanelColumn'
import { PANELS, TOOLBAR_PANELS, extraPanels, togglePanelKind } from './panels'
import type { PanelKind, SetPanels } from './panels'
import type { Side } from './workspace'
import type { DesktopLayout } from './prefs'
import { CommandMenu } from './CommandMenu'
import type { Command } from './CommandMenu'
import { Shortcuts } from './Shortcuts'
import { StatusBar } from './StatusBar'
import { toggleTerminal } from './keys'
import { Rail } from './Rail'
import type { Screen } from './Rail'
import { Explorer } from './Explorer'
import { mentionText } from './fileWrites'
import type { FilesTarget } from './Explorer'
import { ExplorerPhone } from './ExplorerPhone'
import { PhoneTabs } from './PhoneTabs'
import { AgentBubble } from './AgentBubble'
import { AgentsPage } from './AgentsView'
import { choosePhoneScreen } from './phoneScreen'
import './App.css'

// One tap turns notifications for this pane off or on. Hidden until this browser has notifications on.

function App() {
  const [unauthorized, setUnauthorized] = useState(false)
  const api: Api = useCallback(async (path, body, signal, method) => {
    try { return await request(path, body, signal, method) } catch (error) {
      if (error instanceof ApiError && error.status === 401) setUnauthorized(true)
      throw error
    }
  }, [])
  const fetchSessions = useCallback((signal: AbortSignal) => api<SessionsData>('sessions', undefined, signal), [api])
  const { data, error, loading, refresh } = usePolling(fetchSessions, 4000)
  const [prefs, update] = usePrefs()
  const [drawer, setDrawer] = useState(false)
  const [newSession, setNewSession] = useState(false)
  const [actions, setActions] = useState<string | null>(null)
  const [actionsDelete, setActionsDelete] = useState(false)
  const [paneActions, setPaneActions] = useState<string | null>(null)
  // Where the actions menu opens on the desktop. Without it (the phone) the actions show as a sheet.
  const [menuAt, setMenuAt] = useState<Anchor>()
  const [resume, setResume] = useState<PastChat | null>(null)
  const [notice, setNotice] = useState('')
  const [settings, setSettings] = useState(false)
  // The Project sheet for a tmux session, with a file from Files to pin when it came from there.
  const [project, setProject] = useState<{ session: string; pin?: string } | null>(null)
  const [planSheet, setPlanSheet] = useState(false)
  // Desktop workspace: the ⌘K command menu or the ? shortcuts sheet.
  const [menu, setMenu] = useState<'commands' | 'shortcuts' | null>(null)
  // Home-screen shortcuts open /chat/?go=attention|new|usage (manifest.webmanifest).
  const [go, setGo] = useState(() => new URL(location.href).searchParams.get('go'))
  // The Agents page (Active and Unread tabs). Tether's summary notification opens it with /chat/?go=agents.
  const [agentsPage, setAgentsPage] = useState(false)
  const markRead = useCallback((ids: string[]) => {
    const now = Date.now() / 1000
    update(current => ({ seen: { ...current.seen, ...Object.fromEntries(ids.map(id => [id, now])) } }))
  }, [update])
  useEffect(() => {
    const root = document.documentElement
    if (prefs.theme === 'system') delete root.dataset.theme; else root.dataset.theme = prefs.theme
    root.dataset.textSize = prefs.textSize
  }, [prefs.theme, prefs.textSize])
  useLayoutEffect(() => applyScheme(prefs.scheme), [prefs.scheme])
  // Unsent drafts are part of the synced prefs: a reload, a pane switch or another device keeps them.
  const reload = () => location.reload()
  const setDraft = (id: string, text: string) => update(current => (current.drafts[id]?.text ?? '') === text ? {} : { drafts: { ...current.drafts, [id]: { text, at: Date.now() / 1000 } } })
  // An installed app on Android resumes instead of reloading. When it comes back to the front, check for a newer build.
  const [updated, setUpdated] = useState(false)
  useEffect(() => {
    const current = document.querySelector<HTMLScriptElement>('script[type="module"][src*="/assets/"]')?.getAttribute('src')
    if (!current) return
    const check = () => {
      if (document.visibilityState !== 'visible') return
      fetch('/chat/', { cache: 'no-store' }).then(response => response.text()).then(html => {
        const latest = /src="([^"]*\/assets\/index-[^"]+\.js)"/.exec(html)?.[1]
        if (latest && latest !== current) setUpdated(true)
      }, () => {})
    }
    document.addEventListener('visibilitychange', check)
    const timer = setInterval(check, 300000)
    return () => { document.removeEventListener('visibilitychange', check); clearInterval(timer) }
  }, [])
  const panes = useMemo(() => data?.sessions ?? [], [data])
  const sessions = useMemo(() => groupSessions(panes).map(item => ({ ...item, panes: orderPanes(item.panes, prefs.paneOrder[item.id]) })), [panes, prefs.paneOrder])
  const pane = panes.find(pane => pane.id === prefs.selected) ?? panes.find(pane => data?.default_thread != null && pane.thread === data.default_thread) ?? sortByPrefs(sessions, prefs.order)[0]?.panes[0]
  // Desktop workspace: on only at desktop width with a mouse or trackpad. The phone keeps one pane.
  const [wide, setWide] = useState(() => typeof matchMedia === 'function' && matchMedia(WORKSPACE_QUERY).matches)
  useEffect(() => {
    const query = matchMedia(WORKSPACE_QUERY)
    const change = () => setWide(query.matches)
    query.addEventListener('change', change)
    return () => query.removeEventListener('change', change)
  }, [])
  const [zoomed, setZoomed] = useState(false)
  // Desktop rail: Chats and Terminals are the workspace (chat or terminal view of the focused slot); Files is the explorer.
  const [screen, setScreen] = useState<Screen>('chats')
  const [filesTarget, setFilesTarget] = useState<FilesTarget | null>(null)
  const files = wide && screen === 'files'
  const phoneFiles = !wide && screen === 'files'
  const chooseScreen = useCallback((next: Screen) => {
    setScreen(next)
    if (next === 'files') return
    setTimeout(() => {
      const toggle = document.querySelector<HTMLButtonElement>('.slot.focused .terminal-toggle')
      if (toggle && (toggle.getAttribute('aria-pressed') === 'true') !== (next === 'terminals')) toggle.click()
    }, 0)
  }, [])
  const screenKeys = useRef<{ files: boolean; choose: (next: Screen) => void } | null>(null)
  useEffect(() => { screenKeys.current = wide ? { files, choose: chooseScreen } : null }, [wide, files, chooseScreen])
  useEffect(() => {
    const open = (event: Event) => { const { path, select } = (event as CustomEvent<Partial<FilesTarget>>).detail ?? {}; if (!path) return; setFilesTarget({ path, select }); setScreen('files') }
    window.addEventListener('tether:open-files', open)
    return () => window.removeEventListener('tether:open-files', open)
  }, [])
  const live = useMemo(() => new Set(panes.map(item => item.id)), [panes])
  const owner = pane ? sessions.find(item => item.id === pane.tmux_session) : undefined
  const [marks, loadMarks] = useProjectMarks(api, panes.length > 0)
  // Files → "Pin to project": the open chat's session gets the file.
  const pinTarget = owner ? { label: `Pin to ${owner.name} project`, run: (path: string) => setProject({ session: owner.id, pin: path }) } : undefined
  // Each tmux session keeps its own layout. Selecting a pane that the current layout shows only moves focus;
  // selecting any other pane opens its own session's layout, so other sessions never land in this split.
  const deskKey = deskKeyFor(prefs.layout, pane?.id, owner?.id, sessions.map(item => item.id))
  const desk = deskFor(prefs.layout, deskKey)
  // Slots show only the layout session's panes, plus panes of other sessions the user placed (desk.guests).
  const deskSession = sessions.find(item => item.id === deskKey) ?? owner
  const sameSession = deskSession ? deskSession.panes.map(item => item.id).filter(id => id !== pane?.id) : []
  const view = wide && data && pane ? resolveSlots(desk, pane.id, live, sameSession) : null
  const slotPanes = view ? view.slots.map(id => panes.find(item => item.id === id)).filter((item): item is Pane => !!item) : []
  const multi = slotPanes.length > 1
  // Desktop "Needs you" list (Inbox.tsx). Its show/hide switch is the shared layout.desktop.inbox pref.
  const desktop = !!view
  const inbox = useMemo(() => desktop ? inboxItems(panes, prefs.seen, pane?.id) : [], [desktop, panes, prefs.seen, pane?.id])
  const stacked = !!desk.stack && slotPanes.length === 2
  // Keep the stored slots in step with what shows, so focus moves and pane picks survive a reload.
  const slotKey = view ? `${view.slots.join(',')}|${view.focus}` : ''
  useEffect(() => {
    if (!slotKey) return
    const [ids, at] = slotKey.split('|')
    const visible = ids.split(','), focus = Number(at)
    update(current => {
      const stored = deskFor(current.layout, deskKey)
      const list = storeSlots(stored.panes, visible)
      // A guest that left the screen is no longer placed here.
      const guests = stored.guests?.filter(id => visible.includes(id))
      if (list.join() === stored.panes.join() && stored.focus === focus && (guests?.length ?? 0) === (stored.guests?.length ?? 0)) return {}
      return { layout: withDesk(current.layout, deskKey, { ...stored, panes: list, focus, guests }) }
    })
  }, [slotKey, deskKey, update])
  const setDesk = (patch: Partial<DesktopLayout>) => update(current => ({ layout: withDesk(current.layout, deskKey, { ...deskFor(current.layout, deskKey), ...patch }) }))
  // The panel column beside the slots (PanelColumn.tsx), saved with this session's layout.
  const setPanels: SetPanels = change => update(current => { const stored = deskFor(current.layout, deskKey); return { layout: withDesk(current.layout, deskKey, { ...stored, ...withPanels(stored, change(stored)) }) } })
  const focusSlot = (index: number) => {
    const target = slotPanes[index]
    if (!view || !target) return
    setZoomed(false)
    update(current => {
      const stored = deskFor(current.layout, deskKey)
      return { selected: target.id, lastPane: { ...current.lastPane, [target.tmux_session]: target.id }, layout: withDesk(current.layout, deskKey, { ...stored, panes: storeSlots(stored.panes, view.slots), focus: index }) }
    })
  }
  const setCount = (count: DesktopLayout['count'], stack = false) => {
    if (!view || !pane) return
    setZoomed(false)
    update(current => {
      const stored = deskFor(current.layout, deskKey)
      const list = storeSlots([...sameSession, ...stored.panes], view.slots)
      const at = Math.max(0, list.indexOf(pane.id))
      if (at >= count) [list[at], list[count - 1]] = [list[count - 1], list[at]]
      return { layout: withDesk(current.layout, deskKey, { ...stored, count, stack, panes: list, focus: Math.min(at, count - 1) }) }
    })
  }
  const closeSlot = (index: number) => {
    if (!view || !pane || view.slots.length < 2) return
    setZoomed(false)
    const out = view.slots[index], rest = view.slots.filter(id => id !== out)
    const next = index === view.focus ? rest[Math.min(index, rest.length - 1)] : pane.id
    update(current => {
      const stored = deskFor(current.layout, deskKey)
      // The closed pane is remembered right after the visible ones, so the next split brings it back.
      const list = [...rest, out, ...stored.panes.filter(id => !view.slots.includes(id))].slice(0, 4)
      return { selected: next, layout: withDesk(current.layout, deskKey, { ...stored, count: rest.length as DesktopLayout['count'], panes: list, focus: rest.indexOf(next) }) }
    })
  }
  const toggleZoom = () => { if (multi || zoomed) setZoomed(!zoomed) }
  /** The layout's guests after the user places pane `id` here: a pane of another session is remembered as placed on purpose. */
  const guestsWith = (stored: DesktopLayout, id: string) => {
    const from = panes.find(item => item.id === id)?.tmux_session
    return from && from !== deskKey && !stored.guests?.includes(id) ? [...(stored.guests ?? []), id] : stored.guests
  }
  // Manual picks: one pane into one slot, or every pane of a session across the slots.
  const showInSlot = (id: string, index: number) => {
    if (!view) return
    setZoomed(false)
    const list = placePane(view.slots, id, index)
    update(current => { const stored = deskFor(current.layout, deskKey); return { selected: id, layout: withDesk(current.layout, deskKey, { ...stored, panes: storeSlots(list, list), focus: index, guests: guestsWith(stored, id) }) } })
  }
  const showSession = (target: Session) => {
    if (!view) return
    setZoomed(false)
    const ids = target.panes.slice(0, 4).map(item => item.id)
    const focus = Math.max(0, ids.indexOf(pane?.id ?? ''))
    update(current => ({ selected: ids[focus], layout: withDesk(current.layout, target.id, { ...deskFor(current.layout, target.id), count: ids.length as DesktopLayout['count'], stack: false, panes: ids, focus }) }))
  }
  const [picking, setPicking] = useState<number | null>(null)
  // Drag and drop: a sidebar pane into a slot (or beside it), a session across the slots, or one slot onto another.
  // A side that does not fit the layout shows the pane in the slot instead.
  const dropPane = (id: string, index: number, side: Side | null) => {
    if (!view || !live.has(id)) return
    const shape = side && splitSlot(view.slots, stacked, index, side, id)
    if (!shape) return showInSlot(id, index)
    setZoomed(false)
    update(current => { const stored = deskFor(current.layout, deskKey); return { selected: id, layout: withDesk(current.layout, deskKey, { ...stored, ...shape, focus: shape.panes.indexOf(id), guests: guestsWith(stored, id) }) } })
  }
  // The "Show in this window" items of the actions menu: each one only when it fits the layout.
  const placeFor = (id: string, all?: Session): Place | undefined => {
    if (!view) return undefined
    const fits = (side: Side) => splitSlot(view.slots, stacked, view.focus, side, id) ? () => dropPane(id, view.focus, side) : undefined
    if (all && all.panes.length > 1) return { all: () => showSession(all) }
    return { here: view.slots[view.focus] === id ? undefined : () => showInSlot(id, view.focus), right: fits('right'), below: fits('bottom') }
  }
  const dropSession = (id: string) => { const target = sessions.find(item => item.id === id); if (target) showSession(target) }
  const swap = (from: number, to: number) => {
    if (!view || !pane) return
    const list = swapSlots(view.slots, from, to)
    update(current => ({ layout: withDesk(current.layout, deskKey, { ...deskFor(current.layout, deskKey), panes: storeSlots(list, list), focus: list.indexOf(pane.id) }) }))
  }
  // The global key handler reads the workspace through this ref, so it does not re-subscribe on each render.
  const workspaceKeys = useRef<{ count: number; focus: number; focusSlot: (index: number) => void; setCount: (count: DesktopLayout['count']) => void; closeSlot: (index: number) => void; toggleZoom: () => void; toggleSidebar: () => void; togglePanel: (kind: PanelKind) => void; sideChat: () => void } | null>(null)
  useEffect(() => {
    workspaceKeys.current = view ? { count: slotPanes.length, focus: view.focus, focusSlot, setCount, closeSlot, toggleZoom, toggleSidebar: () => setDesk({ sidebar: !desk.sidebar }), togglePanel: kind => togglePanelKind(setPanels, kind), sideChat: () => void sideChat() } : null
  })
  useEffect(() => {
    const open = (id: unknown) => {
      if (typeof id !== 'string' || !id) return
      update(() => ({ selected: id }))
      setDrawer(false)
      void refresh()
      const url = new URL(location.href)
      url.searchParams.delete('session')
      history.replaceState(null, '', url)
    }
    open(new URL(location.href).searchParams.get('session'))
    const message = (event: MessageEvent) => { if (event.data?.go) setGo(String(event.data.go)); else open(event.data?.session) }
    if ('serviceWorker' in navigator) navigator.serviceWorker.addEventListener('message', message)
    return () => { if ('serviceWorker' in navigator) navigator.serviceWorker.removeEventListener('message', message) }
  }, [update, refresh])
  useEffect(() => {
    const close = () => {
      if (!pane?.id || document.visibilityState !== 'visible' || !('serviceWorker' in navigator)) return
      void navigator.serviceWorker.getRegistration('/chat/').then(async registration => {
        const notifications = await registration?.getNotifications({ tag: 'session-' + pane.id })
        notifications?.forEach(notification => notification.close())
      }).catch(() => {})
    }
    close()
    document.addEventListener('visibilitychange', close)
    return () => document.removeEventListener('visibilitychange', close)
  }, [pane?.id])
  useEffect(() => {
    if (!go || (go === 'attention' && !data)) return
    const url = new URL(location.href)
    url.searchParams.delete('go')
    history.replaceState(null, '', url)
    setGo(null)
    if (go === 'new') setNewSession(true)
    else if (go === 'usage') setPlanSheet(true)
    else if (go === 'agents') {
      setDrawer(false); setAgentsPage(true)
      void navigator.serviceWorker?.getRegistration('/chat/').then(async registration => (await registration?.getNotifications({ tag: 'tether-summary' }))?.forEach(item => item.close())).catch(() => {})
    }
    else if (go === 'attention') {
      const target = panes.find(item => needsYou(item.state)) ?? panes.find(item => isDone(item, prefs.seen, prefs.selected ?? undefined))
      if (target) update(() => ({ selected: target.id }))
      else setNotice('Nothing needs you right now.')
    }
  }, [go, data, panes, prefs.selected, prefs.seen, update])
  // Mark the panes on screen as seen, again whenever a turn ends while you watch, so only turns you missed show Done.
  const watched = (view && !zoomed ? slotPanes : pane ? [pane] : []).map(item => `${item.id} ${item.finished ?? ''}`).join('|')
  useEffect(() => {
    if (!watched) return
    const now = Date.now() / 1000
    update(current => ({ seen: { ...current.seen, ...Object.fromEntries(watched.split('|').map(item => [item.split(' ')[0], now])) } }))
  }, [watched, update])
  // Desktop keys: Ctrl/Cmd+K searches sessions, Alt+Up/Down opens the session above or below,
  // and typing with nothing focused goes to the message box.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const typing = (event.target as Element | null)?.closest('input, textarea, select, [contenteditable="true"], dialog')
      const slots = workspaceKeys.current
      const rail = screenKeys.current
      // ⌘1–3 (Ctrl off a Mac) pick Chats, Files or Terminals. A browser tab may keep these keys; an installed app window gets them.
      const screenKey = /^Digit([1-3])$/.exec(event.code)
      if (rail && screenKey && (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && !document.querySelector('dialog[open]')) {
        event.preventDefault()
        rail.choose((['chats', 'files', 'terminals'] as const)[Number(screenKey[1]) - 1])
        return
      }
      // The Files screen handles its own keys; the hidden workspace must not take them.
      if (rail?.files) return
      if (slots && !document.querySelector('dialog[open]')) {
        // Browsers keep Cmd+1-9, Cmd+W and Cmd+N, so slot keys use Alt. Read event.code: Alt+letter types a symbol on a Mac.
        const mod = event.ctrlKey || event.metaKey
        const act = (run: () => void) => { event.preventDefault(); run() }
        if (mod && !event.altKey && event.code === 'Backslash') return act(() => slots.setCount(event.shiftKey ? 4 : Math.min(3, Math.max(2, slots.count + 1)) as DesktopLayout['count']))
        if (mod && !event.altKey && !event.shiftKey && event.code === 'KeyB') return act(slots.toggleSidebar)
        if (mod && !event.altKey && !event.shiftKey && event.code === 'Period') return act(() => slots.togglePanel('terminal'))
        if (mod && !event.altKey && !event.shiftKey && event.code === 'Semicolon') return act(slots.sideChat)
        if (event.altKey && !mod && !event.shiftKey) {
          const digit = /^Digit([1-4])$/.exec(event.code)
          // Leave the old slot's message box, so typing goes to the newly focused slot.
          if (digit && Number(digit[1]) <= slots.count) return act(() => { (document.activeElement as HTMLElement | null)?.blur(); slots.focusSlot(Number(digit[1]) - 1) })
          if (event.code === 'KeyZ') return act(slots.toggleZoom)
          if (event.code === 'KeyX' && slots.count > 1) return act(() => slots.closeSlot(slots.focus))
          if (event.code === 'KeyT') return act(toggleTerminal)
          if (event.code === 'KeyN') return act(() => setNewSession(true))
        }
        if (event.key === '?' && !typing && !mod && !event.altKey) return act(() => setMenu('shortcuts'))
      }
      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        // The workspace opens the command menu; ⌘K again closes it. The phone keeps the sidebar search.
        if (slots) { setMenu(current => current === 'commands' ? null : document.querySelector('dialog[open]') ? current : 'commands'); return }
        const input = document.querySelector<HTMLInputElement>('.sidebar .sidebar-search input')
        if (input?.offsetParent) input.select()
        else { setDrawer(true); setTimeout(() => document.querySelector<HTMLInputElement>('.drawer .sidebar-search input')?.focus(), 50) }
        return
      }
      if (event.altKey && !event.ctrlKey && !event.metaKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown') && !document.querySelector('dialog[open]')) {
        // Every row that opens a pane: one-pane sessions and the pane rows under a session with several panes.
        const rows = [...document.querySelectorAll<HTMLButtonElement>('.sidebar .session-groups .session-row:not(.session-head) .session-main, .sidebar .session-groups .pane-children button')].filter(row => row.offsetParent)
        const at = rows.findIndex(row => row.getAttribute('aria-current') === 'page')
        const next = rows[at < 0 ? 0 : at + (event.key === 'ArrowDown' ? 1 : -1)]
        if (next) { event.preventDefault(); next.click(); next.scrollIntoView({ block: 'nearest' }) }
        return
      }
      if (!typing && event.key.length === 1 && event.key !== ' ' && !event.ctrlKey && !event.metaKey && !event.altKey && !document.querySelector('dialog[open]')) {
        const box = document.querySelector<HTMLTextAreaElement>('.slot.focused form.composer textarea') ?? document.querySelector<HTMLTextAreaElement>('form.composer textarea')
        if (box && !box.disabled && !box.readOnly) box.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const drawerModal = useModal(drawer, () => setDrawer(false))
  const drawerTouch = useRef<{ x: number; y: number } | null>(null)
  useEffect(() => { if (!notice) return; const timer = setTimeout(() => setNotice(''), 5000); return () => clearTimeout(timer) }, [notice])
  // Files explorer → chat: "Add to message" puts @paths in the active chat's draft; "Terminal here" opens a side shell at a folder.
  const addToMessage = (paths: string[]) => {
    if (!pane || !paths.length) return setNotice('Open a chat first. Then add files to its message.')
    const old = prefs.drafts[pane.id]?.text ?? ''
    setDraft(pane.id, old + (old && !/\s$/.test(old) ? ' ' : '') + mentionText(paths))
    chooseScreen('chats')
    setNotice(`Added ${paths.length === 1 ? paths[0] : `${paths.length} items`} to the message for ${paneTitle(pane)}.`)
    setTimeout(() => {
      const box = document.querySelector<HTMLTextAreaElement>('.slot.focused form.composer textarea') ?? document.querySelector<HTMLTextAreaElement>('form.composer textarea')
      if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length) }
    }, 50)
  }
  const terminalHere = (folder: string) => {
    if (!view) return setNotice('Open a chat first. The terminal opens in the panel beside it.')
    setPanels(current => ({ cols: addPanel(panelCols(current), { id: newPanelId(), kind: 'terminal', folder, fresh: true }) }))
    chooseScreen('chats')
  }
  const openPane = (target: Pane) => {
    update(current => ({ selected: target.id, lastPane: { ...current.lastPane, [target.tmux_session]: target.id } }))
    setDrawer(false)
  }
  const openById = (id: string) => { update(() => ({ selected: id })); setDrawer(false); void refresh() }
  // A new tmux pane. On the desktop workspace it opens in a new slot beside the focused one, on the same side as the tmux split.
  // The list is fetched first so the slot keeps it.
  const openNewPane = async (id: string, direction: 'right' | 'down' = 'right') => {
    setDrawer(false)
    if (!view) return openById(id)
    await refresh()
    const shape = splitSlot(view.slots, stacked, view.focus, direction === 'down' ? 'bottom' : 'right', id)
    const next = shape ? { ...shape, focus: shape.panes.indexOf(id) } : { ...addSlot(view.slots, view.focus, id), stack: false }
    setZoomed(false)
    update(current => ({ selected: id, layout: withDesk(current.layout, deskKey, { ...deskFor(current.layout, deskKey), ...next }) }))
  }
  async function split(direction: 'right' | 'down', target = pane) {
    if (!target) return
    try { await openNewPane((await api<{ pane: string }>('split', { session: target.id, direction })).pane, direction) } catch (error) { setNotice(errorText(error)) }
  }
  // A copy of the chat with its full history, in a new window of the same tmux session (claude --fork-session, codex fork).
  async function fork(target: Pane) {
    try {
      const result = await api<{ pane: string; name: string }>('fork', { session: target.id })
      await openNewPane(result.pane, 'right')
      setNotice(`Opened “${result.name}” with this chat’s history.`)
    } catch (error) { setNotice(errorText(error)) }
  }
  // ⌘; and ⋯ › Side chat: a fork of the focused chat, shown in the panel column instead of a slot. Again closes the panel; the fork keeps running.
  async function sideChat() {
    if (!view || !pane) return
    if (hasPanel(panelCols(desk), 'chat')) return setPanels(current => ({ cols: panelCols(current).flatMap(col => col.items).filter(item => item.kind === 'chat').reduce((out, item) => removePanel(out, item.id), panelCols(current)) }))
    if (!canFork(pane)) return setNotice('A side chat needs a Claude or Codex chat with a first message.')
    try {
      const result = await api<{ pane: string; name: string }>('fork', { session: pane.id })
      await refresh()
      setPanels(current => ({ cols: addPanel(panelCols(current), { id: newPanelId(), kind: 'chat', pane: result.pane }) }))
      setNotice(`Opened “${result.name}” beside this chat, with its history.`)
    } catch (error) { setNotice(errorText(error)) }
  }
  const actionSession = sessions.find(item => item.id === actions)
  const ordered = sortByPrefs(sessions, prefs.order).map(item => item.id)
  // Sessions a pane or a session can move into from the ⋯ menus (the keyboard path for drag to merge).
  const moveTargets = sortByPrefs(sessions, prefs.order).filter(item => !isArchived(item, prefs.archived))
  const move = (id: string, step: -1 | 1) => update(current => {
    const order = sortByPrefs(sessions, current.order).map(item => item.id)
    const at = order.indexOf(id), to = at + step
    if (at < 0 || to < 0 || to >= order.length) return {}
    ;[order[at], order[to]] = [order[to], order[at]]
    return { order }
  })
  const pickPastChat = (chat: PastChat) => {
    const live = chat.live ? panes.find(item => item.thread === chat.id) : undefined
    if (live) openPane(live)
    else { setDrawer(false); setResume(chat) }
  }
  const sidebarProps = { api, sessions, marks, onMarks: loadMarks, selected: pane?.id, slots: multi ? view?.slots : undefined, drag: !!view, prefs, update, loading, error, onRetry: () => void refresh(), onOpen: openPane, onNew: () => { setDrawer(false); setNewSession(true) }, onActions: (item: Session, remove?: boolean, at?: Anchor) => { if (remove) setDrawer(false); setMenuAt(view ? at : undefined); setActionsDelete(!!remove); setActions(item.id) }, onPaneActions: (item: Pane, at?: Anchor) => { setDrawer(false); setMenuAt(view ? at : undefined); setPaneActions(item.id) }, onPastChat: pickPastChat, onSettings: () => { setDrawer(false); setSettings(true) }, onCommandMenu: view ? () => setMenu('commands') : undefined }
  // The command menu runs the same actions as the keys. A command shows only when it would do something now.
  const commands: Command[] = view ? [
    ...(slotPanes.length < 3 ? [{ label: 'Split right', key: '⌘\\', run: () => setCount(Math.max(2, slotPanes.length + 1) as DesktopLayout['count']) }] : []),
    ...(slotPanes.length < 4 ? [{ label: 'Grid of four', key: '⌘⇧\\', run: () => setCount(4) }] : []),
    ...(multi || zoomed ? [{ label: zoomed ? 'Restore the split' : 'Zoom the focused slot', key: '⌥Z', run: toggleZoom }] : []),
    ...(multi ? [{ label: 'Close the focused slot', key: '⌥X', run: () => closeSlot(view.focus) }] : []),
    { label: 'Terminal view of the focused pane', key: '⌥T', run: toggleTerminal },
    ...(pane ? [...TOOLBAR_PANELS.map(({ kind, key }) => ({ label: `${hasPanel(panelCols(desk), kind) ? 'Hide' : 'Show'} the ${PANELS[kind].title.toLowerCase()} panel`, key, run: () => togglePanelKind(setPanels, kind) })),
      ...extraPanels(desk, pane).map(({ kind, key, open }) => ({ label: open ? `Hide the ${PANELS[kind].title.toLowerCase()} panel` : kind === 'chat' ? 'Side chat: fork this chat beside it' : `Show ${PANELS[kind].title.toLowerCase()}`, key, run: () => kind === 'chat' ? void sideChat() : togglePanelKind(setPanels, kind) }))] : []),
    { label: 'New session', key: '⌥N', run: () => setNewSession(true) },
    { label: desk.sidebar ? 'Hide the sidebar' : 'Show the sidebar', key: '⌘B', run: () => setDesk({ sidebar: !desk.sidebar }) },
    { label: 'Keyboard shortcuts', key: '?', run: () => setMenu('shortcuts') },
    { label: 'Plan usage', run: () => setPlanSheet(true) },
  ] : []
  const running = runningPanes(sessions).length
  const chatFor = (target: Pane, slot?: SlotControls) => {
    const owner = sessions.find(item => item.id === target.tmux_session)
    return <Chat liveReplyPreview={prefs.liveReplyPreview} key={target.id} pane={target} session={owner} api={api} onMenu={() => setDrawer(true)} onUpdate={() => void refresh()} draft={prefs.drafts[target.id]?.text ?? ''} setDraft={text => setDraft(target.id, text)} onOpenPane={openPane} onSessionActions={at => { if (owner) { setMenuAt(view ? at : undefined); setActionsDelete(false); setActions(owner.id) } }} onPaneActions={(id, at) => { setMenuAt(view ? at : undefined); setPaneActions(id) }} onSplit={direction => void split(direction, target)} onFork={() => void fork(target)} onReorder={ids => { if (owner) update(current => ({ paneOrder: { ...current.paneOrder, [owner.id]: ids } })) }} micOk={!prefs.voiceOff} chime={prefs.voiceSound} readAloud={prefs.readAloud} handsFree={prefs.handsFree} slot={slot} running={running} />
  }
  if (unauthorized) return <main className="signin"><Terminal size={32} strokeWidth={1.4} /><h1>Sign in</h1><p>Sign in to access your terminal sessions.</p><a className="primary" href="/mobile/login?next=/chat/">Sign in</a></main>
  return <main className={['app-shell', view && !desk.sidebar && 'no-sidebar', wide && 'with-rail', files && 'screen-files', !wide && 'phone-tabs', phoneFiles && 'phone-files'].filter(Boolean).join(' ')}>{wide && <Rail screen={screen} onScreen={chooseScreen} />}<aside className="sidebar"><Sidebar {...sidebarProps} /></aside><dialog {...drawerModal} className="drawer" aria-label="Sessions" onClick={event => { const box = event.currentTarget.getBoundingClientRect(); if (event.target === event.currentTarget && (event.clientX > box.right || event.clientX < box.left || event.clientY < box.top || event.clientY > box.bottom)) setDrawer(false) }} onTouchStart={event => { drawerTouch.current = (event.target as Element).closest('.swipe-row') ? null : { x: event.touches[0].clientX, y: event.touches[0].clientY } }} onTouchEnd={event => { const start = drawerTouch.current; drawerTouch.current = null; if (!start) return; const dx = event.changedTouches[0].clientX - start.x, dy = event.changedTouches[0].clientY - start.y; if (dx < -60 && Math.abs(dy) < Math.abs(dx) * 0.6) setDrawer(false) }}>{drawer && <Sidebar {...sidebarProps} onClose={() => setDrawer(false)} />}</dialog>
    {pane ? (view ? <Workspace layout={desk} slots={slotPanes} focus={view.focus} zoomed={zoomed && multi} onCount={setCount} onFocus={focusSlot} onLayout={setDesk}
      renderSlot={(target, index, shown) => chatFor(target, shown > 1 || zoomed ? { index, zoomed, onZoom: toggleZoom, onClose: zoomed ? undefined : () => closeSlot(index), onPick: () => setPicking(index), onDragStart: event => { event.dataTransfer.setData(DRAG_SLOT, String(index)); event.dataTransfer.effectAllowed = 'move' } } : undefined)}
      onDropPane={dropPane} onDropSession={dropSession} onSwap={swap}
      tools={<>
        {owner && owner.panes.length > 1 && <button type="button" className="icon-button" aria-label={`Show all panes of ${owner.name}`} title={`Show all panes of ${owner.name}`} onClick={() => showSession(owner)}><LayoutDashboard size={17} /></button>}
        <button type="button" className="icon-button" aria-label="New pane right (tmux)" title="New pane right (tmux split, opens in a new slot)" onClick={() => void split('right')}><SquareSplitHorizontal size={17} /></button>
        <button type="button" className="icon-button" aria-label="New pane below (tmux)" title="New pane below (tmux split, opens in a new slot)" onClick={() => void split('down')}><SquareSplitVertical size={17} /></button>
        <InboxButton count={inbox.length} open={desk.inbox} onToggle={() => setDesk({ inbox: !desk.inbox })} />
      </>}
      panelTools={<PanelTools desk={desk} pane={pane} setPanels={setPanels} onSideChat={() => void sideChat()} />}
      panels={<PanelColumn api={api} desk={desk} pane={pane} setPanels={setPanels} renderChat={id => { const target = panes.find(item => item.id === id); return target ? chatFor(target) : null }} />} /> : chatFor(pane)) : <section className="no-session"><header><button className="icon-button mobile-menu" aria-label="Open sessions" onClick={() => setDrawer(true)}><Menu /></button></header><div><Terminal size={32} strokeWidth={1.4} /><h1>{loading ? 'Finding your sessions…' : 'Space for your next task.'}</h1><p>{error || 'Open a session, or start something new.'}</p><button className="primary" onClick={() => setNewSession(true)}><Plus size={18} />New session</button></div></section>}
    {files && <Explorer api={api} panes={panes} pane={pane} target={filesTarget} onTarget={() => setFilesTarget(null)} onAddToMessage={addToMessage} onTerminalHere={terminalHere} onPin={pinTarget} />}
    {phoneFiles && <ExplorerPhone api={api} target={filesTarget?.path ?? null} onTarget={() => setFilesTarget(null)} onPin={pinTarget} chatName={pane ? (pane.pane_name || owner?.name || pane.label) : undefined}
      onAddToChat={pane ? paths => {
        // "Add to chat": append @~/path to the open chat's unsent draft, then go back to the chat.
        const before = prefs.drafts[pane.id]?.text ?? ''
        setDraft(pane.id, before + (before && !/\s$/.test(before) ? ' ' : '') + mentionText(paths))
        choosePhoneScreen('chats', setScreen)
        setNotice(paths.length === 1 ? `Added ${paths[0]} to the message.` : `Added ${paths.length} paths to the message.`)
      } : undefined} />}
    {prefs.agentBubble && sessions.some(item => item.panes.some(p => p.agent !== 'shell')) && <AgentBubble sessions={sessions} seen={prefs.seen} selected={pane?.id} onOpen={openPane} onMarkRead={markRead} onPage={() => setAgentsPage(true)} onHide={() => { update(() => ({ agentBubble: false })); setNotice('Agents bubble hidden. Turn it on in Settings › Chat.') }} />}
    {!wide && <PhoneTabs screen={screen} onScreen={next => choosePhoneScreen(next, setScreen)} setScreen={setScreen} terminals={!!pane} />}
    {view && <Inbox api={api} items={inbox} sessions={sessions} selected={pane?.id} open={desk.inbox} onToggle={() => setDesk({ inbox: !desk.inbox })} onOpen={openPane} onUpdate={() => void refresh()} />}
    {view && picking !== null && picking < view.slots.length && <SlotPicker index={picking} sessions={sortByPrefs(sessions, prefs.order).filter(item => !isArchived(item, prefs.archived))} slots={view.slots} onPane={id => { showInSlot(id, picking); setPicking(null) }} onSession={target => { showSession(target); setPicking(null) }} onClose={() => setPicking(null)} />}
    {project && sessions.some(item => item.id === project.session) && <ProjectSheet key={project.session} api={api} session={sessions.find(item => item.id === project.session)!} mark={marks[project.session]} pin={project.pin} onChanged={loadMarks} onClose={() => setProject(null)} />}
    {settings && <Settings api={api} prefs={prefs} update={update} onReload={reload} onClose={() => setSettings(false)} />}
    {newSession && <NewSession api={api} onClose={() => setNewSession(false)} onCreated={created => { openPane(created); setNewSession(false); void refresh() }} />}
    {actionSession && pane && <SessionActions key={`${actionSession.id}-${actionsDelete}`} startDelete={actionsDelete} at={actionsDelete ? undefined : menuAt} place={placeFor(actionSession.panes[0].id, actionSession)} api={api} session={actionSession} pane={actionSession.panes.find(item => item.id === pane.id)?.id ?? actionSession.panes[0].id} pinned={prefs.pinned.includes(actionSession.id)} archived={isArchived(actionSession, prefs.archived)} canMoveUp={ordered.indexOf(actionSession.id) > 0} canMoveDown={ordered.indexOf(actionSession.id) < ordered.length - 1} targets={moveTargets} marks={marks} onMarks={loadMarks}
      onPin={() => update(current => ({ pinned: current.pinned.includes(actionSession.id) ? current.pinned.filter(id => id !== actionSession.id) : [...current.pinned, actionSession.id] }))}
      onArchive={() => update(toggleArchive(actionSession))}
      onMove={step => move(actionSession.id, step)} onProject={() => { setActions(null); setProject({ session: actionSession.id }) }} onClose={() => setActions(null)} onDone={(newPane, direction) => { setActions(null); if (newPane) void openNewPane(newPane, direction); else void refresh() }} />}
    {paneActions && panes.find(item => item.id === paneActions) && (() => { const target = panes.find(item => item.id === paneActions)!; return <PaneActions api={api} pane={target} at={menuAt} place={placeFor(target.id)} session={sessions.find(item => item.id === target.tmux_session)} targets={moveTargets} onClose={() => setPaneActions(null)} onFork={() => { setPaneActions(null); void fork(target) }} onDone={(newPane, direction) => { setPaneActions(null); if (newPane) void openNewPane(newPane, direction); else void refresh() }} /> })()}
    {resume && <ResumeChat api={api} chat={resume} onClose={() => setResume(null)} onOpened={id => { setResume(null); openById(id) }} />}
    {notice && <div className="toast app-toast" role="status">{notice}</div>}
    {planSheet && <PlanUsage api={api} onClose={() => setPlanSheet(false)} />}
    {view && menu === 'commands' && <CommandMenu sessions={sortByPrefs(sessions, prefs.order).filter(item => !isArchived(item, prefs.archived))} seen={prefs.seen} selected={pane?.id} slots={multi ? view.slots : []} commands={commands} onPane={openPane} onSession={showSession} onClose={() => setMenu(null)} />}
    {view && menu === 'shortcuts' && <Shortcuts onClose={() => setMenu(null)} />}
    {agentsPage && <AgentsPage sessions={sessions} seen={prefs.seen} selected={pane?.id} onOpen={openPane} onMarkRead={markRead} onClose={() => setAgentsPage(false)} />}
    {view && !files && <StatusBar panes={panes} loading={loading} error={error} />}
    {updated && <div className="update-banner" role="status"><span>A new version of Tether is ready.</span><button type="button" onClick={reload}>Reload</button></div>}
  </main>
}
export default App
