// Every side panel of the wide shell can change size and close, in every layout (Classic too). There is no code per
// layout: PanelHandles.tsx finds the panels in the page and works out which side of each one faces the chat.
// Sizes and closed panels are kept per layout on this device (prefs.panelSizes, prefs.panelsClosed).
// A panel is a direct child of the shell listed in PANELS, or any child with data-area="<name>". A new panel only
// needs that attribute, and App.tsx must skip it when its name is in `closed`.

export type Edge = 'left' | 'right' | 'top' | 'bottom'
export type Rect = { left: number; right: number; top: number; bottom: number }
export type Found = { name: string; rect: Rect }
export type Handle = { name: string; edge: Edge; rect: Rect }  // edge: the panel side that faces the chat

export interface Columns { columns: { top?: string; main: string; bottom?: string }[] }

export const PANELS: Record<string, string> = { sessions: '.sidebar', inbox: '.inbox', status: '.l-status', waiting: '.l-waiting' }
export const WIDTH = { min: 180, max: 640 }
export const HEIGHT = { min: 80 }

const safe = (name: string) => name.replace(/[^a-z0-9-]/g, '')
/** The saved size key: the panel name for a width, "top" or "bottom" for an edge row height. */
export const sizeKey = (handle: Pick<Handle, 'name' | 'edge'>) => handle.edge === 'top' || handle.edge === 'bottom' ? handle.edge : safe(handle.name)
export const sizeVar = (key: string) => key === 'top' || key === 'bottom' ? `--lh-${key}` : `--lw-${safe(key)}`

/** The style variables for the saved sizes of one layout. */
export function sizeVars(sizes: Record<string, number> | undefined) {
  const vars: Record<string, string> = {}
  for (const [key, value] of Object.entries(sizes ?? {})) if (Number.isFinite(value) && value > 0) vars[sizeVar(key)] = `${Math.round(value)}px`
  return vars
}

const overlaps = (a: Rect, b: Rect) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1

/** Which side of each panel faces the chat. A panel that the layout puts at the top or bottom of a column is an edge
 *  panel (its height changes), unless it fills the column alone. Any other panel is a column: its width changes, on
 *  the side towards the middle of the chat. */
export function handles(panels: Found[], chat: Rect, grid?: Columns): Handle[] {
  const centre = (chat.left + chat.right) / 2
  const all = [...panels.map(item => item.rect), chat]
  const role = (name: string) => grid?.columns.find(column => column.top === name) ? 'top' : grid?.columns.find(column => column.bottom === name) ? 'bottom' : null
  return panels.map(({ name, rect }) => {
    const below = all.some(other => other !== rect && overlaps(rect, other) && other.top >= rect.bottom - 1)
    const above = all.some(other => other !== rect && overlaps(rect, other) && other.bottom <= rect.top + 1)
    const edge: Edge = role(name) === 'top' && below ? 'bottom' : role(name) === 'bottom' && above ? 'top' : (rect.left + rect.right) / 2 < centre ? 'right' : 'left'
    return { name, edge, rect }
  })
}

const clamp = (value: number, min: number, max: number) => Math.round(Math.min(max, Math.max(min, value)))

/** The new size after moving the handle by (dx, dy) pixels. `room` is the shell height, for the edge rows. */
export function dragSize(handle: Handle, dx: number, dy: number, room: number) {
  const { rect, edge } = handle
  const most = Math.max(HEIGHT.min, room * 0.6)
  if (edge === 'bottom') return clamp(rect.bottom - rect.top + dy, HEIGHT.min, most)
  if (edge === 'top') return clamp(rect.bottom - rect.top - dy, HEIGHT.min, most)
  return clamp(rect.right - rect.left + (edge === 'right' ? dx : -dx), WIDTH.min, WIDTH.max)
}

/** Where a closed panel's reopen tab goes: the side of the chat that the panel had in the layout. */
export function closedSide(grid: Columns, name: string): Edge {
  const chat = grid.columns.findIndex(column => column.main === 'chat')
  const at = grid.columns.findIndex(column => column.main === name || column.top === name || column.bottom === name)
  if (at < 0) return 'right'
  if (at === chat) return grid.columns[at].top === name ? 'top' : 'bottom'
  return at < chat ? 'left' : 'right'
}

const LABELS: Record<string, string> = { sessions: 'Sessions', inbox: 'Needs you', status: 'Status', waiting: 'Waiting on you' }
export const panelLabel = (name: string) => LABELS[name] ?? name.charAt(0).toUpperCase() + name.slice(1)
