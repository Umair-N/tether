import { useEffect, useState } from 'react'
import type { Api } from './api'

// Layouts are data: { id, name, grid, panels, style, theme }. The desktop shell draws the same panels in a different
// grid; the phone keeps its own UI and takes only the theme and the style. Classic is today's UI and adds nothing.
// Custom layouts are JSON files in the server's layouts folder (server/custom_layouts.py). The checks below repeat
// the server's checks, so a file that reaches the browser some other way is still refused. Keep the two in step.

export const PANEL_NAMES = ['waiting', 'chat', 'agents', 'sessions', 'status', 'activity'] as const
export type PanelName = typeof PANEL_NAMES[number]
type Placed = Exclude<PanelName, 'agents'>
const PLACED: readonly string[] = ['activity', 'sessions', 'chat', 'status', 'waiting']
export interface GridColumn { top?: 'waiting'; main: Placed; bottom?: 'waiting'; width?: number }
export interface LayoutGrid { columns: GridColumn[]; chatWidth?: number; sessionsClosed?: boolean }
export type Surface = 'flat' | 'glass'
export type Backdrop = 'ember' | 'graphite' | 'dusk' | 'photo'
export interface LayoutStyle { radius: number; gap: number; surface: Surface }
const THEME_KEYS = ['bg', 'sidebar', 'surface', 'bubble', 'text', 'muted', 'line', 'hover', 'accent', 'accentText', 'attention', 'done', 'pin', 'error'] as const
const THEME_REQUIRED = ['bg', 'surface', 'text', 'muted', 'line', 'accent', 'attention'] as const
type ThemeKey = typeof THEME_KEYS[number]
export type LayoutTheme = Pick<Record<ThemeKey, string>, typeof THEME_REQUIRED[number]> & Partial<Record<ThemeKey, string>>
export interface Layout {
  id: string; name: string; description?: string; grid: LayoutGrid; panels: PanelName[]; style: LayoutStyle; theme: LayoutTheme | null
  /** Built-in layouts name their colours for the "Save your current look first?" dialog. */ themeName?: string
  /** Custom layouts: the file it came from, and why the file is now refused (the last good layout still shows). */ file?: string; stale?: string
}
/** The look on this device: the chosen layout, the Style and the Glass backdrop. custom: the last good copy of a custom layout. */
export interface Look {
  layout: string; surface: Surface; backdrop: Backdrop; custom?: Layout
  photo?: { id: string; url: string }
  tint?: string  // hex colour
  blur?: number  // 0–40
  fill?: number  // 0.3–0.9 panel opacity
}
/** "My look": the look and colours saved before a layout replaced them. */
export interface SavedLook extends Look { scheme: string }

export const DEFAULT_LOOK: Look = { layout: 'classic', surface: 'flat', backdrop: 'ember' }
export const CUSTOM_PREFIX = 'custom:'
const MIN_CONTRAST = 4.5
const RESERVED = ['classic', 'status', 'islands', 'vscode', 'zed', 'mine']

export const BUILT_INS: Layout[] = [
  { id: 'classic', name: 'Classic', description: 'Today’s Tether: the sessions list, the chat and the tabs.', panels: ['activity', 'sessions', 'chat', 'agents'],
    grid: { columns: [{ main: 'activity' }, { main: 'sessions', width: 248 }, { main: 'chat' }] }, style: { radius: 0, gap: 0, surface: 'flat' }, theme: null },
  { id: 'status', name: 'Status column', description: 'A right column shows every agent, grouped by what it needs.', panels: ['sessions', 'chat', 'agents', 'status'],
    grid: { columns: [{ main: 'sessions', width: 220 }, { main: 'chat' }, { main: 'status', width: 230 }] }, style: { radius: 0, gap: 0, surface: 'flat' }, theme: null },
  { id: 'islands', name: 'Islands', description: 'Floating cards on a night stage. What waits on you sits over the sessions.', panels: ['waiting', 'sessions', 'chat', 'agents'],
    grid: { columns: [{ top: 'waiting', main: 'sessions', width: 250 }, { main: 'chat' }] }, style: { radius: 16, gap: 10, surface: 'flat' }, themeName: 'Night islands',
    theme: { bg: '#0b0a1a', sidebar: '#16132e', surface: '#181434', bubble: '#221d47', text: '#ecebff', muted: '#a19bd0', line: '#2e2862', hover: '#2a2458', accent: '#a78bfa', accentText: '#120d2b', attention: '#fbbf24', done: '#4ade80', pin: '#7dd3fc', error: '#fb7185' } },
  { id: 'vscode', name: 'VS Code', description: 'An icon bar, the sessions list, tabs over the chat, and the waiting queue as a bottom panel.', panels: ['activity', 'sessions', 'chat', 'agents', 'waiting'],
    grid: { columns: [{ main: 'activity' }, { main: 'sessions', width: 230 }, { main: 'chat', bottom: 'waiting' }] }, style: { radius: 0, gap: 0, surface: 'flat' }, themeName: 'Dark modern',
    theme: { bg: '#1f1f1f', sidebar: '#181818', surface: '#1f1f1f', bubble: '#2b2b2b', text: '#cccccc', muted: '#9d9d9d', line: '#2b2b2b', hover: '#2a2d2e', accent: '#0078d4', accentText: '#ffffff', attention: '#cca700', done: '#2ea043', pin: '#4daafc', error: '#f48771' } },
  { id: 'zed', name: 'Zed', description: 'The chat sits in the middle with space around it. The sidebar stays hidden until you show it (⌘B).', panels: ['sessions', 'chat', 'agents'],
    grid: { columns: [{ main: 'sessions', width: 220 }, { main: 'chat' }], chatWidth: 720, sessionsClosed: true }, style: { radius: 8, gap: 0, surface: 'flat' }, themeName: 'Paper',
    theme: { bg: '#fafaf8', sidebar: '#f1f0ec', surface: '#fafaf8', bubble: '#ecebe6', text: '#1f1f1c', muted: '#6b6a64', line: '#e2e0da', hover: '#eceae4', accent: '#3a6ee8', accentText: '#ffffff', attention: '#b26b00', done: '#2f8a4c', pin: '#3a6ee8', error: '#b3261e' } },
]
export const CLASSIC = BUILT_INS[0]

class Bad extends Error {}
const isObject = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
function keys(value: unknown, allowed: readonly string[], where: string): asserts value is Record<string, unknown> {
  if (!isObject(value)) throw new Bad(`${where} must be an object.`)
  const extra = Object.keys(value).filter(key => !allowed.includes(key)).sort()
  if (extra.length) throw new Bad(`${where} has an unknown key: ${extra[0]}.`)
}
function whole(value: unknown, low: number, high: number, where: string) {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < low || value > high) throw new Bad(`${where} must be a whole number from ${low} to ${high}.`)
  return value
}
// Python's str.strip() and its control-character check, so both sides accept the same names.
function plain(value: unknown, high: number, where: string) {
  if (typeof value !== 'string' || !value.trim() || value.length > high || [...value].some(c => c.charCodeAt(0) < 32 || c.charCodeAt(0) === 127)) throw new Bad(`${where} must be plain text of 1 to ${high} characters.`)
  return value.trim()
}

const channel = (hex: string, at: number) => { const value = parseInt(hex.slice(at, at + 2), 16) / 255; return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4 }
const luminance = (hex: string) => 0.2126 * channel(hex, 1) + 0.7152 * channel(hex, 3) + 0.0722 * channel(hex, 5)
export function contrast(one: string, two: string) {
  const [a, b] = [luminance(one), luminance(two)].sort((x, y) => y - x)
  return (a + 0.05) / (b + 0.05)
}

function theme(value: unknown): LayoutTheme | null {
  if (value === null || value === undefined) return null
  keys(value, THEME_KEYS, 'theme')
  for (const key of THEME_REQUIRED) if (!(key in value)) throw new Bad(`theme needs ${key}.`)
  for (const [key, colour] of Object.entries(value)) if (typeof colour !== 'string' || !/^#[0-9a-fA-F]{6}$/.test(colour)) throw new Bad(`theme.${key} must be a hex colour such as #1a2b3c.`)
  const clean = Object.fromEntries(Object.entries(value).map(([key, colour]) => [key, (colour as string).toLowerCase()])) as LayoutTheme
  for (const key of ['bg', 'surface'] as const) if (contrast(clean.text, clean[key]) < MIN_CONTRAST) throw new Bad(`theme.text needs a contrast of 4.5:1 or more against theme.${key}.`)
  return clean
}

function grid(value: unknown, panels: string[]): LayoutGrid {
  keys(value, ['columns', 'chatWidth', 'sessionsClosed'], 'grid')
  const columns = value.columns
  if (!Array.isArray(columns) || columns.length < 1 || columns.length > 5) throw new Bad('grid.columns must be a list of 1 to 5 columns.')
  const placed: string[] = []
  const out = columns.map((column: unknown, at) => {
    const where = `grid.columns[${at}]`
    keys(column, ['top', 'main', 'bottom', 'width'], where)
    if (!('main' in column)) throw new Bad(`${where} needs main.`)
    const item: Record<string, unknown> = {}
    for (const slot of ['top', 'main', 'bottom'] as const) {
      if (!(slot in column)) continue
      const name = column[slot]
      if (typeof name !== 'string' || !PLACED.includes(name)) throw new Bad(`${where}.${slot} must be one of: ${PLACED.join(', ')}.`)
      if (slot !== 'main' && name !== 'waiting') throw new Bad(`${where}.${slot} can only be waiting.`)
      if (name === 'activity' && at !== 0) throw new Bad('activity can only be the main panel of the first column.')
      if (placed.includes(name)) throw new Bad(`${name} is placed twice.`)
      if (!panels.includes(name)) throw new Bad(`${name} is in grid but not in panels.`)
      placed.push(name)
      item[slot] = name
    }
    if ('width' in column) item.width = whole(column.width, 160, 480, `${where}.width`)
    return item as unknown as GridColumn
  })
  if (out.filter(column => column.main === 'chat').length !== 1) throw new Bad('chat must be the main panel of one column.')
  for (const name of panels) if (PLACED.includes(name) && !placed.includes(name)) throw new Bad(`${name} is in panels but has no place in grid.`)
  const result: LayoutGrid = { columns: out }
  if ('chatWidth' in value) {
    if (value.chatWidth !== 0) whole(value.chatWidth, 480, 1600, 'grid.chatWidth')
    result.chatWidth = whole(value.chatWidth, 0, 1600, 'grid.chatWidth')
  }
  if ('sessionsClosed' in value) {
    if (typeof value.sessionsClosed !== 'boolean') throw new Bad('grid.sessionsClosed must be true or false.')
    result.sessionsClosed = value.sessionsClosed
  }
  return result
}

/** Check a custom layout. Returns a clean copy, or throws an Error whose message says what to fix. */
export function validateLayout(data: unknown, fallbackId: string): Layout {
  keys(data, ['id', 'name', 'description', 'grid', 'panels', 'style', 'theme'], 'The layout')
  const id = 'id' in data ? data.id : fallbackId
  if (typeof id !== 'string' || !/^[a-z0-9][a-z0-9-]{0,39}$/.test(id)) throw new Bad('id must be 1 to 40 lowercase letters, digits or hyphens.')
  if (RESERVED.includes(id)) throw new Bad(`id "${id}" belongs to a built-in layout.`)
  const panels = data.panels
  if (!Array.isArray(panels) || !panels.length || panels.some(name => !(PANEL_NAMES as readonly unknown[]).includes(name))) throw new Bad(`panels must be a list of: ${PANEL_NAMES.join(', ')}.`)
  if (new Set(panels).size !== panels.length) throw new Bad('panels has a name twice.')
  if (!panels.includes('chat')) throw new Bad('panels must include chat.')
  if (!('grid' in data)) throw new Bad('The layout needs grid.')
  const style = 'style' in data ? data.style : {}
  keys(style, ['radius', 'gap', 'surface'], 'style')
  const surface = 'surface' in style ? style.surface : 'flat'
  if (surface !== 'flat' && surface !== 'glass') throw new Bad('style.surface must be flat or glass.')
  const layout: Layout = {
    id, name: plain(data.name, 40, 'name'), grid: grid(data.grid, panels as string[]), panels: [...panels] as PanelName[],
    style: { radius: whole('radius' in style ? style.radius : 0, 0, 24, 'style.radius'), gap: whole('gap' in style ? style.gap : 0, 0, 24, 'style.gap'), surface },
    theme: theme(data.theme),
  }
  if ('description' in data) layout.description = plain(data.description, 200, 'description')
  return layout
}

export interface CustomLayouts { folder: string; layouts: Layout[]; errors: { file: string; reason: string }[] }

/** Read the custom layouts from the server, and check each one again here. A refused one moves to errors. */
export function useCustomLayouts(api: Api, active = true) {
  const [state, setState] = useState<CustomLayouts | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    api<{ folder?: unknown; layouts?: unknown; errors?: unknown }>('layouts', undefined, controller.signal).then(data => {
      const errors = Array.isArray(data.errors) ? data.errors.filter(isObject).map(item => ({ file: String(item.file ?? ''), reason: String(item.reason ?? '') })) : []
      const layouts: Layout[] = []
      for (const raw of Array.isArray(data.layouts) ? data.layouts : []) {
        if (!isObject(raw)) continue
        const { file, stale, ...rest } = raw
        try {
          const layout = validateLayout(rest, String(rest.id ?? ''))
          layouts.push({ ...layout, file: String(file ?? ''), ...typeof stale === 'string' ? { stale } : {} })
        } catch (error) { errors.push({ file: String(file ?? ''), reason: error instanceof Error ? error.message : String(error) }) }
      }
      setState({ folder: typeof data.folder === 'string' ? data.folder : '', layouts, errors })
      setError('')
    }).catch(failure => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure)) })
    return () => controller.abort()
  }, [api, active])
  return { custom: state, error }
}

/** The layout a look shows. A custom layout uses its saved copy, checked again; a broken copy falls back to Classic. */
export function lookLayout(look: Look | undefined): Layout {
  if (!look) return CLASSIC
  if (look.layout.startsWith(CUSTOM_PREFIX) && look.custom) {
    const { id, name, description, grid, panels, style, theme } = look.custom
    try { return { ...validateLayout({ id, name, ...description === undefined ? {} : { description }, grid, panels, style, theme }, id), file: look.custom.file } } catch { return CLASSIC }
  }
  return BUILT_INS.find(layout => layout.id === look.layout) ?? CLASSIC
}
export const lookKey = (layout: Layout) => BUILT_INS.includes(layout) ? layout.id : CUSTOM_PREFIX + layout.id
export const themeLabel = (layout: Layout) => layout.themeName ?? `${layout.name} colours`

/** Dark or light, from the theme's background. A layout theme sets the page's theme to match. */
export const themeMode = (value: LayoutTheme) => luminance(value.bg) < 0.2 ? 'dark' : 'light'

/** Apply a layout's colours: one style tag after the scheme's, so it wins over the user's colours in light and dark. */
export function applyLayoutTheme(value: LayoutTheme | null) {
  let style = document.getElementById('tether-layout-theme')
  if (!value) { style?.remove(); return }
  if (!style) { style = document.createElement('style'); style.id = 'tether-layout-theme' }
  document.head.appendChild(style)
  const mode = themeMode(value)
  const accentText = value.accentText ?? (contrast('#ffffff', value.accent) >= contrast('#111111', value.accent) ? '#ffffff' : '#111111')
  const tokens: Record<string, string | undefined> = {
    '--bg': value.bg, '--sidebar': value.sidebar ?? value.bg, '--surface': value.surface, '--bubble': value.bubble ?? value.line, '--text': value.text, '--muted': value.muted,
    '--line': value.line, '--hover': value.hover ?? value.line, '--accent': value.accent, '--accent-text': accentText, '--attention': value.attention,
    '--error': value.error ?? (mode === 'dark' ? '#f09888' : '#a43b31'), '--done': value.done, '--pin': value.pin,
  }
  // Every value passed the hex check, so the text holds no CSS other than these tokens.
  style.textContent = `:root:root:root{color-scheme:${mode};color:${value.text};background:${value.bg};${Object.entries(tokens).filter(([, colour]) => colour).map(([name, colour]) => `${name}:${colour};`).join('')}}`
  document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]').forEach(meta => { meta.content = value.bg })
}

/** What the shell shows now. The rail always shows on a wide screen: it is Tether's way to the other screens. */
export interface GridShown { rail: boolean; sessions: boolean; inbox: boolean }
type Area = Placed | 'inbox'
interface Column { top?: Area; main: Area; bottom?: Area; width?: number }

/** The shell's CSS grid for a layout: three rows (top, main, bottom) and one track per column, left to right. */
export function gridStyle(layout: Layout, shown: GridShown) {
  let columns: Column[] = layout.grid.columns.map(column => ({ ...column }))
  if (shown.rail && columns[0].main !== 'activity') columns.unshift({ main: 'activity' })
  if (!shown.rail) columns = columns.filter(column => column.main !== 'activity')
  if (!shown.sessions) columns = columns.flatMap(column => {
    if (column.main !== 'sessions') return [column]
    const edge = column.top ?? column.bottom
    return edge ? [{ main: edge, width: column.width }] : []
  })
  if (shown.inbox) columns.push({ main: 'inbox' })
  const track = (column: Column) => column.main === 'chat' ? 'minmax(0, 1fr)' : column.main === 'activity' || column.main === 'inbox' ? 'auto' : `${column.width ?? (column.main === 'sessions' ? 248 : 240)}px`
  const row = (pick: (column: Column) => Area) => `"${columns.map(pick).join(' ')}"`
  return {
    gridTemplateColumns: columns.map(track).join(' '),
    gridTemplateRows: 'auto minmax(0, 1fr) auto',
    gridTemplateAreas: [row(column => column.top ?? column.main), row(column => column.main), row(column => column.bottom ?? column.main)].join(' '),
  }
}

export const hasPanel = (layout: Layout, name: PanelName) => layout.panels.includes(name)
