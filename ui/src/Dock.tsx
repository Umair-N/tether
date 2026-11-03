import { useCallback, useEffect, useRef, useState } from 'react'
import { Search, Clock3, Check, Terminal, X, RefreshCw, Gauge, SquareTerminal, Layers } from 'lucide-react'
import { TextMessagePartProvider } from '@assistant-ui/react'
import { errorText } from './api'
import type { Api, Approval, Attention, ChatData, Choice, Footer, FooterTask, LiveState, Pane } from './api'
import { useModal, usePolling } from './hooks'
import { agentNames } from './Sidebar'
import { Linked, PanelText, PanelPre, CopyButton, Markdown } from './Messages'
import { Elapsed } from './Notices'








export function ChoiceDock({ approval, attention, plan, agent, disabled, onChoose, onKey, onType }: { plan?: ChatData['plan']; approval: Approval | null; attention: Attention | null; agent: string; disabled: boolean
  onChoose: (kind: 'approval' | 'menu', option: Choice, approval: Approval | null, feedback?: string) => Promise<void>; onKey: (key: string) => void; onType?: (text: string) => Promise<boolean> }) {
  const [feedback, setFeedback] = useState('')
  const [code, setCode] = useState('')
  const [sending, setSending] = useState(false)
  const [picked, setPicked] = useState<number | null>(null)
  const kind = approval ? 'approval' : 'menu'
  const options = (approval ? (approval.actionable ? approval.options : undefined) : attention?.options?.filter(option => option.id <= 9)) ?? []
  const keepPlanning = plan && approval?.actionable ? options.find(option => /^No,?\s+keep planning\b/i.test(option.text)) : undefined
  const panel = !approval && attention?.reason === 'panel'
  // A question taller than the pane: the screen shows only its last rows, so read it from the transcript. The keys answer it.
  const ask = !approval && !options.length && !panel ? attention?.ask?.questions : undefined
  // A sign-in screen: its URL wraps over several lines, so the terminal can't open or copy it in one piece.
  const login = !approval && !options.length && !panel ? attention?.login : undefined
  const keys = ask?.length === 1 ? Array.from({ length: Math.min(9, ask[0].options.length + 2) }, (_, at) => String(at + 1)) : ['1', '2', '3']
  const title = plan ? 'Review plan' : login ? `${agent} sign-in` : approval ? `${agent} needs your permission` : panel ? `${agent} panel` : options.length || ask ? `${agent} asks` : `${agent} is waiting for you`
  const text = approval ? approval.text : attention?.question || attention?.text || ''
  const root = useRef<HTMLElement>(null)
  // The message box of this pane: the desktop workspace can show several.
  const focusBox = () => root.current?.closest('.chat-panel')?.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus()
  // "Chat about this" closes the question so the user can talk it through: put them in the message box after it.
  const chat = (option: Choice) => !approval && option.text === 'Chat about this'
  const pick = async (option: Choice) => {
    if (option.free) { focusBox(); return }
    setPicked(option.id); try { await onChoose(kind, option, approval, option.id === keepPlanning?.id ? feedback.trim() || undefined : undefined) } finally { setPicked(null) }
    if (chat(option)) focusBox() }
  return <section ref={root} className={`choice-dock ${plan ? 'plan-review' : ''} open`} aria-label={title}>
    <div className="choice-head"><span className="choice-mark" />{title}</div>
    <>
      {plan ? <div className="plan-text assistant-message" tabIndex={0} aria-label="Plan"><TextMessagePartProvider text={plan.text}><Markdown /></TextMessagePartProvider></div> : panel ? <PanelPre text={text} /> : login ? <div className="login-card">
        <p>{login.code ? 'Open the sign-in page and sign in. Then paste the code it shows here.' : 'Open the sign-in page and sign in. This card closes when sign-in ends.'}</p>
        <div className="login-card-links"><a className="primary" href={login.url} target="_blank" rel="noopener noreferrer">Open sign-in page</a><CopyButton text={login.url} label="Copy link" className="copy-chip" /></div>
        {login.code && onType && <form className="login-card-code" onSubmit={event => { event.preventDefault(); const value = code.trim(); if (!value) return; setSending(true); void onType(value).then(ok => { if (ok) setCode('') }).finally(() => setSending(false)) }}>
          <label htmlFor={`login-code-${attention?.id}`}>Sign-in code</label>
          <div><input id={`login-code-${attention?.id}`} type="password" autoComplete="off" autoCapitalize="none" spellCheck={false} maxLength={500} value={code} disabled={disabled || sending} onChange={event => setCode(event.target.value)} />
          <button type="submit" className="primary" disabled={disabled || sending || !code.trim()}>{sending ? 'Sending…' : 'Send code'}</button></div></form>}
      </div> : ask ? <div className="ask-card">{ask.map((item, at) => <div key={at}>
        {(item.header || ask.length > 1) && <p className="ask-header">{ask.length > 1 ? `Question ${at + 1} of ${ask.length}` : ''}{ask.length > 1 && item.header ? ' · ' : ''}{item.header}</p>}
        <p className="ask-question"><Linked text={item.question} /></p>
        <ol className="ask-options">{item.options.map(option => <li key={option.label}><strong>{option.label}</strong>{option.description && <small>{option.description}</small>}</li>)}
          <li><strong>Type something</strong><small>Write your own answer in the terminal.</small></li><li><strong>Chat about this</strong></li></ol></div>)}
        {ask.length === 1 ? <p className="ask-hint">Press a number to choose. The pane is too short to show the whole question.</p> : <p className="ask-hint">Answer each question in turn. The pane is too short to show the whole question.</p>}</div> : text && <div className={`choice-text ${approval || !options.length ? 'mono' : ''}`}>{approval || !options.length ? <PanelText text={text} /> : <Linked text={text} />}</div>}
      {keepPlanning && <label className="plan-feedback">Keep planning — optional feedback<textarea value={feedback} onChange={event => setFeedback(event.target.value)} maxLength={10000} rows={2} disabled={disabled || picked !== null} placeholder="Tell Claude what to change" /></label>}
      {options.length > 0 && <div className="choice-options" role="list">{options.map(option => <button key={option.id} type="button" role="listitem" className={[option.selected ? 'current' : '', approval ? /^yes\b/i.test(option.text) ? 'accept' : /^no\b/i.test(option.text) ? 'decline' : '' : ''].filter(Boolean).join(' ')} disabled={disabled || picked !== null} onClick={() => void pick(option)}>
        <span className="choice-number">{picked === option.id ? '…' : option.id}</span>{option.free
          ? <span><strong>Type your own answer</strong><small>{option.text === 'Type something.' ? 'Write it in the message box below, then send.' : `In the terminal now: “${option.text}”. Your answer from the message box replaces it.`}</small></span>
          : <span><strong>{option.text}</strong>{chat(option) ? <small>Close the question and talk it through in the message box.</small> : option.detail && <small>{option.detail}</small>}</span>}</button>)}</div>}
      <div className="choice-keys">
        {!options.length && !panel && !login && [...keys, 'Up', 'Down', 'Enter'].map(key => <button key={key} type="button" disabled={disabled} onClick={() => onKey(key)} aria-label={ask?.length === 1 && /^\d$/.test(key) ? `Choose ${key}: ${ask[0].options[Number(key) - 1]?.label ?? (Number(key) === ask[0].options.length + 1 ? 'Type something' : 'Chat about this')}` : undefined}>{key === 'Up' ? '↑' : key === 'Down' ? '↓' : key}</button>)}
        <button type="button" disabled={disabled} onClick={() => onKey('Escape')}>{options.length ? 'Cancel (Esc)' : 'Close (Esc)'}</button>
        {!options.length && !login && text && <CopyButton text={text} label="Copy panel text" className="copy-chip" />}
      </div>
    </>
  </section>
}

// Claude Code's ✦ card in each state: an offer, "Making your page…", a ready page link, a written explanation, or a failure.
// "Chat in main session" puts the note in this composer (Claude would put it in the terminal's input box), then closes the card.
const plainMarkdown = (line: string) => line.replace(/\*\*(.+?)\*\*/g, '$1').replace(/`([^`]+)`/g, '$1')
export function HeadsUp({ card, disabled, onKey, onChat }: { card: NonNullable<Footer['heads_up']>; disabled: boolean; onKey: (key: string) => Promise<void>; onChat: (note: string) => void }) {
  const [sent, setSent] = useState('')
  // The card always shows in full. The status panel scrolls, and its grip sets the height.
  useEffect(() => { setSent('') }, [card.text, card.state])
  useEffect(() => { if (!sent) return; const timer = setTimeout(() => setSent(''), 4000); return () => clearTimeout(timer) }, [sent])  // the card did not change: let the person try again
  const linked = (text: string) => card.url && text.includes(card.url)
    ? <>{text.slice(0, text.indexOf(card.url))}<a href={card.url} target="_blank" rel="noreferrer">{card.url}</a>{text.slice(text.indexOf(card.url) + card.url.length)}</>
    : text
  const text = card.text.replace(/^Heads up · /, '')
  const lines = card.full ? card.full.split('\n').map(line => line.trim()) : card.detail ?? []
  const press = (key: string, label: string) => {
    setSent(label)
    if (card.note && label === 'Chat in main session') { onChat(card.note); key = '1' }  // "Understood" closes the card without filling the terminal's input
    void onKey(key).catch(() => setSent(''))
  }
  return <div className={`heads-up ${card.state ?? 'offer'}`} role="status">
    <p className="heads-up-text">{card.state === 'making' && <RefreshCw size={14} className="spin" />}{linked(text)}</p>
    {!!lines.length && <div className="heads-up-detail">{lines.map((line, index) => line ? <p key={index} className={/^[-*] /.test(line) ? 'bullet' : /^\*\*.+\*\*$/.test(line) ? 'title' : ''}>{linked(plainMarkdown(line.replace(/^[-*] /, '')))}</p> : null)}{!card.full && !!card.more && <p className="heads-up-more">{card.more} more lines in the terminal</p>}</div>}
    {card.state === 'ready' && card.url && !text.includes(card.url) && <p><a href={card.url} target="_blank" rel="noreferrer">Open the page</a></p>}
    {card.state === 'making' && <p className="heads-up-note">Claude keeps working. The page link shows here when it is ready.</p>}
    {!!card.options.length && <div className="heads-up-keys">{card.options.map(option => <button key={option.key} type="button" className={option.key === '0' ? 'secondary' : ''} disabled={disabled || !!sent} aria-busy={sent === option.label} onClick={() => press(option.key, option.label)}>{sent === option.label ? 'Sent…' : option.label}</button>)}</div>}
  </div>
}

// Claude's mode line says "2 shells" or "1 monitor", which already reads well.
export const taskLabel = (task: string) => task

export function SessionInfo({ pane, footer, mode, onClose }: { pane: Pane; footer: Footer; mode: string; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const parts = (footer.status ?? '').split('|').map(part => part.trim()).filter(Boolean)
  const rows: [string, string][] = [['Pane', `${pane.label}`], ['Folder', pane.dir], ['Agent', agentNames[pane.agent]], ['State', pane.state],
    ...(pane.agent === 'claude' ? [['Mode', mode], ['Effort', footer.effort ?? 'Not shown'], ['Status line', parts.join(' · ') || 'Not shown'], ['Goal', footer.goal ? `Active${footer.goal !== 'active' ? ` · ${footer.goal}` : ''}` : 'None'], ['Background tasks', footer.tasks?.map(taskLabel).join(', ') || 'None'], ['IDE context', footer.ide || 'None']] as [string, string][] : []),
    ...((footer.agents ?? []).filter(agent => !agent.main).map(agent => ['Background agent', [agent.name, agent.task, agent.detail].filter(Boolean).join(' · ')]) as [string, string][])]
  return <dialog {...modal} className="sheet" aria-labelledby="info-title">
    <div className="sheet-heading"><h2 id="info-title">Session details</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <dl className="info-list">{rows.map(([name, value], index) => <div key={index}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>
  </dialog>
}
const since = (time?: number | null) => {
  if (!time) return ''
  const minutes = Math.max(0, Math.round((Date.now() / 1000 - time) / 60))
  return minutes < 1 ? 'just now' : minutes < 60 ? `${minutes} min ago` : minutes < 1440 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min ago` : `${Math.floor(minutes / 1440)} d ago`
}

// The goal's last check, or the conversation's background tasks and their latest output.
export function LiveSheet({ api, pane, show, taskDetails, onClose, onTerminal }: { api: Api; pane: Pane; show: 'goal' | 'tasks'; taskDetails?: FooterTask[]; onClose: () => void; onTerminal: () => void }) {
  const modal = useModal(true, onClose)
  const [stopping, setStopping] = useState<string | null>(null)
  const [stopError, setStopError] = useState('')
  const fetcher = useCallback((signal: AbortSignal) => api<LiveState>(`live?session=${encodeURIComponent(pane.id)}`, undefined, signal), [api, pane.id])
  const { data: state, error, refresh } = usePolling(fetcher, 5000)
  const stop = async (task: string) => {
    if (!window.confirm('Stop this shell?')) return
    setStopping(task); setStopError('')
    try { await api('stop-task', { session: pane.id, task }); await refresh() }
    catch (error) { setStopError(error instanceof Error ? error.message : 'Could not stop this shell.') }
    finally { setStopping(null) }
  }
  const goal = state?.goal
  const hookTasks = show === 'tasks' && taskDetails !== undefined
  return <dialog {...modal} className="sheet live-sheet" aria-labelledby="live-title">
    <div className="sheet-heading"><h2 id="live-title">{show === 'goal' ? 'Goal' : 'Background tasks'}</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    {!hookTasks && error && <p className="live-empty" role="alert">Could not load: {error}</p>}
    {stopError && <p className="live-empty" role="alert">{stopError}</p>}
    {!hookTasks && !state && !error && <p className="live-empty">Loading…</p>}
    {state && show === 'goal' && (goal ? <div className="live-goal">
      <p className="live-label">Condition</p>
      <p className="live-condition">{goal.condition}</p>
      <p className={goal.met ? 'live-status met' : 'live-status'}>{goal.met ? <Check size={15} /> : <Clock3 size={15} />}{goal.met ? 'Met' : 'Not met yet'}{goal.checked_at ? ` · checked ${since(goal.checked_at)}` : ''}</p>
      {goal.reason && <><p className="live-label">Last check</p><p className="live-reason">{goal.reason}</p></>}
      {goal.set_at && <p className="live-meta">Set {since(goal.set_at)}</p>}
    </div> : <p className="live-empty">No goal found in this conversation.</p>)}
    {hookTasks && (taskDetails.length ? <ul className="live-tasks hook-tasks">{taskDetails.map(task => <li key={task.id}>
      <p className="live-task-title">{task.type === 'monitor' ? <Gauge size={15} /> : task.type === 'shell' ? <Terminal size={15} /> : <Layers size={15} />}<strong>{task.description || task.type || 'Task'}</strong></p>
      <p className="live-meta">{task.type || 'Task'}{task.status ? ` · ${task.status}` : ''}{task.started != null && <> · <Elapsed since={task.started * 1000} /></>}</p>
      {task.command && <code className="live-command">{task.command}</code>}
    </li>)}</ul> : <p className="live-empty">No background tasks in this conversation.</p>)}
    {!hookTasks && state && show === 'tasks' && (state.tasks.length ? <ul className="live-tasks">{[...state.tasks.filter(task => task.state === 'running').reverse(), ...state.tasks.filter(task => task.state !== 'running').reverse().slice(0, 5)].map(task => <li key={task.id}>
      <p className="live-task-title">{task.kind === 'monitor' ? <Gauge size={15} /> : <Terminal size={15} />}<strong>{task.title || task.command}</strong></p>
      <p className="live-meta">{task.kind === 'monitor' ? 'Monitor' : 'Shell'} · {task.state === 'running' ? 'Running' : 'Ended'} · {task.id}{task.started ? ` · started ${new Date(task.started * 1000).toLocaleString()}` : ''}</p>
      {task.title && <code className="live-command">{task.command}</code>}
      {task.output ? <pre className="live-output">{task.output}</pre> : <p className="live-meta">No output yet.</p>}
      {task.kind === 'shell' && task.state === 'running' && <div className="live-actions"><button type="button" disabled={stopping !== null} onClick={() => void stop(task.id)}>{stopping === task.id ? 'Stopping…' : 'Stop'}</button></div>}
    </li>)}</ul> : <p className="live-empty">No background tasks in this conversation.</p>)}
    <div className="live-actions"><button type="button" className="secondary" onClick={() => { onClose(); onTerminal() }}><SquareTerminal size={16} />Open terminal</button></div>
  </dialog>
}
interface SearchMatch { id: string; role: string; kind?: string; time?: string; from_end: number; snippet: string }
// Finds words in the whole conversation (not only the loaded part). Tapping a result loads enough of the chat and scrolls to it.
export function SearchSheet({ api, pane, shell, onClose, onPick }: { api: Api; pane: Pane; shell: boolean; onClose: () => void; onPick: (match: SearchMatch, maxLimit: number) => void }) {
  const modal = useModal(true, onClose)
  const [query, setQuery] = useState(() => lastSearch.get(pane.id) ?? '')
  const [result, setResult] = useState<{ matches: SearchMatch[]; total: number; max_limit: number; query: string } | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    lastSearch.set(pane.id, query)
    if (!query.trim()) { setResult(null); setError(''); return }
    const abort = new AbortController()
    const timer = setTimeout(() => {
      api<{ matches: SearchMatch[]; total: number; max_limit: number }>(`search?session=${encodeURIComponent(pane.id)}&q=${encodeURIComponent(query.trim())}${shell ? '&mode=shell' : ''}`, undefined, abort.signal)
        .then(data => { setResult({ ...data, query: query.trim() }); setError('') }, failure => { if (!abort.signal.aborted) setError(errorText(failure)) })
    }, 250)
    return () => { clearTimeout(timer); abort.abort() }
  }, [api, pane.id, shell, query])
  const words = (result?.query ?? '').toLowerCase().split(/\s+/).filter(Boolean)
  const marked = (text: string) => {
    if (!words.length) return text
    const parts = text.split(new RegExp(`(${words.map(word => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'ig'))
    return parts.map((part, index) => index % 2 ? <mark key={index}>{part}</mark> : part)
  }
  return <dialog {...modal} className="sheet search-sheet" aria-labelledby="search-title">
    <div className="sheet-heading"><h2 id="search-title">Search this chat</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <label className="search-field"><Search size={17} /><input type="search" autoFocus enterKeyHint="search" placeholder="Words to find" aria-label="Words to find" value={query} onChange={event => setQuery(event.target.value)} /></label>
    {error && <p className="live-empty" role="alert">Could not search: {error}</p>}
    {result && <p className="search-count" role="status">{result.total === 0 ? 'No messages found.' : `${result.total} ${result.total === 1 ? 'message' : 'messages'}${result.total > result.matches.length ? `, newest ${result.matches.length} shown` : ''}`}</p>}
    {result && !!result.matches.length && <ul className="search-results">{result.matches.map(match => <li key={match.id}>
      <button type="button" disabled={match.from_end > result.max_limit} onClick={() => onPick(match, result.max_limit)}>
        <small>{match.kind === 'actions' ? 'Tools' : match.role === 'user' ? 'You' : 'Agent'}{match.time ? ` · ${since(Date.parse(match.time) / 1000)}` : ''}{match.from_end > result.max_limit ? ' · too far back to open' : ''}</small>
        <span>{marked(match.snippet)}</span>
      </button></li>)}</ul>}
  </dialog>
}
const lastSearch = new Map<string, string>()
