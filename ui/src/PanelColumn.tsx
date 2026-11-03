import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Check, Maximize2, Minimize2, MoreHorizontal, Plus, SquareX, X } from 'lucide-react'
import { errorText } from './api'
import type { Api, Pane } from './api'
import { anchorOf, useModal, usePopover } from './hooks'
import type { Anchor } from './hooks'
import { keyLabel } from './keys'
import { PANELS, TOOLBAR_PANELS, extraPanels, togglePanelKind } from './panels'
import type { PanelItem, SetPanels } from './panels'
import type { DesktopLayout } from './prefs'
import { paneTitle } from './Sidebar'
import { addPanel, canMovePanel, clampRow, hasPanel, movePanel, newPanelId, panelCols, panelSplit, panelWidth, removePanel, rowBounds, moveBound, evenBounds, tracks } from './workspace'
import type { PanelMove } from './workspace'

const GAP = 8

/** A small menu next to a button: the same popover as the pane and session menus. */
export function PopMenu({ at, label, onClose, children }: { at: Anchor; label: string; onClose: () => void; children: ReactNode }) {
  const modal = useModal(true, onClose)
  const popover = usePopover(modal.ref, at, onClose)
  // The menu unmounts instead of closing, so the browser does not put focus back on its button. Do that here.
  const [opener] = useState(() => document.activeElement as HTMLElement | null)
  useEffect(() => () => { if (opener?.isConnected && (!document.activeElement || document.activeElement === document.body)) opener.focus() }, [opener])
  return <dialog {...modal} {...popover} className="sheet popover panel-menu" aria-label={label}>{children}</dialog>
}

/** A drag handle between two areas. Arrow keys move it, Enter or a double-click resets it. */
function Handle({ vertical, label, share, style, onMove, onDone, onReset, onStep }: { vertical: boolean; label: string; share: number; style: React.CSSProperties; onMove: (event: React.PointerEvent) => void; onDone: () => void; onReset: () => void; onStep: (step: number) => void }) {
  const [drag, setDrag] = useState(false)
  return <div role="separator" tabIndex={0} className={`slot-divider ${vertical ? 'vertical' : 'horizontal'} ${drag ? 'dragging' : ''}`} aria-orientation={vertical ? 'vertical' : 'horizontal'} aria-label={label}
    aria-valuemin={15} aria-valuemax={85} aria-valuenow={Math.round(share * 100)} title="Drag to resize. Double-click to reset." style={style}
    onPointerDown={event => { event.currentTarget.setPointerCapture(event.pointerId); event.currentTarget.focus(); event.preventDefault(); setDrag(true) }}
    onPointerMove={event => { if (drag) onMove(event) }}
    onPointerUp={() => { if (drag) { setDrag(false); onDone() } }}
    onPointerCancel={() => { setDrag(false); onDone() }}
    onDoubleClick={onReset}
    onKeyDown={event => {
      const back = vertical ? 'ArrowLeft' : 'ArrowUp', forward = vertical ? 'ArrowRight' : 'ArrowDown'
      if (event.key === back || event.key === forward) { event.preventDefault(); onStep((event.shiftKey ? 0.1 : 0.02) * (event.key === forward ? 1 : -1)) }
      else if (event.key === 'Enter') { event.preventDefault(); onReset() }
    }}><span aria-hidden="true" /></div>
}

/** Where handle j sits between cells that share the space with a GAP between them. */
const handleAt = (share: number, j: number, cells: number) => `calc((100% - ${(cells - 1) * GAP}px) * ${share.toFixed(4)} + ${j * GAP + GAP / 2 - 6}px)`

interface ColumnProps {
  api: Api
  desk: DesktopLayout
  pane: Pane
  setPanels: SetPanels
  /** The app's chat for a side chat panel, or null when that pane has ended. */
  renderChat: (pane: string) => ReactNode | null
}

/** The right-hand panel column: one or two columns of stacked panels, a handle to resize it, and ⤢ to let one panel fill the workspace. */
export function PanelColumn({ api, desk, pane, setPanels, renderChat }: ColumnProps) {
  const cols = panelCols(desk)
  const [draft, setDraft] = useState<{ width?: number; split?: number; rows?: { c: number; bounds: number[] } } | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [active, setActive] = useState<string | null>(null)
  const [menu, setMenu] = useState<{ id: string; at: Anchor } | null>(null)
  const root = useRef<HTMLElement>(null)
  const ids = cols.flatMap(col => col.items.map(item => item.id))
  const shown = expanded && ids.includes(expanded) ? expanded : null
  useEffect(() => {
    if (!shown) return
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape' && !document.querySelector('dialog[open]')) { event.preventDefault(); setExpanded(null) } }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [shown])
  if (!cols.length) return null
  const width = draft?.width ?? panelWidth(desk), split = draft?.split ?? panelSplit(desk)
  const close = (id: string) => setPanels(current => ({ cols: removePanel(panelCols(current), id) }))
  const setItem = (id: string, patch: Partial<PanelItem>) => setPanels(current => ({ cols: panelCols(current).map(col => ({ ...col, items: col.items.map(item => item.id === id ? { ...item, ...patch } : item) })) }))
  const another = (item: PanelItem) => setPanels(current => ({ cols: addPanel(panelCols(current), { id: newPanelId(), kind: item.kind, fresh: true }, item.id) }))
  const move = (id: string, where: PanelMove) => setPanels(current => ({ cols: movePanel(panelCols(current), id, where) }))
  const save = () => {
    const done = draft
    setDraft(null)
    if (!done) return
    setPanels(current => done.rows ? { cols: panelCols(current).map((col, c) => c === done.rows!.c ? { ...col, rows: done.rows!.bounds } : col) } : { width: done.width ?? current.panels?.width, split: done.split ?? current.panels?.split })
  }
  const setRows = (c: number, bounds: number[]) => setPanels(current => ({ cols: panelCols(current).map((col, n) => n === c ? { ...col, rows: bounds } : col) }))
  const menuItem = menu ? cols.flatMap(col => col.items).find(item => item.id === menu.id) : undefined

  return <aside ref={root} className={shown ? 'panel-column expanded' : 'panel-column'} style={shown ? undefined : { flexBasis: `${(width * 100).toFixed(2)}%` }} aria-label="Panels">
    {!shown && <Handle vertical label="Resize the panel column" share={width} style={{ left: -2, top: 8, bottom: 8 }}
      onMove={event => { const area = root.current!.parentElement!.getBoundingClientRect(); setDraft({ width: Math.min(0.75, Math.max(0.2, (area.right - event.clientX) / area.width)) }) }}
      onDone={save} onReset={() => setPanels(() => ({ width: undefined }))} onStep={step => setPanels(current => ({ width: Math.min(0.75, Math.max(0.2, panelWidth(current) - step)) }))} />}
    <div className="panel-cols" style={!shown && cols.length > 1 ? { gridTemplateColumns: tracks([split]) } : undefined}>
      {cols.map((col, c) => {
        const bounds = draft?.rows?.c === c ? draft.rows.bounds : rowBounds(col)
        return <div key={col.items[0].id} className="panel-stack" style={!shown && col.items.length > 1 ? { gridTemplateRows: tracks(bounds) } : undefined}>
          {col.items.map(item => {
            const spec = PANELS[item.kind]
            const Icon = spec.icon, Body = spec.body
            const big = shown === item.id
            const chat = item.kind === 'chat' && item.pane ? renderChat(item.pane) : null
            const sub = item.kind === 'chat' ? '' : item.kind === 'terminal' ? item.folder ?? pane.dir : paneTitle(pane)
            return <section key={item.id} className={`panel${big ? ' expanded' : ''}${active === item.id ? ' active' : ''}`} aria-label={spec.title}
              onPointerDownCapture={() => setActive(item.id)} onFocusCapture={() => setActive(item.id)}>
              <header className="panel-header">
                <Icon size={15} aria-hidden="true" /><h2>{spec.title}</h2>{sub && <span className="panel-sub" title={sub}><bdi>{sub}</bdi></span>}
                <span className="panel-header-tools">
                  {spec.multi && <button type="button" className="icon-button" aria-label={`New ${spec.title.toLowerCase()}`} title={`New ${spec.title.toLowerCase()}`} onClick={() => another(item)}><Plus size={15} /></button>}
                  <button type="button" className="icon-button" aria-label={`${spec.title} options`} title="Options" aria-haspopup="dialog" aria-expanded={menu?.id === item.id} onClick={event => setMenu({ id: item.id, at: anchorOf(event) })}><MoreHorizontal size={15} /></button>
                  <button type="button" className="icon-button" aria-label={big ? 'Restore (Esc)' : `Expand ${spec.title.toLowerCase()}`} title={big ? 'Restore (Esc)' : 'Expand to fill the workspace'} aria-pressed={big} onClick={() => setExpanded(big ? null : item.id)}>{big ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button>
                  <button type="button" className="icon-button" aria-label={`Close ${spec.title.toLowerCase()}`} title="Close panel" onClick={() => { if (big) setExpanded(null); close(item.id) }}><X size={15} /></button>
                </span>
              </header>
              <div className="panel-body">
                {item.kind === 'chat' ? chat ?? <div className="panel-empty"><p><strong>This side chat has ended.</strong></p><p>Close the panel, or open a new side chat from ⋯.</p></div>
                  : <Body api={api} pane={pane} folder={pane.dir || null} active={!shown || big} item={item} onItem={patch => setItem(item.id, patch)} />}
              </div>
            </section>
          })}
          {!shown && col.items.length > 1 && bounds.map((share, j) => <Handle key={j} vertical={false} label={`Resize panels ${j + 1} and ${j + 2}`} share={share} style={{ top: handleAt(share, j, col.items.length), left: 0, right: 0 }}
            onMove={event => { const area = (event.currentTarget.parentElement as HTMLElement).getBoundingClientRect(); const total = area.height - (col.items.length - 1) * GAP; setDraft({ rows: { c, bounds: moveBound(bounds, j, (event.clientY - area.top - j * GAP - GAP / 2) / total) } }) }}
            onDone={save} onReset={() => setRows(c, evenBounds(col.items.length))} onStep={step => setRows(c, moveBound(bounds, j, share + step))} />)}
        </div>
      })}
      {!shown && cols.length > 1 && <Handle vertical label="Resize the two panel columns" share={split} style={{ left: handleAt(split, 0, 2), top: 0, bottom: 0 }}
        onMove={event => { const area = (event.currentTarget.parentElement as HTMLElement).getBoundingClientRect(); setDraft({ split: clampRow((event.clientX - area.left - GAP / 2) / (area.width - GAP)) }) }}
        onDone={save} onReset={() => setPanels(() => ({ split: undefined }))} onStep={step => setPanels(current => ({ split: clampRow(panelSplit(current) + step) }))} />}
    </div>
    {menu && menuItem && <PanelOptions api={api} item={menuItem} at={menu.at} canMove={where => canMovePanel(cols, menuItem.id, where)}
      onMove={where => move(menuItem.id, where)} onClose={() => setMenu(null)} onRemove={() => { if (shown === menuItem.id) setExpanded(null); close(menuItem.id) }} />}
  </aside>
}

const MOVES: { where: PanelMove; label: string; icon: typeof ArrowUp }[] = [
  { where: 'up', label: 'Move up', icon: ArrowUp }, { where: 'down', label: 'Move down', icon: ArrowDown },
  { where: 'left', label: 'Move to the left column', icon: ArrowLeft }, { where: 'right', label: 'Move to the right column', icon: ArrowRight },
]

/** A panel's ⋯ menu: move it, end its shell (terminal), or close it. */
function PanelOptions({ api, item, at, canMove, onMove, onClose, onRemove }: { api: Api; item: PanelItem; at: Anchor; canMove: (where: PanelMove) => boolean; onMove: (where: PanelMove) => void; onClose: () => void; onRemove: () => void }) {
  const [confirm, setConfirm] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const title = PANELS[item.kind].title
  const end = async () => {
    setPending(true); setError('')
    try { await api('side-shell/end', { pane: item.pane }); onClose(); onRemove() } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  const moves = MOVES.filter(move => canMove(move.where))
  return <PopMenu at={at} label={`${title} options`} onClose={() => { if (!pending) onClose() }}>
    <div className="sheet-heading"><h2>{title}</h2></div>
    {confirm ? <div className="confirm-box" role="alertdialog" aria-labelledby="end-shell-title">
        <p id="end-shell-title"><strong>End this shell?</strong> This stops anything running in it. Your chats and other panes stay open.</p>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setConfirm(false)}>Cancel</button><button type="button" className="danger" autoFocus disabled={pending} onClick={() => void end()}>{pending ? 'Ending…' : 'End shell'}</button></div>
      </div>
    : <div className="action-list">
        {moves.map(({ where, label, icon: Icon }) => <button key={where} type="button" onClick={() => { onClose(); onMove(where) }}><Icon size={16} />{label}</button>)}
        {moves.length > 0 && <hr />}
        {item.kind === 'terminal' && item.pane && <button type="button" className="danger-item" onClick={() => setConfirm(true)}><SquareX size={16} />End this shell</button>}
        <button type="button" onClick={() => { onClose(); onRemove() }}><X size={16} />Close panel</button>
      </div>}
  </PopMenu>
}

interface ToolsProps {
  desk: DesktopLayout
  pane: Pane
  setPanels: SetPanels
  onSideChat: () => void
}

/** The toolbar's panel buttons (Terminal, Changes, Browser, Files) and its ⋯ menu (Side chat, Background tasks, Plan). */
export function PanelTools({ desk, pane, setPanels, onSideChat }: ToolsProps) {
  const cols = panelCols(desk)
  const [menu, setMenu] = useState<Anchor | null>(null)
  const extras = extraPanels(desk, pane)
  return <div className="panel-tools" role="group" aria-label="Panels">
    {TOOLBAR_PANELS.map(({ kind, key }) => {
      const { title, icon: Icon } = PANELS[kind]
      const label = key ? `${title} (${keyLabel(key)})` : title
      return <button key={kind} type="button" className="icon-button" aria-label={label} title={label} aria-pressed={hasPanel(cols, kind)} onClick={() => togglePanelKind(setPanels, kind)}><Icon size={17} /></button>
    })}
    <button type="button" className="icon-button" aria-label="More panels" title="More panels" aria-haspopup="dialog" aria-expanded={!!menu} onClick={event => setMenu(anchorOf(event))}><MoreHorizontal size={17} /></button>
    {menu && <PopMenu at={menu} label="More panels" onClose={() => setMenu(null)}>
      <div className="sheet-heading"><h2>More panels</h2></div>
      <div className="action-list">
        {extras.map(({ kind, key, open }) => {
          const { title, icon: Icon } = PANELS[kind]
          return <button key={kind} type="button" aria-pressed={open} onClick={() => { setMenu(null); if (kind === 'chat' && !open) onSideChat(); else togglePanelKind(setPanels, kind) }}>
            <Icon size={16} />{kind === 'chat' && !open ? 'Side chat' : title}{open && <Check size={15} className="panel-menu-check" aria-label="Open" />}{key && <kbd>{keyLabel(key)}</kbd>}</button>
        })}
        {!extras.length && <p className="panel-menu-empty">Nothing more for this pane. Side chat needs a Claude or Codex chat with a first message.</p>}
      </div>
    </PopMenu>}
  </div>
}
