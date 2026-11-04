import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { Bot, Hand, X } from 'lucide-react'
import { errorText } from './api'
import type { Api, Pane } from './api'
import type { BrowserAskRow } from './needs'
import { BrowserPanel } from './BrowserPanel'
import './AgentBrowser.css'

// Agents (Playwright MCP, scripts) drive tabs in Tether's shared browser. See server/browser_cdp.py.
export interface AgentTab { id: string; url: string; title: string }
export interface AgentAsk { id: string; pane: string | null; text: string; at: number }
// Server shape (contract.md): `agents[].tabs` are target ids. The poll adds titles from `browser/tabs`.
interface AgentBrowserWire {
  enabled: boolean; available: boolean; reason: string
  agents: { pane: string | null; tabs: string[]; since: number }[]
  asks: AgentAsk[]
  // Newer servers (spec-s3): can Claude and Codex find the browser, and which running agents started before it was on.
  setup?: { claude: SetupState; codex: SetupState; claude_reason?: string; codex_reason?: string }
  stale?: string[]
}
type SetupState = 'ok' | 'missing' | 'none' | 'manual'
export interface AgentBrowserStatus extends Omit<AgentBrowserWire, 'agents'> {
  agents: { pane: string | null; tabs: AgentTab[]; since: number }[]
}

export const WATCH_EVENT = 'tether-browser-watch'
export const TAB_KEY = 'tether-browser-tab'
const POLL_MS = 4000

// One poll for the whole app, however many chats show the card.
let status: AgentBrowserStatus | null = null
let poller: { api: Api; timer: number } | null = null
const listeners = new Set<() => void>()
function emit() { listeners.forEach(listener => listener()) }
let known: Record<string, AgentTab> = {}
async function accept(api: Api, wire: AgentBrowserWire | null) {
  // Older servers answer 404: treat that as "no agent browser".
  if (!wire || !Array.isArray(wire.agents)) { status = null; emit(); return }
  const ids = wire.agents.flatMap(agent => agent.tabs)
  // Only ask for titles when agents have tabs: then the browser runs already, and `browser/tabs` does not start it.
  if (ids.length) {
    try {
      const rows = (await api<{ tabs: AgentTab[] }>('browser/tabs')).tabs
      known = Object.fromEntries(rows.map(row => [row.id, { id: row.id, url: row.url, title: row.title }]))
    } catch { /* keep the old titles */ }
  }
  status = { ...wire, agents: wire.agents.map(agent => ({ ...agent, tabs: agent.tabs.map(id => known[id] ?? { id, url: '', title: '' }) })) }
  emit()
}
async function poll(api: Api) {
  if (document.visibilityState !== 'visible') return
  let wire: AgentBrowserWire | null = null
  try { wire = await api<AgentBrowserWire>('browser/agents') } catch { /* off or old server */ }
  await accept(api, wire)
}
function subscribe(api: Api) {
  return (listener: () => void) => {
    listeners.add(listener)
    if (!poller) {
      poller = { api, timer: window.setInterval(() => void poll(api), POLL_MS) }
      void poll(api)
    }
    return () => {
      listeners.delete(listener)
      if (!listeners.size && poller) { clearInterval(poller.timer); poller = null }
    }
  }
}
const subscribers = new WeakMap<Api, (listener: () => void) => () => void>()
export function useAgentBrowser(api: Api) {
  let sub = subscribers.get(api)
  if (!sub) { sub = subscribe(api); subscribers.set(api, sub) }
  return useSyncExternalStore(sub, () => status)
}
export async function changeAgentBrowser(api: Api, path: 'browser/agents' | 'browser/ask/dismiss' | 'browser/agents/setup', body: unknown) {
  await accept(api, await api<AgentBrowserWire>(path, body))
}

/** Open asks for the "Needs you" list, each with its agent's first tab. */
export function browserAsks(state: AgentBrowserStatus | null): BrowserAskRow[] {
  return (state?.asks ?? []).map(ask => ({ ...ask, tab: state?.agents.find(agent => agent.pane === ask.pane)?.tabs[0]?.id }))
}

// Chat names for the owner label in the browser panel, as the sidebar shows them. App.tsx keeps this current.
let labels: Record<string, string> = {}
const labelListeners = new Set<() => void>()
export function setPaneLabels(panes: { id: string; label: string }[]) {
  const next: Record<string, string> = {}
  for (const pane of panes) next[pane.id] = pane.label
  if (JSON.stringify(next) === JSON.stringify(labels)) return
  labels = next
  labelListeners.forEach(listener => listener())
}
const subscribeLabels = (listener: () => void) => { labelListeners.add(listener); return () => { labelListeners.delete(listener) } }
export function usePaneLabel(pane: string | null | undefined) {
  const all = useSyncExternalStore(subscribeLabels, () => labels)
  return pane ? all[pane] ?? null : null
}

/** Open the browser on an agent's tab: the desktop panel, or the phone sheet. App.tsx listens. */
export function watchTab(tab: string, pane: string) {
  try { localStorage.setItem(TAB_KEY, tab) } catch { /* private mode: the panel picks the first tab */ }
  window.dispatchEvent(new CustomEvent(WATCH_EVENT, { detail: { tab, pane } }))
}

export function hostOf(url: string) {
  try { return new URL(url).host || url } catch { return url }
}

/** Status stack items for one chat: its agent's tabs, and a request for the person when there is one. */
export function agentBrowserItems(api: Api, state: AgentBrowserStatus | null, pane: string) {
  const tabs = state?.agents.filter(agent => agent.pane === pane).flatMap(agent => agent.tabs) ?? []
  const ask = state?.asks.find(item => item.pane === pane) ?? null
  if (!tabs.length && !ask) return []
  const first = tabs.find(tab => tab.url && tab.url !== 'about:blank') ?? tabs[0]
  const label = ask ? 'Needs you in the browser' : first ? `Browsing ${hostOf(first.url) || 'a page'}` : 'Browser'
  return [{ id: ask ? 'browser-ask' : 'browser', label, active: true, content: <AgentBrowserCard api={api} pane={pane} tabs={tabs} ask={ask} /> }]
}

function AgentBrowserCard({ api, pane, tabs, ask }: { api: Api; pane: string; tabs: AgentTab[]; ask: AgentAsk | null }) {
  const [error, setError] = useState('')
  const watchFirst = () => { if (tabs[0]) watchTab(tabs[0].id, pane) }
  return <div className="agent-browser-card">
    {ask && <div className="agent-browser-ask" role="alert">
      <Hand size={16} aria-hidden="true" />
      <p>{ask.text}</p>
      <div className="agent-browser-actions">
        {tabs.length > 0 && <button type="button" className="primary" onClick={watchFirst}>Open the page</button>}
        <button type="button" className="secondary" onClick={() => void changeAgentBrowser(api, 'browser/ask/dismiss', { id: ask.id }).catch(failure => setError(errorText(failure)))}>Done</button>
      </div>
    </div>}
    {tabs.length > 0 && <ul className="agent-browser-tabs">
      {tabs.map(tab => {
        // Chromium uses the URL as the title when a page has none.
        const title = tab.title && tab.title !== tab.url ? tab.title : ''
        return <li key={tab.id}>
        <Bot size={15} aria-hidden="true" />
        <span><strong>{title || hostOf(tab.url) || 'New tab'}</strong>{title && <small>{hostOf(tab.url)}</small>}</span>
        <button type="button" className="secondary" onClick={() => watchTab(tab.id, pane)}>Watch</button>
      </li>
      })}
    </ul>}
    {!tabs.length && ask && <p className="setting-note">The agent has no page open yet.</p>}
    {error && <p className="error" role="alert">{error}</p>}
  </div>
}

/** Phone: the browser panel full screen, over the chat. */
export function BrowserSheet({ api, pane, onClose }: { api: Api; pane: Pane; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const box = dialog.current
    if (box && !box.open) box.showModal()
  }, [])
  return <dialog ref={dialog} className="browser-sheet" aria-label="Browser" onCancel={event => { event.preventDefault(); onClose() }}>
    <header className="browser-sheet-head">
      <button type="button" className="icon-button" aria-label="Close the browser" onClick={onClose}><X /></button>
      <strong>Browser</strong>
    </header>
    <BrowserPanel api={api} pane={pane} folder={null} active />
  </dialog>
}

/** Settings › System: the switch and why it is off. */
export function AgentBrowserSettings({ api }: { api: Api }) {
  const state = useAgentBrowser(api)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  if (!state) return null
  const connected = state.agents.length
  const setup = async (agent: 'claude' | 'codex') => {
    setBusy(true); setError('')
    try { await changeAgentBrowser(api, 'browser/agents/setup', { agent }) } catch (failure) { setError(errorText(failure)) } finally { setBusy(false) }
  }
  const note = !state.enabled ? 'Off. Agents start their own hidden browser.'
    : !state.available ? `Not available: ${state.reason || 'unknown reason'}.`
    : connected ? `On · ${connected} ${connected === 1 ? 'agent' : 'agents'} connected.` : 'On · Agents started from now on use it.'
  return <section className="settings-group" aria-labelledby="settings-agent-browser"><h3 id="settings-agent-browser">Shared browser</h3>
    {error && <p className="error" role="alert">{error}</p>}
    <label className="switch-row"><span><strong>Agents use Tether’s browser</strong><small>{note}</small></span>
      <input type="checkbox" role="switch" checked={state.enabled} disabled={busy} onChange={event => {
        setBusy(true); setError('')
        void changeAgentBrowser(api, 'browser/agents', { enabled: event.target.checked }).catch(failure => setError(errorText(failure))).finally(() => setBusy(false))
      }} /></label>
    {state.enabled && state.setup && (['claude', 'codex'] as const).filter(agent => state.setup![agent] !== 'none').map(agent => {
      const name = agent === 'claude' ? 'Claude Code' : 'Codex'
      const value = state.setup![agent]
      const note = value === 'ok' ? 'Ready.'
        : value === 'missing' ? (agent === 'claude' ? 'Claude does not know about the shared browser yet.' : 'Codex blocks the browser address for Playwright MCP.')
        : state.setup![`${agent}_reason` as const] || 'Change the settings file by hand.'
      return <div className="switch-row agent-browser-setup" key={agent}>
        <span><strong>{name}</strong><small>{note}</small></span>
        {value === 'missing' && <button type="button" className="secondary" disabled={busy} onClick={() => void setup(agent)}>Set up</button>}
      </div>
    })}
    {state.enabled && !!state.stale?.length && <StaleAgents panes={state.stale} />}
    <p className="setting-note">You can watch their tabs in the browser. Set up changes your Claude or Codex settings file and keeps a copy of the old file.</p>
  </section>
}

function StaleAgents({ panes }: { panes: string[] }) {
  const all = useSyncExternalStore(subscribeLabels, () => labels)
  const [open, setOpen] = useState(false)
  const names = panes.map(pane => all[pane] ?? pane)
  const one = names.length === 1
  return <div className="setting-note agent-browser-stale" role="status">
    <p>{one ? `${names[0]} started before this was on.` : `${names.length} agents started before this was on.`} {one ? 'Restart it to use the shared browser.' : 'Restart an agent to let it use the shared browser.'}</p>
    {!one && <button type="button" className="agent-browser-names" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? 'Hide names' : 'Show names'}</button>}
    {!one && open && <ul>{names.map((name, index) => <li key={panes[index]}>{name}</li>)}</ul>}
  </div>
}
