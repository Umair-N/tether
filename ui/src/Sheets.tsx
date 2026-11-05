import { playChime, prepareChime } from './sound'
import { useWords, count } from './words'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Archive, ArchiveRestore, ArrowDown, BookMarked, ArrowUp, Bell, Check, Columns2, GitFork, History, LayoutDashboard, Merge, ArrowRightLeft, Plus, PanelBottom, PanelRight, Pencil, Pin, PinOff, Shapes, Sparkles, RotateCw, Rows2, Square, Trash2, X } from 'lucide-react'
import { SESSION_NAME, SESSION_NAME_HELP, canFork, creditsLabel, errorText, usageLabel } from './api'
import type { Api, Catalog, Footer, ModelOptions, Pane, PastChat, SessionsData } from './api'
import { loadCatalog } from './Suggest'
import type { Prefs } from './prefs'
import { useModal, usePopover, usePolling } from './hooks'
import type { Anchor } from './hooks'
import { AgentIcon, PANE_ICONS, agentNames, paneTitle, shownIndex } from './Sidebar'
import type { Session } from './Sidebar'
import { canSpeak } from './speech'
import { KeepChoice, keepNeeded } from './Project'
import type { Keep, ProjectMarks } from './Project'
import { SCHEMES, schemeById } from './schemes'
import { HostResources } from './HostResources'
import { GlobalCodexLevel } from './CodexLevel'
import { AgentBrowserSettings } from './AgentBrowser'

type PaneLevel = 'all' | 'important' | 'urgent' | 'off'
type Delivery = 'sound' | 'silent' | 'off'
type Priority = 'urgent' | 'important' | 'low'
interface PushConfig { supported: boolean; publicKey: string; muted?: string[]; levels?: Record<string, PaneLevel>; delivery?: Record<Priority, Delivery> }
const PANE_LEVELS: [PaneLevel, string][] = [['all', 'All'], ['important', 'Important and urgent'], ['urgent', 'Urgent only'], ['off', 'Off']]
const PRIORITIES: [Priority, string][] = [['urgent', 'Urgent — approvals, questions, errors'], ['important', 'Important — tasks you started'], ['low', 'Low — background updates']]

// Pane notification submenu. Hidden when this browser has no push subscription.
export function MuteItem({ api, pane, disabled = false }: { api: Api; pane: string; disabled?: boolean }) {
  const [level, setLevel] = useState<PaneLevel | null>(null)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return
    let live = true
    Promise.all([navigator.serviceWorker.ready.then(registration => registration.pushManager.getSubscription()), api<PushConfig>('push/config')])
      .then(([subscription, config]) => { if (live) setLevel(subscription ? config.levels?.[pane] ?? ((config.muted ?? []).includes(pane) ? 'off' : 'all') : null) }, () => {})
    return () => { live = false }
  }, [api, pane])
  if (level === null) return null
  async function choose(next: PaneLevel) {
    setBusy(true); setError('')
    try { const result = await api<{ levels: Record<string, PaneLevel> }>('push/level', { session: pane, level: next }); setLevel(result.levels[pane] ?? 'all') }
    catch (failure) { setError(errorText(failure)) } finally { setBusy(false) }
  }
  return <>
    <button type="button" aria-expanded={open} disabled={disabled || busy} onClick={() => setOpen(!open)}><Bell size={16} />Notifications</button>
    {open && <div className="notification-levels" role="group" aria-label="Pane notifications">
      {PANE_LEVELS.map(([value, label]) => <button type="button" key={value} aria-pressed={level === value} disabled={disabled || busy} onClick={() => void choose(value)}><span>{label}</span>{level === value && <Check size={16} />}</button>)}
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
  </>
}


export function Usage({ footer, onClose }: { footer: Footer; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const context = footer.context
  const credits = creditsLabel(footer.credits)
  return <dialog {...modal} className="sheet" aria-labelledby="usage-title">
    <div className="sheet-heading"><h2 id="usage-title">Usage</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    {footer.model && <p className="sheet-sub">{footer.model}{footer.effort && ` · ${footer.effort}`}</p>}
    <div className="usage-detail">
      {context && <div className="usage-row"><div><strong>Context</strong><span>{context.percent.toFixed(1)}% used · {(100 - context.percent).toFixed(1)}% left</span></div>
        {context.tokens != null && context.limit != null && <small>{context.tokens.toLocaleString()} / {context.limit.toLocaleString()} tokens</small>}</div>}
      {footer.windows?.map(window => <div className="usage-row" key={window.label}><div><strong>{usageLabel(window.label)}</strong><span>{window.percent}% used</span></div>
        <div className="meter-bar" role="meter" aria-label={`${usageLabel(window.label)} used`} aria-valuenow={window.percent} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${Math.max(0, Math.min(100, window.percent))}%` }} /></div>
        {window.resets_at != null && <small>Resets {new Date(window.resets_at * 1000).toLocaleString()}</small>}</div>)}
      {credits && <dl className="info-list"><div><dt>Credits</dt><dd>{credits}</dd></div></dl>}
    </div>
  </dialog>
}

/** Desktop workspace moves for a pane or session: each one is there only when it fits the current layout. */
export interface Place { here?: () => void; right?: () => void; below?: () => void; all?: () => void }

function PlaceItems({ place, pending, onClose }: { place: Place | undefined; pending: boolean; onClose: () => void }) {
  if (!place || !(place.here || place.right || place.below || place.all)) return null
  const item = (run: (() => void) | undefined, icon: React.ReactNode, label: string) => run && <button disabled={pending} onClick={() => { run(); onClose() }}>{icon}{label}</button>
  return <>
    <p className="action-label">Show in this window</p>
    {item(place.all, <LayoutDashboard size={18} />, 'Show all panes')}
    {item(place.here, <Square size={18} />, 'Show in the focused slot')}
    {item(place.right, <PanelRight size={18} />, 'Open to the right')}
    {item(place.below, <PanelBottom size={18} />, 'Open below')}
    <hr />
    <p className="action-label">tmux</p>
  </>
}

/** The keyboard path for drag to merge: a list of the other sessions to choose from. */
function SessionPicker({ label, targets, pending, extra, onPick, onCancel }: { label: string; targets: Session[]; pending: boolean; extra?: React.ReactNode; onPick: (target: Session) => void; onCancel: () => void }) {
  return <div className="action-list" role="group" aria-label={label}>
    <p className="action-label">{label}</p>
    {targets.map((target, at) => <button key={target.id} autoFocus={at === 0} disabled={pending} onClick={() => onPick(target)}>{target.name}<small>{target.panes.length === 1 ? '1 pane' : `${target.panes.length} panes`}</small></button>)}
    {!targets.length && !extra && <p className="hint">There is no other session.</p>}
    {extra}
    <hr />
    <button disabled={pending} onClick={onCancel}>Cancel</button>
  </div>
}

/** A sheet on the phone; a popover at `at` on the desktop. The popover closes on Esc or a click outside it. */
function useActionsDialog(at: Anchor | undefined, close: () => void) {
  const modal = useModal(true, close)
  const popover = usePopover(modal.ref, at, close)
  return { ...modal, ...popover, className: at ? 'sheet popover' : 'sheet' }
}

export function SessionActions({ api, session, pane, pinned, archived, canMoveUp, canMoveDown, startDelete = false, at, place, targets, marks, onMarks, onPin, onArchive, onMove, onProject, onDone, onClose }: {
  api: Api; session: Session; pane: string; pinned: boolean; archived: boolean; canMoveUp: boolean; canMoveDown: boolean; startDelete?: boolean; at?: Anchor; place?: Place; targets: Session[]; marks?: ProjectMarks; onMarks?: () => void
  onPin: () => void; onArchive: () => void; onMove: (step: -1 | 1) => void; onProject: () => void; onDone: (newPane?: string, direction?: 'right' | 'down') => void; onClose: () => void
}) {
  const words = useWords()
  const [renaming, setRenaming] = useState(false)
  const [confirming, setConfirming] = useState(startDelete)
  // "Merge into…": choose a session, then confirm, as a drop of the whole session asks first.
  const [merging, setMerging] = useState<Session | 'pick' | null>(null)
  const [keep, setKeep] = useState<Keep | null>(null)  // which project a merge keeps, when both sessions have one
  const [name, setName] = useState(session.name)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const dialog = useActionsDialog(at, () => { if (!pending) onClose() })
  async function run(action: () => Promise<string | undefined>, direction?: 'right' | 'down') {
    setPending(true); setError('')
    try { onDone(await action(), direction) } catch (failure) { setError(errorText(failure)); onMarks?.() } finally { setPending(false) }
  }
  const rename = (event: React.FormEvent) => {
    event.preventDefault()
    const clean = name.trim()
    if (!SESSION_NAME.test(clean)) { setError(SESSION_NAME_HELP); return }
    // A busy agent keeps its own name: say so, and keep the sheet open.
    void (async () => {
      setPending(true); setError('')
      try { const saved = await api<{ note?: string }>('rename', { pane, name: clean }); if (saved.note) setError(`Saved. ${saved.note}`); else onDone(undefined) }
      catch (failure) { setError(errorText(failure)); onMarks?.() } finally { setPending(false) }
    })()
  }
  const split = (direction: 'right' | 'down') => run(async () => (await api<{ pane: string }>('split', { session: pane, direction })).pane, direction)
  return <dialog {...dialog} aria-labelledby="actions-title">
    <div className="sheet-heading"><h2 id="actions-title">{session.name}</h2>{!at && <button type="button" className="icon-button" aria-label="Close" disabled={pending} onClick={onClose}><X /></button>}</div>
    <p className="sheet-sub">{count(words, session.panes.length)} · {session.dir}</p>
    {merging === 'pick'
      ? <SessionPicker label={`Merge ${session.name} into`} targets={targets.filter(target => target.id !== session.id)} pending={pending} onPick={target => { setKeep(null); setMerging(target) }} onCancel={() => setMerging(null)} />
      : merging
      ? <div className="confirm-box" role="alertdialog" aria-labelledby="merge-session-title">
          <p id="merge-session-title"><strong>Merge {session.name} into {merging.name}?</strong> {session.panes.length === 1 ? `Its ${words.pane} moves` : `Its ${count(words, session.panes.length)} move`} there, and {session.name} closes.</p>
          {keepNeeded(marks, session.id, merging.id) && <KeepChoice source={session} target={merging} value={keep} disabled={pending} onChange={setKeep} />}
          <div className="sheet-buttons"><button type="button" className="secondary" autoFocus disabled={pending} onClick={() => setMerging('pick')}>Cancel</button><button type="button" className="primary" disabled={pending || (keepNeeded(marks, session.id, merging.id) && !keep)} onClick={() => void run(async () => { await api('merge', { kind: 'session', source: session.id, target: merging.id, ...(keep && { keep }) }); return undefined })}>{pending ? 'Merging…' : 'Merge'}</button></div>
        </div>
      : confirming
      ? <div className="confirm-box" role="alertdialog" aria-labelledby="delete-session-title">
          <p id="delete-session-title"><strong>Delete {session.name}?</strong> This ends {session.panes.length === 1 ? `its ${words.pane}` : `all ${count(words, session.panes.length)}`} and stops anything running in them. You cannot undo this.</p>
          <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setConfirming(false)}>Cancel</button><button type="button" className="danger" disabled={pending} onClick={() => void run(async () => { await api('tmux-sessions', { session: session.id }, undefined, 'DELETE'); return undefined })}>{pending ? 'Deleting…' : 'Delete session'}</button></div>
        </div>
      : renaming
      ? <form className="rename-form" onSubmit={rename}>
          <label htmlFor="rename-input">New name</label>
          <input id="rename-input" autoFocus value={name} maxLength={64} disabled={pending} autoComplete="off" onChange={event => setName(event.target.value)} />
          <p className="hint">This renames the tmux session too.</p>
          <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setRenaming(false)}>Cancel</button><button className="primary" disabled={pending}>{pending ? 'Renaming…' : 'Rename'}</button></div>
        </form>
      : <div className="action-list">
          <button disabled={pending} onClick={() => { onPin(); onClose() }}>{pinned ? <PinOff size={18} /> : <Pin size={18} />}{pinned ? 'Unpin' : 'Pin to top'}</button>
          <button disabled={pending} onClick={() => { onArchive(); onClose() }}>{archived ? <ArchiveRestore size={18} /> : <Archive size={18} />}{archived ? 'Restore from archive' : 'Archive'}</button>
          <button disabled={pending} onClick={() => setRenaming(true)}><Pencil size={18} />Rename</button>
          <button disabled={pending} onClick={onProject}><BookMarked size={18} />Project…</button>
          <button disabled={pending || !canMoveUp} onClick={() => onMove(-1)}><ArrowUp size={18} />Move up</button>
          <button disabled={pending || !canMoveDown} onClick={() => onMove(1)}><ArrowDown size={18} />Move down</button>
          <button disabled={pending} onClick={() => setMerging('pick')}><Merge size={18} />Merge into…</button>
          <hr />
          <PlaceItems place={place} pending={pending} onClose={onClose} />
          <button disabled={pending} onClick={() => void split('right')}><Columns2 size={18} />{words.newRight}{words.tmux && <kbd>Ctrl-b %</kbd>}</button>
          <button disabled={pending} onClick={() => void split('down')}><Rows2 size={18} />{words.newBelow}{words.tmux && <kbd>Ctrl-b "</kbd>}</button>
          <hr />
          <button className="danger-item" disabled={pending} onClick={() => setConfirming(true)}><Trash2 size={18} />Delete session</button>
        </div>}
    {error && <p className="error" role="alert">{error}</p>}
  </dialog>
}

export function PaneActions({ api, session, pane, at, place, targets, onFork, onDone, onClose }: {
  api: Api; session: Session | undefined; pane: Pane; at?: Anchor; place?: Place; targets: Session[]; onFork?: () => void; onDone: (newPane?: string, direction?: 'right' | 'down') => void; onClose: () => void
}) {
  const words = useWords()
  const [mode, setMode] = useState<'menu' | 'rename' | 'icon' | 'close' | 'move'>('menu')
  const [name, setName] = useState(pane.pane_name ?? '')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const dialog = useActionsDialog(at, () => { if (!pending) onClose() })
  const last = (session?.panes.length ?? 1) <= 1
  async function run(action: () => Promise<string | undefined>, direction?: 'right' | 'down') {
    setPending(true); setError('')
    try { onDone(await action(), direction) } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  // A busy agent keeps its own name: say so, and keep the sheet open.
  async function rename() {
    setPending(true); setError('')
    try { const saved = await api<{ note?: string }>('pane-name', { pane: pane.id, name }); if (saved.note) setError(`Saved in Tether. ${saved.note}`); else onDone(undefined) }
    catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  const split = (direction: 'right' | 'down') => run(async () => (await api<{ pane: string }>('split', { session: pane.id, direction })).pane, direction)
  const title = [paneTitle(pane), shownIndex(pane)].filter(Boolean).join(' · ')
  return <dialog {...dialog} aria-labelledby="pane-title">
    <div className="sheet-heading"><h2 id="pane-title">{title}</h2>{!at && <button type="button" className="icon-button" aria-label="Close" disabled={pending} onClick={onClose}><X /></button>}</div>
    <p className="sheet-sub">{words.Pane} in {session?.name ?? pane.label} · {pane.dir}</p>
    {mode === 'move'
      ? <SessionPicker label={`Move this ${words.pane} to`} targets={targets.filter(target => target.id !== pane.tmux_session)} pending={pending} onCancel={() => setMode('menu')}
          onPick={target => void run(async () => { await api('merge', { kind: 'pane', source: pane.id, target: target.id }); return undefined })}
          extra={!last && <button disabled={pending} onClick={() => void run(async () => { await api('merge', { kind: 'break', source: pane.id }); return undefined })}><Plus size={18} />New session</button>} />
      : mode === 'rename'
      ? <form className="rename-form" onSubmit={event => { event.preventDefault(); void rename() }}>
          <label htmlFor="pane-name-input">{words.Pane} name</label>
          <input id="pane-name-input" autoFocus value={name} maxLength={40} disabled={pending} autoComplete="off" placeholder={paneTitle({ ...pane, pane_name: '' })} onChange={event => setName(event.target.value)} />
          <p className="hint">{pane.agent === 'claude' || pane.agent === 'codex' ? `${agentNames[pane.agent]} gets the same name. ` : ''}{last ? 'The session takes it too. ' : ''}Leave it empty to show the agent name again.</p>
          <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setMode('menu')}>Cancel</button><button className="primary" disabled={pending}>{pending ? 'Saving…' : 'Save'}</button></div>
        </form>
      : mode === 'icon'
      ? <IconPicker api={api} pane={pane} pending={pending} run={run} onBack={() => setMode('menu')} />
      : mode === 'close'
      ? <div className="confirm-box" role="alertdialog" aria-labelledby="close-pane-title">
          <p id="close-pane-title"><strong>Close this {words.pane}?</strong> {last ? `It is the last ${words.pane}, so this also ends the session ${session?.name ?? ''}.` : 'This stops anything running in it.'} You cannot undo this.</p>
          <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setMode('menu')}>Cancel</button><button type="button" className="danger" disabled={pending} onClick={() => void run(async () => { await api('session', { pane: pane.id }, undefined, 'DELETE'); return undefined })}>{pending ? 'Closing…' : `Close ${words.pane}`}</button></div>
        </div>
      : <div className="action-list">
          <button disabled={pending} onClick={() => setMode('rename')}><Pencil size={18} />Rename {words.pane}</button>
          <button disabled={pending} onClick={() => setMode('icon')}><Shapes size={18} />Change icon</button>
          <button disabled={pending} onClick={() => setMode('move')}><ArrowRightLeft size={18} />Move to session…</button>
          <hr />
          <PlaceItems place={place} pending={pending} onClose={onClose} />
          <button disabled={pending} onClick={() => void split('right')}><Columns2 size={18} />{words.newRight}{words.tmux && <kbd>Ctrl-b %</kbd>}</button>
          <button disabled={pending} onClick={() => void split('down')}><Rows2 size={18} />{words.newBelow}{words.tmux && <kbd>Ctrl-b "</kbd>}</button>
          {onFork && canFork(pane) && <button disabled={pending} onClick={onFork}><GitFork size={18} />New chat from this</button>}
          <hr />
          <button className="danger-item" disabled={pending} onClick={() => setMode('close')}><Trash2 size={18} />Close {words.pane}{words.tmux && <kbd>Ctrl-b x</kbd>}</button>
        </div>}
    {error && <p className="error" role="alert">{error}</p>}
  </dialog>
}

// Pick a pane icon: suggested ones, a search over all lucide icons, or one Claude draws (server/pane_icons.py, about 10 s).
function IconPicker({ api, pane, pending, run, onBack }: { api: Api; pane: Pane; pending: boolean; run: (action: () => Promise<string | undefined>) => Promise<void>; onBack: () => void }) {
  const [query, setQuery] = useState('')
  const [names, setNames] = useState<string[]>([])
  const [hint, setHint] = useState('')
  const [drawing, setDrawing] = useState(false)
  useEffect(() => { if (query && !names.length) void import('lucide-react/dynamic').then(module => setNames(module.iconNames)) }, [query, names.length])
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  const found = words.length ? names.filter(name => words.every(word => name.includes(word))).slice(0, 96) : []
  const choices: [string, string][] = words.length ? found.map(name => [name, name.replace(/-/g, ' ')])
    : [['', 'Agent icon'], ...(pane.pane_icon?.startsWith('svg-') ? [[pane.pane_icon, 'Drawn by Claude'] as [string, string]] : []), ...Object.keys(PANE_ICONS).map(key => [key, key] as [string, string])]
  const pick = (icon: string) => void run(async () => { await api('pane-icon', { pane: pane.id, icon }); return undefined })
  async function draw() {
    setDrawing(true)
    try { await run(async () => { await api('pane-icon/draw', { pane: pane.id, label: paneTitle(pane), hint }); return undefined }) } finally { setDrawing(false) }
  }
  const busy = pending || drawing
  return <div className="icon-picker">
    <input type="search" aria-label="Search icons" placeholder="Search 1,600 icons" value={query} disabled={busy} onChange={event => setQuery(event.target.value)} />
    <div className={words.length ? 'icon-grid scroll' : 'icon-grid'} role="group" aria-label="Pane icon">
      {choices.map(([key, label]) =>
        <button key={key || 'agent'} type="button" title={label} aria-label={label} aria-pressed={(pane.pane_icon ?? '') === key} disabled={busy} onClick={() => pick(key)}>
          <AgentIcon agent={pane.agent} icon={key} size={20} />
        </button>)}
    </div>
    {words.length > 0 && !found.length && <p className="hint">{names.length ? 'No icon has that name. Ask Claude to draw one.' : 'Loading icons…'}</p>}
    <form className="icon-draw" onSubmit={event => { event.preventDefault(); void draw() }}>
      <input aria-label="Describe the icon for Claude" placeholder={`Describe it, or leave empty for “${paneTitle(pane)}”`} maxLength={200} value={hint} disabled={busy} onChange={event => setHint(event.target.value)} />
      <button className="primary" disabled={busy}><Sparkles size={16} />{drawing ? 'Drawing…' : 'Draw'}</button>
    </form>
    <p className="hint">Claude Haiku draws it in about 10 s.</p>
    <div className="sheet-buttons"><button type="button" className="secondary" disabled={busy} onClick={onBack}>Back</button></div>
  </div>
}

function slug(title: string) {
  const words = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').split('-').slice(0, 4).join('-')
  return (words || 'resume').slice(0, 40)
}

export function ResumeChat({ api, chat, onClose, onOpened }: { api: Api; chat: PastChat; onClose: () => void; onOpened: (pane: string) => void }) {
  const [name, setName] = useState(() => slug(chat.title))
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const modal = useModal(true, () => { if (!pending) onClose() })
  async function resume(event: React.FormEvent) {
    event.preventDefault()
    const clean = name.trim()
    if (!SESSION_NAME.test(clean)) { setError(SESSION_NAME_HELP); return }
    setPending(true); setError('')
    try {
      const result = await api<{ pane: string }>('resume', { id: chat.id, name: clean })
      // The new pane shows up in the session list a moment later.
      for (let tries = 0; tries < 15; tries++) {
        const list = await api<SessionsData>('sessions')
        if (list.sessions.some(pane => pane.id === result.pane)) break
        await new Promise(resolve => setTimeout(resolve, 300))
      }
      onOpened(result.pane)
    } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  return <dialog {...modal} className="sheet" aria-labelledby="resume-title">
    <form onSubmit={resume}>
      <div className="sheet-heading"><h2 id="resume-title">Resume chat</h2><button type="button" className="icon-button" aria-label="Close" disabled={pending} onClick={onClose}><X /></button></div>
      <p className="sheet-sub"><History size={14} aria-hidden="true" />{chat.title || 'Untitled chat'}</p>
      <p className="hint">Opens a new tmux session in {chat.dir ?? 'your home folder'} and runs <code>claude --resume</code>.</p>
      <label htmlFor="resume-name">Session name</label>
      <input id="resume-name" autoFocus value={name} maxLength={64} disabled={pending} autoComplete="off" onChange={event => setName(event.target.value)} />
      {error && <p className="error" role="alert">{error}</p>}
      <button className="primary create-button" disabled={pending}>{pending ? 'Starting…' : 'Resume in new session'}</button>
    </form>
  </dialog>
}

function Choice<T extends string>({ legend, name, value, options, onChange }: { legend: string; name: string; value: T; options: [T, string][]; onChange: (value: T) => void }) {
  return <fieldset className="choice"><legend>{legend}</legend><div className="segmented">
    {options.map(([key, label]) => <label key={key}><input type="radio" name={name} value={key} checked={value === key} onChange={() => onChange(key)} /><span>{label}</span></label>)}
  </div></fieldset>
}

// Push subscription is browser-specific; delivery settings apply to all subscribed devices.
function Notifications({ api }: { api: Api }) {
  const [notice, onNotice] = useState('')
  useEffect(() => { if (!notice) return; const timer = setTimeout(() => onNotice(''), 5000); return () => clearTimeout(timer) }, [notice])
  const supported = 'Notification' in window && 'serviceWorker' in navigator && 'PushManager' in window
  const [state, setState] = useState(supported ? Notification.permission === 'denied' ? 'blocked' : 'off' : 'unsupported')
  const [pending, setPending] = useState(true)
  const [publicKey, setPublicKey] = useState('')
  const [delivery, setDelivery] = useState<Record<Priority, Delivery>>({ urgent: 'sound', important: 'sound', low: 'off' })
  useEffect(() => {
    if (!supported) { setPending(false); return }
    let live = true
    Promise.all([api<PushConfig>('push/config'), navigator.serviceWorker.ready.then(registration => registration.pushManager.getSubscription())])
      .then(([config, subscription]) => {
        if (!live) return
        setPublicKey(config.publicKey || '')
        if (config.delivery) setDelivery(config.delivery)
        setState(!config.supported ? 'unsupported' : Notification.permission === 'denied' ? 'blocked' : subscription ? 'on' : 'off')
      }, failure => { if (live) onNotice(errorText(failure)) })
      .finally(() => { if (live) setPending(false) })
    return () => { live = false }
  }, [api, supported, onNotice])
  async function toggle() {
    setPending(true)
    try {
      // Request permission from the tap, before any network work.
      if (state !== 'on' && await Notification.requestPermission() !== 'granted') {
        setState(Notification.permission === 'denied' ? 'blocked' : 'off'); return
      }
      const registration = await navigator.serviceWorker.ready
      const existing = await registration.pushManager.getSubscription()
      if (state === 'on') {
        if (existing) {
          await api('push/unsubscribe', { endpoint: existing.endpoint })
          if (!await existing.unsubscribe()) throw new Error('Could not turn notifications off. Please try again.')
        }
        setState('off')
      } else {
        const key = atob(publicKey.replace(/-/g, '+').replace(/_/g, '/'))
        const subscription = existing ?? await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: Uint8Array.from(key, char => char.charCodeAt(0)) })
        try { await api('push/subscribe', { subscription: subscription.toJSON() }) }
        catch (failure) { if (!existing) await subscription.unsubscribe(); throw failure }
        setState('on')
      }
    } catch (failure) { onNotice(errorText(failure)) } finally { setPending(false) }
  }
  async function changeDelivery(priority: Priority, value: Delivery) {
    setPending(true)
    try {
      const result = await api<{ delivery: Record<Priority, Delivery> }>('push/settings', { delivery: { [priority]: value } })
      setDelivery(result.delivery)
    } catch (failure) { onNotice(errorText(failure)) } finally { setPending(false) }
  }
  async function test() {
    setPending(true)
    try {
      const result = await api<{ delivered: number }>('push/test', {})
      onNotice(result.delivered ? `Test sent to ${result.delivered} subscription${result.delivered === 1 ? '' : 's'}.` : 'No test notifications were delivered.')
    } catch (failure) { onNotice(errorText(failure)) } finally { setPending(false) }
  }
  const label = state === 'unsupported' ? 'Unsupported' : state === 'blocked' ? 'Blocked. Allow notifications for this site in the browser or app settings.' : state === 'on' ? 'On' : 'Off'
  return <>
    <label className="switch-row"><span><strong>Push notifications</strong><small>{pending ? 'Please wait…' : label}</small></span><input type="checkbox" role="switch" checked={state === 'on'} disabled={pending || state === 'unsupported' || state === 'blocked' || (!publicKey && state !== 'on')} onChange={() => void toggle()} /></label>
    {notice && <div className="toast" role="status">{notice}</div>}
    {state === 'on' && <div className="notification-delivery">
      <p className="setting-note">Applies to all subscribed devices.</p>
      {PRIORITIES.map(([priority, label]) => <fieldset key={priority} disabled={pending}>
        <legend>{label}</legend><div className="segmented">
          {(['sound', 'silent', 'off'] as const).map(value => <label key={value}><input type="radio" name={`notify-${priority}`} value={value} checked={delivery[priority] === value} onChange={() => void changeDelivery(priority, value)} /><span>{value === 'sound' ? 'Sound' : value === 'silent' ? 'Silent' : 'Off'}</span></label>)}
        </div>
      </fieldset>)}
    </div>}
    {state === 'on' && <div className="switch-row"><span><strong>Test notifications</strong><small>Send a notification to subscribed devices.</small></span><button type="button" className="secondary" disabled={pending} onClick={() => void test()}>Send test</button></div>}
  </>
}

const SETTINGS_TABS = ['Look', 'Chat', 'Voice', 'Alerts', 'Codex', 'System'] as const
type SettingsTab = typeof SETTINGS_TABS[number]
interface UpdateStatus { current: string; latest: string | null; available: boolean; checking: boolean; mode: string; checked_at: number | null; url: string }
function SystemSettings({ api, onReload }: { api: Api; onReload: () => void }) {
  const [host, setHost] = useState<{ resources: boolean; terminal: boolean } | null>(null)
  const fetchStatus = useCallback((signal: AbortSignal) => api<UpdateStatus>('update', undefined, signal), [api])
  const { data: status, error: updateError } = usePolling(fetchStatus, 5000)
  const [error, setError] = useState('')
  useEffect(() => {
    let live = true
    void api<{ resources: boolean; terminal: boolean }>('host').then(data => { if (live) setHost(data) }, failure => { if (live) setError(errorText(failure)) })
    return () => { live = false }
  }, [api])
  return <>
    {host?.resources && <HostResources api={api} terminal={host.terminal} />}
    <section className="settings-group" aria-labelledby="settings-system"><h3 id="settings-system">Tether</h3>
      {(error || updateError) && <p className="error" role="alert">{error || updateError}</p>}
      <div className="switch-row"><span><strong>Updates</strong><small>{status ? `Version ${status.current} · ${status.mode === 'off' ? 'Checks off' : status.checking ? 'Checking…' : status.available ? `${status.latest} available` : status.latest ? 'Up to date' : 'No release data'}` : 'Loading…'}</small></span>{status?.available && status.url.startsWith('https://') && <a className="secondary" href={status.url} target="_blank" rel="noreferrer">Release notes</a>}</div>
      <div className="switch-row"><span><strong>Reload Tether</strong><small>Unsent drafts stay.</small></span><button type="button" className="secondary" onClick={onReload}>Reload</button></div>
    </section>
    <AgentBrowserSettings api={api} />
  </>
}

export function Settings({ api, prefs, update, onReload, onClose }: { api: Api; prefs: Prefs; update: (change: (current: Prefs) => Partial<Prefs>) => void; onReload: () => void; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const [tab, setTab] = useState<SettingsTab>(() => {
    try { const saved = localStorage.getItem('tether.settings.tab'); return SETTINGS_TABS.find(item => item === saved) ?? 'Look' } catch { return 'Look' }
  })
  const tabs = useRef<HTMLDivElement>(null)
  function choose(next: SettingsTab) {
    setTab(next)
    try { localStorage.setItem('tether.settings.tab', next) } catch { /* private mode */ }
  }
  const toggle = (title: string, note: string, checked: boolean, onChange: (checked: boolean) => void, disabled = false) =>
    <label className="switch-row"><span><strong>{title}</strong><small>{note}</small></span><input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={event => onChange(event.target.checked)} /></label>
  return <dialog {...modal} className="sheet settings-sheet" aria-labelledby="settings-title">
    <div className="sheet-heading"><h2 id="settings-title">Settings</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <div className="settings-tabs" role="tablist" aria-label="Settings" ref={tabs} onKeyDown={event => {
      const index = SETTINGS_TABS.indexOf(tab)
      const next = event.key === 'ArrowRight' ? (index + 1) % SETTINGS_TABS.length : event.key === 'ArrowLeft' ? (index + SETTINGS_TABS.length - 1) % SETTINGS_TABS.length : event.key === 'Home' ? 0 : event.key === 'End' ? SETTINGS_TABS.length - 1 : null
      if (next == null) return
      event.preventDefault(); choose(SETTINGS_TABS[next]); tabs.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus()
    }}>
      {SETTINGS_TABS.map(item => <button type="button" role="tab" id={`settings-tab-${item}`} key={item} aria-selected={tab === item} aria-controls={`settings-panel-${item}`} tabIndex={tab === item ? 0 : -1} onClick={() => choose(item)}>{item}</button>)}
    </div>
    {tab !== 'System' && tab !== 'Alerts' && tab !== 'Codex' && <p className="settings-device-note">Saved on this device</p>}
    {SETTINGS_TABS.map(item => <div key={item} role="tabpanel" id={`settings-panel-${item}`} aria-labelledby={`settings-tab-${item}`} hidden={tab !== item} tabIndex={0}>
      {tab === item && <>
      {item === 'Look' && <>
    <section className="settings-group" aria-labelledby="settings-look"><h3 id="settings-look">Appearance</h3>
      <Choice legend="Theme" name="theme" value={prefs.theme} options={[['system', 'System'], ['light', 'Light'], ['dark', 'Dark']]} onChange={theme => update(() => ({ theme }))} />
      <fieldset className="choice schemes"><legend>Colors</legend><div className="scheme-grid">
        {SCHEMES.map(scheme => <label key={scheme.id} className="scheme" style={{ '--s-light': scheme.light.bg, '--s-dark': scheme.dark.bg, '--s-accent-light': scheme.light.accent, '--s-accent-dark': scheme.dark.accent, '--s-line': scheme.light.line } as React.CSSProperties}>
          <input type="radio" name="scheme" value={scheme.id} checked={schemeById(prefs.scheme).id === scheme.id} onChange={() => update(() => ({ scheme: scheme.id }))} />
          <span className="scheme-swatch" aria-hidden="true"><i /><i /></span><span>{scheme.name}</span>
        </label>)}
      </div></fieldset>
    </section>
      </>}
      {item === 'Chat' && <section className="settings-group" aria-labelledby="settings-chat"><h3 id="settings-chat">Chat and sidebar</h3>
      <Choice legend="Text size" name="text-size" value={prefs.textSize} options={[['small', 'Small'], ['default', 'Default'], ['large', 'Large']]} onChange={textSize => update(() => ({ textSize }))} />
      <Choice legend="Order" name="sort" value={prefs.sort} options={[['smart', 'Smart'], ['manual', 'Manual']]} onChange={sort => update(() => ({ sort }))} />
      <p className="setting-note">{prefs.sort === 'smart' ? 'Needs you, then Done, then Working, then the most recent.' : 'Your order. Use Reorder in the sidebar.'}</p>
      {toggle('Folder paths', 'Folder under each session name.', prefs.showPaths, showPaths => update(() => ({ showPaths })))}
      {toggle('tmux words', 'Say pane, split and Ctrl-b keys instead of tab.', prefs.tmuxWords, tmuxWords => update(() => ({ tmuxWords })))}
      {toggle('Live reply preview', 'Show Claude’s reply as it arrives.', prefs.liveReplyPreview, liveReplyPreview => update(() => ({ liveReplyPreview })))}
      {toggle('Agents bubble', 'Floating button that lists working agents. Drag it anywhere.', prefs.agentBubble, agentBubble => update(() => ({ agentBubble })))}
    </section>
      }
      {item === 'Voice' && <>
    <section className="settings-group" aria-labelledby="settings-voice"><h3 id="settings-voice">Voice</h3>
      {toggle('Voice button', 'Use your keyboard mic instead.', !prefs.voiceOff, on => update(() => ({ voiceOff: !on })))}
      {toggle('Start sound', 'Chime when the mic is ready.', prefs.voiceSound && !prefs.voiceOff, voiceSound => update(() => ({ voiceSound })), prefs.voiceOff)}
      {canSpeak && toggle('Read replies aloud', 'Read each completed reply.', prefs.readAloud, readAloud => update(() => ({ readAloud })))}
      {canSpeak && toggle('Hands-free', 'Mic starts after speech. Pause 2 seconds to send.', prefs.readAloud && prefs.handsFree && !prefs.voiceOff, handsFree => update(() => ({ handsFree })), !prefs.readAloud || prefs.voiceOff)}
      <div className="switch-row"><span><strong>Test the sound</strong><small>Uses the media volume.</small></span><button type="button" className="secondary" onClick={() => { prepareChime(); void playChime() }}>Play</button></div>
    </section>
      </>}
      {item === 'Alerts' && <section className="settings-group" aria-labelledby="settings-notify"><h3 id="settings-notify">Notifications</h3><Notifications api={api} /></section>}
      {item === 'Codex' && <GlobalCodexLevel api={api} />}
      {item === 'System' && <SystemSettings api={api} onReload={onReload} />}
      </>}
    </div>)}
  </dialog>
}

// Everything the agent in this pane can use: tap a command or skill to put it in the message box.
export function Tools({ api, pane, onPick, onClose }: { api: Api; pane: Pane; onPick: (text: string) => void; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { loadCatalog(api, pane.id).then(setCatalog, failure => setError(errorText(failure))) }, [api, pane.id])
  const groups = catalog ? [
    { title: 'Skills', items: catalog.commands.filter(item => item.kind === 'skill') },
    { title: 'Commands', items: catalog.commands.filter(item => item.kind !== 'skill') },
  ] : []
  const insert = (name: string) => name.startsWith('$') ? name + ' ' : '/' + name + ' '
  return <dialog {...modal} className="sheet tools-sheet" aria-labelledby="tools-title">
    <div className="sheet-heading"><h2 id="tools-title">Skills, plugins and MCP</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <p className="sheet-sub">{paneTitle(pane)} in {pane.dir}</p>
    <p className="hint">In the message box, type <code>/</code> for commands and skills{pane.agent === 'codex' ? <>, <code>$</code> for skills</> : null}, or <code>@</code> for files.</p>
    {error && <p className="error" role="alert">{error}</p>}
    {!catalog && !error && <p className="hint">Loading…</p>}
    {catalog && <>
      <details className="tools-group"><summary>MCP servers <span className="group-count">{catalog.mcp.length}</span></summary>
        <ul>{catalog.mcp.map(server => <li key={server.name + server.source}><strong>{server.name}</strong><small>{server.source}</small></li>)}</ul>
        {catalog.note && <p className="hint">{catalog.note}</p>}
      </details>
      {catalog.plugins.length > 0 && <details className="tools-group"><summary>Plugins <span className="group-count">{catalog.plugins.filter(plugin => plugin.enabled).length} on</span></summary>
        <ul>{catalog.plugins.map(plugin => <li key={plugin.name}><strong>{plugin.name}</strong><small>{plugin.enabled ? 'On' : 'Off'} · {plugin.source}</small></li>)}</ul>
      </details>}
      {groups.map(group => <details key={group.title} className="tools-group"><summary>{group.title} <span className="group-count">{group.items.length}</span></summary>
        <ul>{group.items.map(item => <li key={item.name}><button type="button" onClick={() => onPick(insert(item.name))}><strong>{item.name.startsWith('$') ? item.name : '/' + item.name}</strong>{item.description && <small>{item.description}</small>}</button></li>)}</ul>
      </details>)}
    </>}
  </dialog>
}

// Model and effort, read from the agent's own /model picker (it opens, is read and closes again: a few seconds).
// The model list costs about 5 seconds (the terminal opens /model), so keep the last one per agent and refresh on request.
const modelCacheKey = (agent: string) => `tether.models.${agent}`
function cachedModels(agent: string): ModelOptions | null {
  try { return JSON.parse(localStorage.getItem(modelCacheKey(agent)) ?? 'null') as ModelOptions | null } catch { return null }
}
// Mark the model the status line shows now; the cached "current" flag may be old.
function markCurrent(options: ModelOptions, label?: string, effort?: string): ModelOptions {
  const name = label?.toLowerCase()
  const match = name ? options.models.find(item => item.label.toLowerCase() === name) ?? options.models.find(item => item.label.toLowerCase().includes(name)) : undefined
  return { ...options, models: match ? options.models.map(item => ({ ...item, current: item.id === match.id })) : options.models, current_effort: effort ?? options.current_effort }
}
export function ModelSheet({ api, pane, thread, currentLabel, currentEffort, onClose, onDone }: { api: Api; pane: Pane; thread: string | null; currentLabel?: string; currentEffort?: string; onClose: () => void; onDone: (message: string) => void }) {
  const initial = cachedModels(pane.agent)
  const [data, setData] = useState<ModelOptions | null>(initial && markCurrent(initial, currentLabel, currentEffort))
  const [note, setNote] = useState('')
  const [loading, setLoading] = useState(false)
  const [model, setModel] = useState(() => data ? (data.models.find(item => item.current) ?? data.models[0])?.id ?? '' : '')
  const [effort, setEffort] = useState(data?.current_effort ?? '')
  const [busy, setBusy] = useState(false)
  const modal = useModal(true, () => { if (!busy) onClose() })
  const [view, setView] = useState<'model' | 'codex'>('model')
  const live = useRef(true)
  useEffect(() => () => { live.current = false }, [])
  const fetchList = useCallback(() => {
    setLoading(true); setNote('')
    api<ModelOptions>('model-options', { session: pane.id, thread }).then(result => {
      if (!live.current) return
      try { localStorage.setItem(modelCacheKey(pane.agent), JSON.stringify(result)) } catch { /* private mode */ }
      setData(result); setLoading(false)
      setModel((result.models.find(item => item.current) ?? result.models[0])?.id ?? '')
      setEffort(result.current_effort ?? '')
    }, failure => { if (live.current) { setNote(errorText(failure)); setLoading(false) } })
  }, [api, pane.id, pane.agent, thread])
  useEffect(() => { if (!initial) fetchList() }, [])  // eslint-disable-line react-hooks/exhaustive-deps -- first open only
  const chosen = data?.models.find(item => item.id === model)
  const current = data?.models.find(item => item.current)
  const changed = !!data && (model !== current?.id || (effort || null) !== (data.current_effort ?? null))
  async function apply() {
    if (!data) return
    setBusy(true); setNote('Applying… the agent is being switched.')
    try {
      const result = await api<{ message?: string }>('model', { session: pane.id, thread, model, effort: effort && effort !== data.current_effort && chosen?.effort !== false ? effort : null })
      onDone(result.message || 'Model changed.')
    } catch (failure) { setNote(errorText(failure)); setBusy(false) }
  }
  return <dialog {...modal} className="sheet model-sheet" aria-labelledby="model-title">
    <div className="sheet-heading"><h2 id="model-title">Model</h2><button type="button" className="icon-button" aria-label="Close" disabled={busy} onClick={onClose}><X /></button></div>
    {pane.agent === 'claude' && <div className="settings-tabs model-tabs" role="tablist" aria-label="Model or Codex">
      {(['model', 'codex'] as const).map(item => <button type="button" role="tab" key={item} id={`model-tab-${item}`} aria-selected={view === item} aria-controls="model-panel" tabIndex={view === item ? 0 : -1} disabled={busy}
        onClick={() => setView(item)} onKeyDown={event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); const next = item === 'model' ? 'codex' : 'model'; setView(next); document.getElementById(`model-tab-${next}`)?.focus() } }}>
        {item === 'model' ? 'This session' : 'Codex helper'}</button>)}
    </div>}
    {view === 'codex' ? <div role="tabpanel" id="model-panel" aria-labelledby="model-tab-codex"><GlobalCodexLevel api={api} /></div> : <>
    <p className="sheet-sub">{pane.agent === 'antigravity' ? 'Applies as soon as you choose.' : 'Applies to this session only.'}</p>
    {data && <div className="model-list" role="radiogroup" aria-label="Model">{data.models.map(item => <button key={item.id} type="button" role="radio" aria-checked={item.id === model} disabled={busy} onClick={() => setModel(item.id)}>
      <span>{item.label}</span>{item.current && <small>current</small>}{item.id === model && <Check size={18} />}</button>)}</div>}
    {data && data.efforts.length > 0 && chosen?.effort !== false && <fieldset className="choice"><legend>Effort</legend><div className="segmented effort-levels">
      {data.efforts.map(level => <label key={level}><input type="radio" name="effort" value={level} checked={effort === level} disabled={busy} onChange={() => setEffort(level)} /><span>{level[0].toUpperCase() + level.slice(1)}</span></label>)}
    </div></fieldset>}
    {loading && <p className="hint" role="status">Reading the model list from the terminal… about 5 seconds.</p>}
    {note && <p className="hint" role="status">{note}</p>}
    {!loading && <button type="button" className="link-button refresh-models" disabled={busy} onClick={fetchList}><RotateCw size={14} />{data ? 'Refresh model list' : 'Try again'}</button>}
    {data && <button type="button" className="primary create-button" disabled={busy || !changed} onClick={() => void apply()}>{busy ? 'Applying…' : 'Apply'}</button>}
    </>}
  </dialog>
}
