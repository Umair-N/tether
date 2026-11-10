import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { errorText } from './api'
import type { PanelBodyProps } from './panels'
import { LiveTerminal } from './Terminal'

// One request per panel, so a second mount (React's strict mode) does not start a second shell.
const starting = new Map<string, Promise<string>>()

/** A plain shell in the focused pane's folder, separate from the agent's own pane. It lives in Tether's tmux session
 * "tether-shells" (server: side_shells.py) and stays the same shell while this panel is open, even when focus moves. */
export function TerminalPanel({ api, pane, item, onItem }: PanelBodyProps) {
  const [shell, setShell] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [requestFor, setRequestFor] = useState({ api, id: item.id, attempt })
  if (requestFor.api !== api || requestFor.id !== item.id || requestFor.attempt !== attempt) {
    setRequestFor({ api, id: item.id, attempt }); setError('')
  }
  // The request reads these once per attempt. A changed focus or a saved shell id does not start another shell.
  const latest = useRef({ pane: pane.id, item, onItem })
  useLayoutEffect(() => { latest.current = { pane: pane.id, item, onItem } }, [pane.id, item, onItem])
  useEffect(() => {
    let live = true
    const key = `${item.id}:${attempt}`
    let request = starting.get(key)
    if (!request) {
      const { pane: session, item: own } = latest.current
      request = api<{ pane: string }>('side-shell', { session, pane: own.pane ?? null, new: !!own.fresh, ...own.folder ? { folder: own.folder } : {} }).then(result => result.pane)
      starting.set(key, request)
      void request.catch(() => '').then(() => starting.delete(key))
    }
    request.then(id => {
      if (!live) return
      setShell(id)
      const own = latest.current.item
      if (own.pane !== id || own.fresh) latest.current.onItem({ pane: id, fresh: undefined })
    }, failure => { if (live) setError(errorText(failure)) })
    return () => { live = false }
  }, [api, item.id, attempt])
  if (error) return <div className="panel-empty" role="alert"><p><strong>Could not start a shell.</strong></p><p>{error}</p><button type="button" className="secondary" onClick={() => setAttempt(attempt + 1)}>Try again</button></div>
  if (!shell) return <div className="panel-empty"><p role="status">Starting a shell…</p></div>
  return <div className="terminal-panel"><LiveTerminal key={shell} paneId={shell} onChat={() => {}} shell /></div>
}
