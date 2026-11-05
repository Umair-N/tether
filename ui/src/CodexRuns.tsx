import { useEffect, useState } from 'react'
import type { Api } from './api'
import { Elapsed, formatElapsed } from './Notices'
import './CodexRuns.css'

// Codex runs that the agent in this pane started (`codex-lane` or `codex exec`), read from each run's Codex session file.
export type CodexRunState = 'starting' | 'running' | 'stalled' | 'done' | 'failed'
export interface CodexRunStep { at: number; kind: 'tool' | 'message' | 'thinking'; text: string }
export interface CodexRun { id: string; pid: number; started: number; cwd: string; prompt: string; steps: CodexRunStep[]; state: CodexRunState; last_activity: number; final: string | null }
export interface CodexRuns { pane: string; running: number; runs: CodexRun[] }

const LIVE: CodexRunState[] = ['starting', 'running', 'stalled']
const STATE_LABEL: Record<CodexRunState, string> = { starting: 'Starting', running: 'Running', stalled: 'Stalled', done: 'Done', failed: 'Failed' }

/** Polls every 3 s while a run lives, else every 15 s. Stops while the page is hidden. */
export function useCodexRuns(api: Api, pane: string, enabled: boolean) {
  const [value, setValue] = useState<CodexRuns | null>(null)
  useEffect(() => {
    if (!enabled) { setValue(null); return }
    let stopped = false, timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    const read = () => {
      clearTimeout(timer)
      if (document.hidden) return
      api<CodexRuns>(`codex-runs?session=${encodeURIComponent(pane)}`, undefined, controller.signal).then(result => {
        if (stopped) return
        setValue(result.pane === pane ? result : null)
        timer = setTimeout(read, result.runs.some(run => LIVE.includes(run.state)) ? 3000 : 15000)
      }, () => { if (!stopped) timer = setTimeout(read, 15000) })
    }
    const visible = () => { if (!document.hidden) read() }
    read()
    document.addEventListener('visibilitychange', visible)
    return () => { stopped = true; controller.abort(); clearTimeout(timer); document.removeEventListener('visibilitychange', visible) }
  }, [api, pane, enabled])
  return value?.pane === pane ? value : null
}

function chipLabel(runs: CodexRun[]) {
  const live = runs.filter(run => LIVE.includes(run.state)).length
  if (live) return `Codex · ${live} running`
  const failed = runs.filter(run => run.state === 'failed').length
  return failed ? `Codex · ${failed} failed` : `Codex · ${runs.length} done`
}

/** The status-stack item: a chip that opens the list of runs. */
export function codexRunItems(value: CodexRuns | null) {
  const runs = value?.runs ?? []
  return [{ id: 'codex-runs', label: chipLabel(runs), active: runs.length > 0, content: <CodexRunList runs={runs} /> }]
}

function folder(path: string) {
  const parts = path.split('/').filter(Boolean)
  return parts.slice(-2).join('/') || path
}

function ago(at: number) {
  return `${formatElapsed((Date.now() - at) / 1000)} ago`
}

// The final message often repeats as the last step; show it once, as the result.
function shownSteps(run: CodexRun) {
  const last = run.steps[run.steps.length - 1]
  const final = run.final?.replace(/\s+/g, ' ').trim()
  return last && final && last.kind === 'message' && final.startsWith(last.text.replace(/…$/, '')) ? run.steps.slice(0, -1) : run.steps
}

function CodexRunList({ runs }: { runs: CodexRun[] }) {
  return <ul className="codex-runs" aria-label="Codex runs">
    {runs.map(run => ({ ...run, steps: shownSteps(run) })).map(run => <li key={run.id} className={`codex-run ${run.state}`}>
      <div className="codex-run-head">
        <span className={`badge ${run.state === 'running' || run.state === 'starting' ? 'accent' : run.state === 'done' ? '' : 'danger'}`}>{STATE_LABEL[run.state]}</span>
        <span className="codex-run-time">{LIVE.includes(run.state) ? <Elapsed since={run.started} /> : formatElapsed((run.last_activity - run.started) / 1000)}</span>
        {run.cwd && <span className="codex-run-cwd" title={run.cwd}>{folder(run.cwd)}</span>}
        {run.state === 'stalled' && <span className="codex-run-note">no new step for {ago(run.last_activity).replace(' ago', '')}</span>}
      </div>
      {run.prompt && <p className="codex-run-prompt" title={run.prompt}>{run.prompt}</p>}
      {run.steps.length > 0 && <ol className="codex-run-steps">
        {run.steps.map((step, index) => <li key={`${step.at}-${index}`} className={step.kind}><span>{step.text}</span><time dateTime={new Date(step.at).toISOString()}>{ago(step.at)}</time></li>)}
      </ol>}
      {run.final && <p className="codex-run-final"><strong>Result</strong>{run.final}</p>}
    </li>)}
  </ul>
}
