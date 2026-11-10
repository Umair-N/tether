import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Check, Copy } from 'lucide-react'
import type { Pane } from './api'
import { copyText } from './clipboard'
import { agentNames, paneTitle, sessionName } from './Sidebar'

export type NameKind = 'session' | 'agent' | 'pane' | 'tmux'
interface PaneName { kind: NameKind; key: string; label: string; value: string; empty: string }

/** The letter that copies each name: alone in the pane menu, with ⌥⇧ anywhere in the workspace (App.tsx). */
export const NAME_KEYS: Record<NameKind, string> = { session: 'S', agent: 'C', pane: 'P', tmux: 'T' }
const KIND_OF_CODE: Record<string, NameKind> = { KeyS: 'session', KeyC: 'agent', KeyP: 'pane', KeyT: 'tmux' }
export const nameKindOf = (code: string): NameKind | undefined => KIND_OF_CODE[code]

/** The four names of a pane. `empty` says why a name is missing; that row stays visible but disabled. */
export function paneNames(pane: Pane): PaneName[] {
  const agent = pane.agent === 'claude' || pane.agent === 'codex' ? agentNames[pane.agent] : ''
  return [
    { kind: 'session', key: NAME_KEYS.session, label: 'Session name', value: paneTitle(pane), empty: '' },
    { kind: 'agent', key: NAME_KEYS.agent, label: `${agent || 'Claude'} session name`, value: pane.agent_name ?? '', empty: agent ? 'No name yet. Use /rename.' : 'No agent runs here.' },
    { kind: 'pane', key: NAME_KEYS.pane, label: 'tmux pane name', value: pane.tmux_pane_name ?? '', empty: 'Not named in tmux.' },
    { kind: 'tmux', key: NAME_KEYS.tmux, label: 'tmux session name', value: sessionName(pane), empty: '' },
  ]
}

/** Copy one name of a pane. Returns the line to show, for the workspace shortcut's toast. */
export async function copyPaneName(pane: Pane, kind: NameKind): Promise<string> {
  const name = paneNames(pane).find(item => item.kind === kind)!
  if (!name.value) return `${name.label}: ${name.empty}`
  try { await copyText(name.value); return `Copied ${name.label}: ${name.value}` } catch { return 'Could not copy. Select the name and copy it.' }
}

/** "Copy name" in a pane or session menu. S, C, P and T copy at once, also while the group is closed. */
export function CopyNames({ pane, disabled = false, onCopied }: { pane: Pane; disabled?: boolean; onCopied: () => void }) {
  const [open, setOpen] = useState(false)
  const [done, setDone] = useState<NameKind | null>(null)
  const [error, setError] = useState('')
  const names = paneNames(pane)
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])
  async function copy(name: PaneName) {
    if (!name.value || disabled) return
    try { await copyText(name.value) } catch { setOpen(true); setError('Could not copy. Select the name and copy it.'); return }
    setDone(name.kind)
    timer.current = setTimeout(onCopied, 600)  // Show the check, then close the menu.
  }
  const copyRef = useRef(copy)
  useLayoutEffect(() => { copyRef.current = copy })
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return
      if ((event.target as Element | null)?.closest('input, textarea, select, [contenteditable="true"]')) return
      const kind = nameKindOf(event.code)
      const name = kind && paneNames(pane).find(item => item.kind === kind)
      if (!name) return
      event.preventDefault()
      void copyRef.current(name)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [pane])
  return <>
    <button type="button" aria-expanded={open} disabled={disabled} onClick={() => setOpen(!open)}>
      <Copy size={18} />Copy name<kbd aria-hidden="true">{names.map(name => name.key).join(' ')}</kbd>
    </button>
    {open && <div className="copy-names" role="group" aria-label="Copy name">
      {names.map(name => <button type="button" key={name.kind} disabled={disabled || !name.value} title={name.value || name.empty}
        aria-label={`Copy ${name.label}${name.value ? `: ${name.value}` : ''}`} aria-keyshortcuts={name.key} onClick={() => void copy(name)}>
        {done === name.kind ? <Check size={16} className="copy-names-done" /> : <Copy size={16} />}
        <span className="copy-names-label">{name.label}</span>
        <span className="copy-names-value">{done === name.kind ? 'Copied' : name.value || name.empty}</span>
        <kbd>{name.key}</kbd>
      </button>)}
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
  </>
}
