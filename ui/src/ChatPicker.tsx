import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowRightToLine, LoaderCircle, Search, TerminalSquare } from 'lucide-react'
import { ApiError, errorText } from './api'
import type { Api, ChatAgent, HistoryData, Pane, PastChat, SessionsData } from './api'
import { usePolling } from './hooks'
import { AgentIcon, agentNames, ago, needsYou, paneTitle } from './Sidebar'
import { FolderMenu } from './SelectMenu'
import './ChatPicker.css'

const PAGE = 50

export function chatSlug(title: string) {
  const words = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
  return (words || 'resume').slice(0, 40)
}

// The new pane shows up in the session list a moment after the server opens it.
async function waitForPane(api: Api, id: string): Promise<Pane> {
  for (let tries = 0; tries < 15; tries++) {
    const list = await api<SessionsData>('sessions')
    const found = list.sessions.find(pane => pane.id === id)
    if (found) return found
    await new Promise(resolve => setTimeout(resolve, 300))
  }
  throw new Error('The tab opened, but it is not in the list yet. Check the session list.')
}

export type ResumeTarget = { after: string } | { name: string }

/** Open a past chat as a new tab after a pane, or as a new tmux session. */
export async function resumeChat(api: Api, chat: Pick<PastChat, 'id' | 'agent'>, target: ResumeTarget) {
  const result = await api<{ pane: string }>('resume', { id: chat.id, agent: chat.agent, ...target })
  return waitForPane(api, result.pane)
}

/** A new tmux session named after the chat. A name that exists takes the next number. */
export async function resumeInNewSession(api: Api, chat: Pick<PastChat, 'id' | 'agent' | 'title'>) {
  const base = chatSlug(chat.title)
  for (let number = 1; ; number++) {
    try { return await resumeChat(api, chat, { name: number === 1 ? base : `${base}-${number}` }) }
    catch (failure) {
      if (!(failure instanceof ApiError) || failure.status !== 409 || number === 5 || /already open/i.test(failure.message)) throw failure
    }
  }
}

function useDebounced<T>(value: T, ms: number) {
  const [settled, setSettled] = useState(value)
  useEffect(() => { const timer = setTimeout(() => setSettled(value), ms); return () => clearTimeout(timer) }, [value, ms])
  return settled
}

interface HistoryFilter { agent: 'all' | ChatAgent; q: string; dir: string; state?: 'all' | 'closed'; off?: boolean }

/** One page of past chats that stays fresh, plus older pages on request. New filters start over. */
export function useChatHistory(api: Api, { agent, q, dir, state = 'all', off }: HistoryFilter) {
  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: String(PAGE), agent, state })
    if (q) params.set('q', q)
    if (dir) params.set('dir', dir)
    return params.toString()
  }, [agent, q, dir, state])
  const fetcher = useCallback((signal: AbortSignal) => off
    ? Promise.resolve<HistoryData>({ chats: [], total: 0, next: null, counts: { all: 0, claude: 0, codex: 0 }, dirs: [] })
    : api<HistoryData>(`history?${query}`, undefined, signal), [api, query, off])
  const { data, error, loading, refresh } = usePolling(fetcher, 30000)
  const [more, setMore] = useState<{ query: string; chats: PastChat[]; next: number | null }>({ query, chats: [], next: null })
  const [loadingMore, setLoadingMore] = useState(false)
  const extra = more.query === query ? more : { query, chats: [], next: null }
  const next = extra.chats.length ? extra.next : data?.next ?? null
  const seen = new Set<string>()
  const chats = [...(data?.chats ?? []), ...extra.chats].filter(chat => { const key = chat.agent + chat.id; if (seen.has(key)) return false; seen.add(key); return true })
  const loadMore = async () => {
    if (next === null || loadingMore) return
    setLoadingMore(true)
    try {
      const page = await api<HistoryData>(`history?${query}&offset=${next}`)
      setMore({ query, chats: [...extra.chats, ...page.chats], next: page.next })
    } finally { setLoadingMore(false) }
  }
  return { chats, data, error, loading, refresh, next, loadMore, loadingMore }
}

type Source = 'all' | ChatAgent | 'tabs'
const SOURCES: { id: Source; label: string }[] = [{ id: 'all', label: 'All' }, { id: 'claude', label: 'Claude' }, { id: 'codex', label: 'Codex' }, { id: 'tabs', label: 'Open tabs' }]

function dayLabel(time: string) {
  const then = new Date(time), now = new Date()
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const at = then.getTime()
  if (at >= start) return 'Today'
  if (at >= start - 86400000) return 'Yesterday'
  if (at >= start - 6 * 86400000) return 'This week'
  return 'Earlier'
}

const matches = (text: string, q: string) => { const low = text.toLowerCase(); return q.split(/\s+/).filter(Boolean).every(term => low.includes(term)) }

/**
 * Find a past chat or an open tab. Used by the + on the tab strip and by New session.
 * Picking a closed chat runs onChat, picking an open tab runs onTab. onMove adds "Move here" to tabs of other sessions.
 */
export function ChatPicker({ api, exclude, here, onChat, onTab, onMove, disabled }: {
  api: Api; exclude?: string; here?: string; disabled?: boolean
  onChat: (chat: PastChat) => Promise<void>; onTab: (pane: Pane) => void; onMove?: (pane: Pane) => Promise<void>
}) {
  const [text, setText] = useState('')
  const [source, setSource] = useState<Source>('all')
  const [dir, setDir] = useState('')
  const [busy, setBusy] = useState('')
  const [failure, setFailure] = useState('')
  const q = useDebounced(text.trim(), 180)
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => { if (typeof matchMedia === 'function' && matchMedia('(pointer: fine)').matches) field.current?.focus() }, [])
  const fetchTabs = useCallback((signal: AbortSignal) => api<SessionsData>('sessions', undefined, signal), [api])
  const tabs = usePolling(fetchTabs, 5000)
  const history = useChatHistory(api, { agent: source === 'claude' || source === 'codex' ? source : 'all', q, dir, state: 'closed', off: source === 'tabs' })
  const allTabs = useMemo(() => (tabs.data?.sessions ?? []).filter(pane => pane.id !== exclude), [tabs.data, exclude])
  const shownTabs = allTabs.filter(pane => (source === 'all' || source === 'tabs' || pane.agent === source) && (!dir || pane.dir === dir)
    && (!q || matches(`${paneTitle(pane)} ${pane.label} ${pane.agent_name ?? ''} ${pane.dir}`, q)))
  const folders = useMemo(() => {
    const counts = new Map<string, number>()
    if (source !== 'tabs') for (const item of history.data?.dirs ?? []) counts.set(item.dir, item.count)
    for (const pane of allTabs) if (pane.dir) counts.set(pane.dir, (counts.get(pane.dir) ?? 0) + 1)
    if (dir && !counts.has(dir)) counts.set(dir, 0)
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([path, count]) => ({ dir: path, count }))
  }, [history.data?.dirs, allTabs, source, dir])
  const groups = useMemo(() => {
    const list: { label: string; chats: PastChat[] }[] = []
    for (const chat of history.chats) {
      const label = dayLabel(chat.time)
      const last = list[list.length - 1]
      if (last?.label === label) last.chats.push(chat)
      else list.push({ label, chats: [chat] })
    }
    return list
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [history.data, history.chats.length])
  const run = async (key: string, work: () => Promise<void>) => {
    if (busy || disabled) return
    setBusy(key); setFailure('')
    try { await work() } catch (error) { setFailure(errorText(error)) } finally { setBusy('') }
  }
  // Up and Down walk the rows from the search field; Enter opens the first row.
  const walk = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const rows = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('.cp-main:not(:disabled)')]
    const inField = event.target === field.current
    if (inField && event.key === 'Enter') { event.preventDefault(); rows[0]?.click(); return }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    if (!(event.target instanceof HTMLInputElement) && !(event.target instanceof HTMLButtonElement)) return
    const at = rows.indexOf(event.target as HTMLButtonElement)
    if (!inField && at < 0) return
    event.preventDefault()
    event.stopPropagation()
    if (event.key === 'ArrowDown') rows[inField ? 0 : Math.min(at + 1, rows.length - 1)]?.focus()
    else if (!inField) (at === 0 ? field.current : rows[at - 1])?.focus()
  }
  const empty = !shownTabs.length && !history.chats.length && !history.loading && !tabs.loading
  const filtered = !!(q || dir || source !== 'all')
  return <div className="cp" onKeyDown={walk}>
    <label className="cp-search"><Search size={15} aria-hidden="true" />
      <input ref={field} type="search" value={text} placeholder="Search chats and tabs" aria-label="Search chats and tabs" autoComplete="off" onChange={event => setText(event.target.value)} />
    </label>
    <div className="cp-filters">
      <div className="cp-sources" role="radiogroup" aria-label="Show">
        {SOURCES.map(item => <button type="button" key={item.id} role="radio" aria-checked={source === item.id} className={source === item.id ? 'on' : ''} onClick={() => setSource(item.id)}>{item.label}</button>)}
      </div>
      <FolderMenu value={dir} folders={folders} onChange={setDir} />
    </div>
    {failure && <p className="error cp-error" role="alert">{failure}</p>}
    <div className="cp-list" tabIndex={-1}>
      {history.error && source !== 'tabs' && <p className="error cp-error" role="alert">{history.error} <button type="button" className="link-button" onClick={() => void history.refresh()}>Retry</button></p>}
      {shownTabs.length > 0 && <section><h3>Open tabs</h3><ul>{shownTabs.map(pane => <li key={pane.id}>
        <button type="button" className="cp-main" disabled={!!busy} onClick={() => onTab(pane)}>
          <span className="cp-icon"><AgentIcon agent={pane.agent} icon={pane.pane_icon} size={15} /></span>
          <span className="cp-text"><span className="cp-title">{paneTitle(pane)}</span><span className="cp-meta"><span className="cp-session"><TerminalSquare size={11} aria-hidden="true" />{pane.label}</span>{pane.dir && <bdi dir="ltr">{pane.dir}</bdi>}</span></span>
          <span className={`cp-end ${pane.state === 'Working' ? 'working' : needsYou(pane.state) ? 'needs' : ''}`}>{needsYou(pane.state) ? 'Needs you' : pane.state === 'Working' ? 'Working' : 'Open'}</span>
        </button>
        {onMove && pane.tmux_session !== here && <button type="button" className="cp-move" disabled={!!busy} aria-label={`Move ${paneTitle(pane)} here`} title="Move this tab here" onClick={() => void run(`move-${pane.id}`, () => onMove(pane))}>{busy === `move-${pane.id}` ? <LoaderCircle size={14} className="task-spin" /> : <ArrowRightToLine size={14} />}<span>Move here</span></button>}
      </li>)}</ul></section>}
      {groups.map(group => <section key={group.label}><h3>{group.label}</h3><ul>{group.chats.map(chat => { const key = chat.agent + chat.id; return <li key={key}>
        <button type="button" className="cp-main" disabled={!!busy} title={chat.title} onClick={() => void run(key, () => onChat(chat))}>
          <span className="cp-icon" title={agentNames[chat.agent]}><AgentIcon agent={chat.agent} size={15} /></span>
          <span className="cp-text"><span className="cp-title">{chat.title || 'Untitled chat'}</span><span className="cp-meta"><span className="cp-agent">{agentNames[chat.agent]}</span>{chat.dir && <bdi dir="ltr">{chat.dir}</bdi>}</span></span>
          <span className="cp-end">{busy === key ? <LoaderCircle size={14} className="task-spin" aria-label="Opening" /> : ago(chat.time)}</span>
        </button>
      </li> })}</ul></section>)}
      {history.next !== null && source !== 'tabs' && <button type="button" className="cp-more" disabled={history.loadingMore} onClick={() => void history.loadMore().catch(error => setFailure(errorText(error)))}>{history.loadingMore ? 'Loading…' : 'Show older chats'}</button>}
      {(history.loading || tabs.loading) && !shownTabs.length && !history.chats.length && <p className="cp-empty" role="status">Loading…</p>}
      {empty && <p className="cp-empty">{filtered ? 'Nothing matches.' : 'No chats or tabs yet.'}{filtered && <> <button type="button" className="link-button" onClick={() => { setText(''); setDir(''); setSource('all') }}>Clear filters</button></>}</p>}
    </div>
  </div>
}
