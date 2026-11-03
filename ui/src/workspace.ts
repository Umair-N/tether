import type { DesktopLayout, PanelItem, PanelKind, PanelLayout, Prefs } from './prefs'

// Desktop workspace: 1, 2 or 3 columns, or a 2×2 grid of live panes. Each slot is a full Chat.
// The focused slot always shows prefs.selected, so everything that follows the selected pane keeps working.

export const DEFAULT_LAYOUT: DesktopLayout = { count: 1, panes: [], focus: 0, cols: {}, row: 0.5, inbox: true, sidebar: true, stack: false }
export const WORKSPACE_QUERY = '(min-width: 900px) and (hover: hover) and (pointer: fine)'
export const MIN_SHARE = 0.15

export const desktopLayout = (stored: Partial<DesktopLayout> | undefined): DesktopLayout => ({ ...DEFAULT_LAYOUT, ...stored })

type Layouts = Prefs['layout']
const MAX_DESKS = 40

/** The layout that shows: the active session's, while it shows the selected pane; otherwise the selected pane's own session's. */
export function deskKeyFor(layout: Layouts, selected: string | undefined, owner: string | undefined, sessions: string[]): string {
  const active = layout.active
  const desk = active ? layout.desks?.[active] : undefined
  // A pane of another session stays in this layout only when the user placed it there (guests); auto-filled panes do not count.
  const belongs = !!selected && (owner === active || !!desk?.guests?.includes(selected))
  const keep = active && sessions.includes(active) && belongs && desk?.panes?.slice(0, desk.count ?? 1).includes(selected)
  return keep ? active : owner ?? active ?? ''
}
/** One session's layout. The sidebar and inbox switches are shared. Before per-session layouts, the one shared layout seeds the first session. */
export function deskFor(layout: Layouts, key: string): DesktopLayout {
  const own = layout.desks ? layout.desks[key] : layout.desktop
  return { ...desktopLayout(own), sidebar: layout.desktop?.sidebar ?? true, inbox: layout.desktop?.inbox ?? true }
}
/** Store one session's layout and make it the active one. */
export function withDesk(layout: Layouts, key: string, desk: DesktopLayout): Layouts {
  const { sidebar, inbox, ...own } = desk
  const others = Object.entries(layout.desks ?? {}).filter(([id]) => id !== key).slice(-(MAX_DESKS - 1))
  return { ...layout, desktop: { ...desktopLayout(layout.desktop), sidebar, inbox }, desks: { ...Object.fromEntries(others), [key]: own }, active: key }
}

/** Column boundaries (0..1) for a layout: one boundary for 2 slots and the 2×2 grid, two for 3 slots. */
export function columnBounds(layout: DesktopLayout, count: number): number[] {
  const want = count === 3 ? 2 : 1
  const stored = layout.cols[count]
  return Array.isArray(stored) && stored.length === want && stored.every(Number.isFinite) ? stored : equalBounds(count)
}
export const equalBounds = (count: number) => count === 3 ? [1 / 3, 2 / 3] : [0.5]

/** Move boundary j to v. Each column keeps at least MIN_SHARE. */
export function moveBound(bounds: number[], j: number, v: number): number[] {
  const out = [...bounds]
  const low = (j === 0 ? 0 : out[j - 1]) + MIN_SHARE, high = (j === out.length - 1 ? 1 : out[j + 1]) - MIN_SHARE
  out[j] = Math.min(high, Math.max(low, v))
  return out
}
export const clampRow = (v: number) => Math.min(1 - MIN_SHARE, Math.max(MIN_SHARE, v))
/** Grid tracks from boundaries: [0.4] → "minmax(0, 0.4fr) minmax(0, 0.6fr)". */
export function tracks(bounds: number[]): string {
  const edges = [0, ...bounds, 1]
  return edges.slice(1).map((edge, at) => `minmax(0, ${(edge - edges[at]).toFixed(4)}fr)`).join(' ')
}

/**
 * Which pane each visible slot shows.
 * - Stored panes keep their slot while they exist.
 * - The selected pane goes to the focused slot, or focus moves to it when it is already visible.
 * - Only the layout session's own panes (`own`) and panes the user placed here (`layout.guests`) can show.
 *   Empty slots never take panes of other sessions, so a closed or missing pane makes the split smaller instead.
 * - Empty slots take the session's other panes, then remembered hidden panes.
 */
export function resolveSlots(layout: DesktopLayout, selected: string | undefined, live: Set<string>, own: string[] = []): { slots: string[]; focus: number } {
  const count = Math.min(4, Math.max(1, layout.count))
  const allowed = (id: string) => live.has(id) && (id === selected || own.includes(id) || !!layout.guests?.includes(id))
  const slots: (string | undefined)[] = Array.from({ length: count }, (_, at) => {
    const id = layout.panes[at]
    return id && allowed(id) && layout.panes.indexOf(id) === at ? id : undefined
  })
  const wantFocus = Math.min(Math.max(0, layout.focus), count - 1)
  if (selected && live.has(selected) && !slots.includes(selected)) slots[wantFocus] = selected
  const used = new Set(slots)
  const spare = [...own, ...layout.panes.slice(count)].filter((id, at, all) => allowed(id) && id !== selected && !used.has(id) && all.indexOf(id) === at)
  const filled = slots.map(id => id ?? spare.shift()).filter((id): id is string => !!id)
  const at = selected ? filled.indexOf(selected) : -1
  return { slots: filled, focus: at >= 0 ? at : Math.min(wantFocus, Math.max(0, filled.length - 1)) }
}

/** The stored slot list: visible slots first, then remembered hidden panes, at most 4. */
export const storeSlots = (stored: string[], visible: string[]) => [...visible, ...stored.filter(id => !visible.includes(id))].slice(0, 4)

/** Put pane `id` in slot `index`. A pane that already shows in another slot swaps places with it. */
export function placePane(visible: string[], id: string, index: number): string[] {
  const out = [...visible]
  const from = out.indexOf(id)
  if (from >= 0) out[from] = out[index]
  out[index] = id
  return out
}

/** Show a new pane in a new slot to the right of the others. With 4 slots open, it replaces the focused slot. */
export function addSlot(visible: string[], focus: number, id: string): { panes: string[]; focus: number; count: DesktopLayout['count'] } {
  if (visible.length >= 4) return { panes: placePane(visible, id, focus), focus, count: 4 }
  const panes = [...visible, id]
  return { panes, focus: panes.length - 1, count: panes.length as DesktopLayout['count'] }
}

/** Drag and drop types: a pane or session from the sidebar, or a slot header being moved. */
export const DRAG_PANE = 'application/x-tether-pane', DRAG_SESSION = 'application/x-tether-session', DRAG_SLOT = 'application/x-tether-slot'

/** Show pane `id` in a new slot right after slot `after`. A pane already on screen moves there. At most 4 slots. */
export function insertSlot(visible: string[], id: string, after: number): string[] {
  const anchor = visible[after]
  const rest = visible.filter(other => other !== id)
  const at = anchor === id ? after : rest.indexOf(anchor) + 1
  return [...rest.slice(0, at), id, ...rest.slice(at)].slice(0, 4)
}

/** Swap the panes in two slots. */
export function swapSlots(visible: string[], from: number, to: number): string[] {
  const out = [...visible]
  ;[out[from], out[to]] = [out[to], out[from]]
  return out
}

/** Where a pane lands next to a slot: beside it (left or right), or above or below it. */
export type Side = 'left' | 'right' | 'top' | 'bottom'
export type Shape = { panes: string[]; count: DesktopLayout['count']; stack: boolean }

/** The slots as rows on screen: columns are one row, "2 stacked" is two rows of one, the grid is two rows of two. */
function toRows(visible: string[], stack: boolean): string[][] {
  if (visible.length === 4) return [visible.slice(0, 2), visible.slice(2)]
  if (visible.length === 2 && stack) return [[visible[0]], [visible[1]]]
  return [visible]
}
function fromRows(rows: string[][]): Shape | null {
  const panes = rows.flat()
  // Four in a row become the 2×2 grid, in reading order.
  if (rows.length === 1 && panes.length <= 4) return { panes, count: panes.length as Shape['count'], stack: false }
  if (rows.length === 2 && rows.every(row => row.length === 1)) return { panes, count: 2, stack: true }
  if (rows.length === 2 && rows.every(row => row.length === 2)) return { panes, count: 4, stack: false }
  return null
}

/**
 * Show pane `id` on one side of slot `index`. A pane already on screen moves there.
 * An empty string marks a cell that the next resolveSlots fills (a 2×2 grid needs 4 panes).
 * Returns null when the result is not a layout Tether has: at most 3 columns, 2 rows, or the 2×2 grid.
 */
export function splitSlot(visible: string[], stack: boolean, index: number, side: Side, id: string): Shape | null {
  const anchor = visible[index]
  if (!anchor || anchor === id) return null
  const rows = toRows(visible, stack).map(row => row.map(cell => cell === id ? '' : cell))
  const r = rows.findIndex(row => row.includes(anchor)), c = rows[r].indexOf(anchor)
  if (side === 'left' || side === 'right') {
    const at = side === 'right' ? c + 1 : c
    rows.forEach((row, n) => row.splice(at, 0, n === r ? id : ''))
  } else {
    if (rows.length > 1) return null
    const row = rows[0].map((_, n) => n === c ? id : '')
    if (side === 'top') rows.unshift(row); else rows.push(row)
  }
  // A moved pane leaves a hole. Drop columns and rows that are now empty; a hole in a 2×2 grid is refilled.
  let out = rows
  if (out.length === 2) out = out.map(row => row.filter((_, n) => out.some(other => other[n])))
  out = out.filter(row => row.some(Boolean))
  if (out.length === 1) out = [out[0].filter(Boolean)]
  return fromRows(out)
}

/* Panel column (panels.ts): at most 2 columns of stacked panels beside the slots. Stored per session in DesktopLayout.panels. */
export const PANEL_KINDS: readonly PanelKind[] = ['terminal', 'changes', 'browser', 'files', 'chat', 'tasks', 'plan']
export type PanelCols = PanelLayout['cols']
const MAX_PANEL_COLS = 2, MAX_PANEL_ROWS = 4
export const PANEL_WIDTH = 0.4
const validItem = (item: unknown): item is PanelItem => !!item && typeof (item as PanelItem).id === 'string' && PANEL_KINDS.includes((item as PanelItem).kind)
/** The stored columns, cleaned: unknown kinds and empty columns go, at most 2 columns of 4. Old prefs have none. */
export function panelCols(desk: DesktopLayout): PanelCols {
  const cols = Array.isArray(desk.panels?.cols) ? desk.panels.cols : []
  return cols.map(col => ({ ...col, items: (Array.isArray(col?.items) ? col.items : []).filter(validItem).slice(0, MAX_PANEL_ROWS) }))
    .filter(col => col.items.length).slice(0, MAX_PANEL_COLS)
}
const clampShare = (v: unknown, low: number, high: number, fallback: number) => typeof v === 'number' && Number.isFinite(v) ? Math.min(high, Math.max(low, v)) : fallback
export const panelWidth = (desk: DesktopLayout) => clampShare(desk.panels?.width, 0.2, 0.75, PANEL_WIDTH)
export const panelSplit = (desk: DesktopLayout) => clampShare(desk.panels?.split, MIN_SHARE, 1 - MIN_SHARE, 0.5)
/** Even boundaries for n stacked panels: 3 → [1/3, 2/3]. */
export const evenBounds = (n: number) => Array.from({ length: Math.max(0, n - 1) }, (_, at) => (at + 1) / n)
export function rowBounds(col: PanelCols[number]): number[] {
  const want = col.items.length - 1
  return Array.isArray(col.rows) && col.rows.length === want && col.rows.every(Number.isFinite) ? col.rows : evenBounds(col.items.length)
}
export const newPanelId = () => 'p' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
/** A changed column loses its row sizes; the others keep theirs. */
const tidy = (cols: PanelCols, changed: number[]): PanelCols => cols.map((col, at) => changed.includes(at) ? { items: col.items } : col).filter(col => col.items.length)
export const findPanel = (cols: PanelCols, id: string) => { const c = cols.findIndex(col => col.items.some(item => item.id === id)); return c < 0 ? null : { c, r: cols[c].items.findIndex(item => item.id === id) } }
/** Open a panel at the bottom of the last column, or below `after`. A full column sends it to the other column. */
export function addPanel(cols: PanelCols, item: PanelItem, after?: string): PanelCols {
  const at = after ? findPanel(cols, after) : null
  const c = at?.c ?? Math.max(0, cols.length - 1)
  if (!cols.length) return [{ items: [item] }]
  if (cols[c].items.length >= MAX_PANEL_ROWS) {
    const other = cols.findIndex(col => col.items.length < MAX_PANEL_ROWS)
    if (other >= 0) return tidy(cols.map((col, n) => n === other ? { items: [...col.items, item] } : col), [other])
    if (cols.length < MAX_PANEL_COLS) return [...cols, { items: [item] }]
    return cols
  }
  const items = [...cols[c].items]
  items.splice(at ? at.r + 1 : items.length, 0, item)
  return tidy(cols.map((col, n) => n === c ? { items } : col), [c])
}
export function removePanel(cols: PanelCols, id: string): PanelCols {
  const at = findPanel(cols, id)
  return at ? tidy(cols.map((col, n) => n === at.c ? { items: col.items.filter(item => item.id !== id) } : col), [at.c]) : cols
}
/** The toolbar buttons: close every panel of this kind, or open one. */
export function togglePanel(cols: PanelCols, kind: PanelKind, item: () => PanelItem): PanelCols {
  const open = cols.flatMap(col => col.items).filter(other => other.kind === kind)
  return open.length ? open.reduce((out, other) => removePanel(out, other.id), cols) : addPanel(cols, item())
}
export const hasPanel = (cols: PanelCols, kind: PanelKind) => cols.some(col => col.items.some(item => item.kind === kind))
export type PanelMove = 'up' | 'down' | 'left' | 'right'
/** Where a panel can go: up or down in its column, or into the column beside it (a second column opens if there is room). */
export function canMovePanel(cols: PanelCols, id: string, move: PanelMove): boolean {
  const at = findPanel(cols, id)
  if (!at) return false
  const col = cols[at.c].items
  if (move === 'up') return at.r > 0
  if (move === 'down') return at.r < col.length - 1
  const to = at.c + (move === 'left' ? -1 : 1)
  if (to >= 0 && to < cols.length) return cols[to].items.length < MAX_PANEL_ROWS
  // A new column only from a column with more than one panel.
  return cols.length < MAX_PANEL_COLS && col.length > 1
}
export function movePanel(cols: PanelCols, id: string, move: PanelMove): PanelCols {
  if (!canMovePanel(cols, id, move)) return cols
  const at = findPanel(cols, id)!
  const item = cols[at.c].items[at.r]
  if (move === 'up' || move === 'down') {
    const items = [...cols[at.c].items], to = at.r + (move === 'up' ? -1 : 1)
    ;[items[at.r], items[to]] = [items[to], items[at.r]]
    return cols.map((col, n) => n === at.c ? { ...col, items } : col)
  }
  const rest = cols.map((col, n) => n === at.c ? { items: col.items.filter(other => other.id !== id) } : col)
  const to = at.c + (move === 'left' ? -1 : 1)
  const all = (list: PanelCols) => tidy(list, list.map((_, n) => n))
  if (to < 0) return all([{ items: [item] }, ...rest])
  if (to >= cols.length) return all([...rest, { items: [item] }])
  return tidy(rest.map((col, n) => n === to ? { items: [...col.items, item] } : col), [at.c, to])
}
export const withPanels = (desk: DesktopLayout, patch: Partial<PanelLayout>): Partial<DesktopLayout> => ({ panels: { cols: panelCols(desk), width: desk.panels?.width, split: desk.panels?.split, ...patch } })
