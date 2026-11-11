import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, ChevronRight, X } from 'lucide-react'
import { errorText } from './api'
import type { Api, ToolSummary } from './api'
import { useModal, usePolling } from './hooks'
import { formatElapsed } from './Notices'
import { Loading } from './Skeleton'
import { RetryButton } from './Retry'
import { ToolRow } from './ToolGroup'
import './AgentMap.css'

// Claude Code subagents of one pane's session: a pill in the composer bar opens a map (the main agent and what it
// spawned); a tap on an agent shows its prompt, its tool calls, its report and its transcript. Data comes from the
// subagent transcripts beside the session transcript, read by the server.
export type AgentState = 'running' | 'finished' | 'failed' | 'stopped'
export interface SubAgent { id: string; description: string; type: string; model: string | null; effort: string | null; background: boolean; state: AgentState; started: string | null; ended: string | null; seconds: number | null; tokens: number | null; tools: number; depth: number; parent: string | null; summary: string | null }
export interface SubAgents { main: { model: string | null; context: number | null; title: string | null }; agents: SubAgent[] }
type Step = { kind: 'text'; text: string } | ({ kind: 'tool' } & ToolSummary)
interface AgentDetail extends SubAgent { prompt: string; steps: Step[]; steps_cut?: boolean; report: string | null }

const STATE_LABEL: Record<AgentState, string> = { running: 'Running', finished: 'Finished', failed: 'Failed', stopped: 'Stopped' }

// "claude-opus-5-5" → "Opus 5.5"; short names ("sonnet") get a capital letter.
export function modelName(model: string | null | undefined) {
  if (!model) return ''
  const found = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?/.exec(model)
  if (found) return `${found[1][0].toUpperCase()}${found[1].slice(1)} ${found[2]}${found[3] ? `.${found[3]}` : ''}`
  return model[0].toUpperCase() + model.slice(1)
}
export const tokenText = (tokens: number | null) => tokens == null ? '' : tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k tokens` : `${tokens} tokens`
const facts = (...parts: (string | false | null | undefined)[]) => parts.filter(Boolean).join(' · ')

// Running agents poll every 4 s; an idle list every 15 s. The pill hides while a session has no subagents.
export function useSubAgents(api: Api, session: string, enabled: boolean) {
  const [fast, setFast] = useState(false)
  const fetcher = useCallback((signal: AbortSignal) => enabled
    ? api<SubAgents>(`subagents?session=${encodeURIComponent(session)}`, undefined, signal).then(value => ({ ...value, session }))
    : Promise.resolve({ main: { model: null, context: null, title: null }, agents: [], session }), [api, session, enabled])
  const { data, error, refresh } = usePolling<SubAgents & { session: string }>(fetcher, fast ? 4000 : 15000)
  // A pane switch keeps the old list until the new one loads; never show another pane's agents.
  const list = enabled && data?.session === session ? data : undefined
  const running = list?.agents.filter(agent => agent.state === 'running').length ?? 0
  // The poll interval follows the newest list (state adjusted during render, not in an effect).
  if (fast !== running > 0) setFast(running > 0)
  return { data: list, error, refresh, running }
}

export function AgentPill({ list, onOpen }: { list: SubAgents | undefined; onOpen: () => void }) {
  const total = list?.agents.length ?? 0
  if (!total) return null
  const running = list!.agents.filter(agent => agent.state === 'running').length
  const label = running ? `${running} ${running === 1 ? 'agent' : 'agents'}` : `${total} ${total === 1 ? 'agent' : 'agents'}`
  return <button type="button" className={`pill agent-pill ${running ? 'running' : ''}`} aria-haspopup="dialog" aria-label={`${label}${running ? ' running' : ''}. Open the agent map`} onClick={onOpen}>
    <span className={`agent-dot ${running ? 'running' : 'finished'}`} aria-hidden="true" />{label}
  </button>
}

function Dot({ state }: { state: AgentState }) {
  return <span className={`agent-dot ${state}`} role="img" aria-label={STATE_LABEL[state]} />
}

export function AgentMap({ api, session, list, onClose }: { api: Api; session: string; list: SubAgents | undefined; onClose: () => void }) {
  const [open, setOpen] = useState<string | null>(null)
  const modal = useModal(true, onClose)
  const agents = list?.agents ?? []
  const running = agents.filter(agent => agent.state === 'running').length
  const chosen = agents.find(agent => agent.id === open)
  return <dialog {...modal} className="sheet agent-map" aria-labelledby="agent-map-title">
    {chosen ? <AgentView api={api} session={session} agent={chosen} onBack={() => setOpen(null)} onClose={onClose} /> : <>
      <div className="sheet-heading"><h2 id="agent-map-title">Agent map</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
      <p className="sheet-sub">{facts(`${agents.length} ${agents.length === 1 ? 'agent' : 'agents'}`, running > 0 && `${running} running`)}</p>
      <div className="agent-tree">
        <div className="agent-node main"><span className={`agent-dot ${running ? 'running' : 'finished'}`} aria-hidden="true" /><span className="agent-node-text"><strong>{list?.main.title || 'Main agent'}</strong><small>{facts(modelName(list?.main.model), list?.main.context != null && `${tokenText(list.main.context)} in context`)}</small></span></div>
        <Children agents={agents} parent={null} onOpen={setOpen} />
      </div>
    </>}
  </dialog>
}

// Each agent sits under the agent that spawned it; an agent whose parent is unknown sits under the main agent.
function Children({ agents, parent, onOpen }: { agents: SubAgent[]; parent: string | null; onOpen: (id: string) => void }) {
  const known = new Set(agents.map(agent => agent.id))
  const here = agents.filter(agent => parent === null ? !agent.parent || !known.has(agent.parent) : agent.parent === parent)
  if (!here.length) return null
  return <ul className="agent-children" aria-label={parent === null ? 'Spawned agents' : undefined}>
    {here.map(agent => <li key={agent.id}>
      <button type="button" className="agent-node" onClick={() => onOpen(agent.id)}>
        <Dot state={agent.state} />
        <span className="agent-node-text"><strong>{agent.description || agent.type}</strong><small>{facts(agent.seconds != null && formatElapsed(agent.seconds), tokenText(agent.tokens), modelName(agent.model))}</small></span>
        <ChevronRight size={15} className="chevron" aria-hidden="true" />
      </button>
      <Children agents={agents} parent={agent.id} onOpen={onOpen} />
    </li>)}
  </ul>
}

function AgentView({ api, session, agent, onBack, onClose }: { api: Api; session: string; agent: SubAgent; onBack: () => void; onClose: () => void }) {
  const [detail, setDetail] = useState<AgentDetail | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [busy, setBusy] = useState(false)
  const [transcript, setTranscript] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [stopError, setStopError] = useState('')
  // Stop drives Claude's /tasks dialog in the pane; the server checks the screen after every key and closes it again.
  const stop = async () => {
    setStopping(true); setStopError('')
    try { await api('subagent-stop', { session, id: agent.id }) } catch (reason) { setStopError(errorText(reason)) }
    setStopping(false)
  }
  // Reload when the list says the agent moved on (more tools, a new state).
  useEffect(() => {
    const controller = new AbortController()
    void api<AgentDetail>(`subagent?session=${encodeURIComponent(session)}&id=${encodeURIComponent(agent.id)}`, undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) { setDetail(value); setError(''); setBusy(false) } })
      .catch(reason => { if (!controller.signal.aborted) { setError(errorText(reason)); setBusy(false) } })
    return () => controller.abort()
  }, [api, session, agent.id, agent.state, agent.tools, attempt])
  const shown = detail ?? agent
  const tools = detail?.steps.filter((step): step is Step & { kind: 'tool' } => step.kind === 'tool') ?? []
  const path = (call: string) => `subagent-tool?session=${encodeURIComponent(session)}&agent=${encodeURIComponent(agent.id)}&id=${encodeURIComponent(call)}`
  const live = shown.state === 'running'
  return <>
    <div className="sheet-heading">
      <button type="button" className="icon-button" aria-label="Back to the agent map" onClick={onBack}><ArrowLeft /></button>
      <h2 id="agent-map-title" className="agent-title"><Dot state={shown.state} />{shown.description || shown.type}</h2>
      <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button>
    </div>
    <p className="agent-facts">{facts(STATE_LABEL[shown.state], shown.type, modelName(shown.model), shown.effort, shown.background && 'background')}<br />
      {facts(shown.seconds != null && formatElapsed(shown.seconds), tokenText(shown.tokens), `${shown.tools} tool ${shown.tools === 1 ? 'call' : 'calls'}`)}</p>
    {error ? <p role="alert">{error} <RetryButton label="Retry" className="" busy={busy} onRetry={() => { setBusy(true); setAttempt(attempt + 1) }} /></p>
      : !detail ? <Loading label="Loading agent…" /> : <div className="agent-body">
        {(shown.state === 'failed' || shown.state === 'stopped') && shown.summary && <p className="agent-summary">{shown.summary}</p>}
        {detail.report && <section aria-label="Report"><h3>Report</h3><pre className="agent-text" tabIndex={0}>{detail.report}</pre></section>}
        <details><summary>Prompt</summary><pre className="agent-text" tabIndex={0}>{detail.prompt}</pre></details>
        <details open={!detail.report}><summary>Tool calls ({tools.length})</summary>
          {tools.length ? <div className="tool-row tool-group agent-tools"><ul className="tool-list">{tools.map((tool, index) => <ToolRow key={`${tool.id}:${index}`} tool={tool} api={api} session={session} live={live} onOpen={() => {}} path={path(tool.id)} />)}</ul></div> : <p className="hint">No tool calls yet.</p>}
        </details>
        <details open={transcript} onToggle={event => setTranscript(event.currentTarget.open)}><summary>Transcript</summary>
          {transcript && <ol className="agent-steps">
            {detail.steps_cut && <li className="hint">Earlier steps are not shown.</li>}
            {detail.steps.map((step, index) => step.kind === 'text'
              ? <li key={index} className="agent-step-text">{step.text}</li>
              : <li key={index} className={`agent-step-tool ${step.is_error ? 'failed' : ''}`}><strong>{step.name}</strong> <span>{step.description || step.title}</span></li>)}
          </ol>}
        </details>
      </div>}
    {live && <div className="agent-actions">
      <button type="button" className="secondary agent-stop" disabled={stopping} onClick={() => void stop()}>{stopping ? 'Stopping…' : 'Stop agent'}</button>
      {stopError && <p role="alert">{stopError}</p>}
    </div>}
  </>
}
