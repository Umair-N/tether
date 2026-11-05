import { createElement } from 'react'
import type { ComponentType } from 'react'
import { Diff, Folder, Globe, Layers, ListChecks, MessageSquare, Terminal } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { canFork } from './api'
import type { Api, Pane } from './api'
import type { DesktopLayout, PanelItem, PanelKind, PanelLayout } from './prefs'
import { hasPanel, newPanelId, panelCols, togglePanel } from './workspace'
import { TerminalPanel } from './TerminalPanel'
import { PlanPanel, TasksPanel } from './SidePanels'
import { ChangesPanel } from './ChangesPanel'
import { BrowserDock } from './BrowserWindow'
import { FilesPanel } from './FilesPanel'

export type { PanelItem, PanelKind }

/** What every panel body gets. The column draws the header (title, +, ⋯, expand, close); a panel draws only its body.
 * pane: the focused slot's pane. folder: that pane's folder (Pane.dir), or null. active: the panel is on screen (not hidden behind an expanded one), so it may poll. */
export interface PanelProps { api: Api; pane: Pane; folder: string | null; active: boolean }
/** The column also passes the panel's own stored entry, for panels that keep state in the layout (the terminal's shell). */
export interface PanelBodyProps extends PanelProps { item: PanelItem; onItem: (patch: Partial<PanelItem>) => void }

export interface PanelSpec {
  title: string
  icon: LucideIcon
  body: ComponentType<PanelBodyProps>
  /** + in the header opens another one of these. */
  multi?: boolean
  /** Only on the toolbar ⋯ menu, not as a toolbar button. */
  menu?: boolean
}

// The side chat body is drawn by the column; this stands in elsewhere.
function ComingSoon({ item }: PanelBodyProps) {
  return createElement('div', { className: 'panel-empty' }, createElement('p', null, createElement('strong', null, PANELS[item.kind].title)), createElement('p', null, 'Coming in this build.'))
}

/** Every panel kind, in toolbar order. The chat body is drawn by the column, which has the app's chat. */
export const PANELS: Record<PanelKind, PanelSpec> = {
  terminal: { title: 'Terminal', icon: Terminal, body: TerminalPanel, multi: true },
  changes: { title: 'Changes', icon: Diff, body: ChangesPanel },
  browser: { title: 'Browser', icon: Globe, body: BrowserDock },
  files: { title: 'Files', icon: Folder, body: FilesPanel },
  chat: { title: 'Side chat', icon: MessageSquare, body: ComingSoon, menu: true },
  tasks: { title: 'Background tasks', icon: Layers, body: TasksPanel, menu: true },
  plan: { title: 'Plan', icon: ListChecks, body: PlanPanel, menu: true },
}
/** The toolbar buttons and their keys. */
export const TOOLBAR_PANELS: { kind: PanelKind; key?: string }[] = [{ kind: 'terminal', key: '⌘.' }, { kind: 'changes' }, { kind: 'browser' }, { kind: 'files' }]

/** Change the panel layout from the latest stored one. */
export type SetPanels = (change: (desk: DesktopLayout) => Partial<PanelLayout>) => void

/** Open or close every panel of a kind, from the toolbar, a key or the command menu. A side chat starts with a fork instead. */
export function togglePanelKind(setPanels: SetPanels, kind: PanelKind) {
  setPanels(current => ({ cols: togglePanel(panelCols(current), kind, () => ({ id: newPanelId(), kind })) }))
}

/** What the workspace ⋯ menu offers for this pane. Items that do nothing here stay out; an open panel can always be closed. */
export function extraPanels(desk: DesktopLayout, pane: Pane): { kind: PanelKind; key?: string; open: boolean }[] {
  const cols = panelCols(desk)
  const fits: Record<string, boolean> = { chat: canFork(pane), tasks: pane.agent === 'claude', plan: pane.agent === 'claude' && pane.needs?.kind === 'plan' }
  return (['chat', 'tasks', 'plan'] as PanelKind[]).map(kind => ({ kind, key: kind === 'chat' ? '⌘;' : undefined, open: hasPanel(cols, kind) })).filter(item => item.open || fits[item.kind])
}
