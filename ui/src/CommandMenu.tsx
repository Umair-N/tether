import { useMemo, useState } from 'react'
import { Search } from 'lucide-react'
import type { Pane } from './api'
import { useModal } from './hooks'
import { fuzzyScore, keyLabel } from './keys'
import { agentNames, paneTitle, shownIndex, status } from './Sidebar'
import type { Session } from './Sidebar'

/** One command of the menu. Leave a command out when its action does not apply now. */
export interface Command { label: string; key?: string; run: () => void }

interface Row { id: string; group: 'Panes' | 'Commands'; label: string; hint?: string; key?: string; dot?: string; run: () => void }

interface Props {
  sessions: Session[]
  seen: Record<string, number>
  selected?: string
  /** Pane ids in slot order: their rows show the slot key. */
  slots: string[]
  commands: Command[]
  onPane: (pane: Pane) => void
  onSession: (session: Session) => void
  onClose: () => void
}

/** ⌘K: go to a pane or session, or run a command. Each command row shows its key, so the menu teaches the keys. */
export function CommandMenu({ sessions, seen, selected, slots, commands, onPane, onSession, onClose }: Props) {
  const modal = useModal(true, onClose)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const rows = useMemo(() => {
    const panes: Row[] = sessions.flatMap(session => {
      const one = session.panes.length === 1
      const paneRows = session.panes.map(pane => {
        const state = status([pane], seen, selected)
        const at = slots.indexOf(pane.id)
        return { id: pane.id, group: 'Panes' as const, label: one ? session.name : `${session.name} · ${[paneTitle(pane), shownIndex(pane)].filter(Boolean).join(' ')}`, hint: `${one ? agentNames[pane.agent] + ' · ' : ''}${state.label}`, key: at < 0 ? undefined : `⌥${at + 1}`, dot: state.kind, run: () => onPane(pane) }
      })
      return one ? paneRows : [{ id: `session:${session.id}`, group: 'Panes' as const, label: session.name, hint: `Show all ${Math.min(4, session.panes.length)} panes`, run: () => onSession(session) }, ...paneRows]
    })
    const commandRows: Row[] = commands.map(command => ({ id: `command:${command.label}`, group: 'Commands', ...command }))
    const words = query.trim()
    if (!words) return [...commandRows, ...panes]
    // A pane also matches on its folder and agent, the same fields the sidebar search reads. A match in the name ranks first.
    const scoreOf = (row: Row, extra: string) => { const named = fuzzyScore(row.label, words); return named !== null ? named * 2 : fuzzyScore(`${row.label} ${extra}`, words) }
    const ranked = (list: Row[], extra: (row: Row) => string) => list
      .map(row => ({ row, score: scoreOf(row, extra(row)) }))
      .filter((item): item is { row: Row; score: number } => item.score !== null)
      .sort((a, b) => b.score - a.score)
      .map(item => item.row)
    const paneText = (row: Row) => { const pane = sessions.flatMap(session => session.panes).find(item => item.id === row.id); return pane ? `${pane.dir} ${agentNames[pane.agent]}` : '' }
    return [...ranked(panes, paneText), ...ranked(commandRows, () => '')]
  }, [sessions, seen, selected, slots, commands, onPane, onSession, query])
  const at = Math.min(active, Math.max(0, rows.length - 1))
  const pick = (row: Row | undefined) => { if (!row) return; onClose(); row.run() }
  const move = (to: number) => {
    const next = (to + rows.length) % rows.length
    setActive(next)
    document.getElementById(`command-${next}`)?.scrollIntoView({ block: 'nearest' })
  }
  return <dialog {...modal} className="sheet command-menu" aria-label="Command menu">
    <label className="command-search"><Search size={17} aria-hidden="true" />
      <input autoFocus role="combobox" aria-expanded="true" aria-controls="command-list" aria-autocomplete="list" aria-activedescendant={rows.length ? `command-${at}` : undefined}
        aria-label="Go to a pane or run a command" placeholder="Go to a pane or run a command" value={query}
        onChange={event => { setQuery(event.target.value); setActive(0) }}
        onKeyDown={event => {
          if (!rows.length) return
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); move(at + (event.key === 'ArrowDown' ? 1 : -1)) }
          else if (event.key === 'Home' && event.ctrlKey) { event.preventDefault(); move(0) }
          else if (event.key === 'End' && event.ctrlKey) { event.preventDefault(); move(rows.length - 1) }
          else if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); pick(rows[at]) }
        }} />
      <kbd aria-hidden="true">Esc</kbd>
    </label>
    <div id="command-list" role="listbox" tabIndex={-1} aria-label="Results" className="command-list">
      {rows.map((row, index) => <div key={row.id} role="presentation">
        {(index === 0 || rows[index - 1].group !== row.group) && <div className="command-group" role="presentation">{row.group}</div>}
        <div id={`command-${index}`} role="option" aria-selected={index === at} className={index === at ? 'command-row active' : 'command-row'}
          onMouseMove={() => { if (index !== at) setActive(index) }} onMouseDown={event => event.preventDefault()} onClick={() => pick(row)}>
          <span className="state-slot" aria-hidden="true">{row.dot && <span className={`state-dot ${row.dot}`} />}</span>
          <span className="command-label">{row.label}</span>
          {row.hint && <span className="command-hint">{row.hint}</span>}
          {row.key && <kbd>{keyLabel(row.key)}</kbd>}
        </div>
      </div>)}
      {!rows.length && <p className="command-empty">No pane or command matches.</p>}
    </div>
  </dialog>
}
