import type { Pane, UsageReport } from './api'
import { needsYou } from './Sidebar'
import { useSharedUsage } from './usage'

interface Props { panes: Pane[]; loading: boolean; error: string }

/** One quiet line under the desktop workspace: connection, what runs, plan limits. */
export function StatusBar({ panes, loading, error }: Props) {
  const usage = useSharedUsage()
  const working = panes.filter(pane => pane.state === 'Working').length
  const waiting = panes.filter(pane => needsYou(pane.state)).length
  const connection = error ? { kind: 'error', label: 'Reconnecting…', title: error } : loading ? { kind: 'idle', label: 'Connecting…', title: undefined } : { kind: 'ok', label: 'Connected', title: undefined }
  const limits = (name: string, report: UsageReport | null | undefined) => report?.windows.map(window => {
    const percent = Math.max(0, Math.min(100, Math.round(window.percent)))
    return <span key={name + window.label} className={percent >= 90 ? 'high' : percent >= 70 ? 'warn' : undefined} title={`${name} ${window.label} limit: ${percent}% used`}>{name} {window.label} {percent}%</span>
  })
  return <footer className="status-bar" aria-label="Status">
    <span className="status-connection" title={connection.title}><span className={`status-light ${connection.kind}`} aria-hidden="true" />{connection.label}</span>
    <span>{working} working{waiting > 0 && <> · <span className="status-needs">{waiting} {waiting === 1 ? 'needs' : 'need'} you</span></>}</span>
    <span className="status-spacer" />
    {usage && <span className="status-limits">{limits('Claude', usage.claude)}{limits('Codex', usage.codex)}</span>}
  </footer>
}
