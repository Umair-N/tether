import { useEffect, useRef, useState } from 'react'
import { ArrowUp, LoaderCircle } from 'lucide-react'
import { errorText } from './api'
import type { Api, Pane } from './api'
import { useModal, usePopover } from './hooks'
import type { Anchor } from './hooks'
import { AgentIcon, agentNames } from './Sidebar'
import './NewTask.css'

type Worker = 'claude' | 'codex'
const WORKERS: Worker[] = ['claude', 'codex']
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const lastWorker = () => { try { return localStorage.getItem('tether.newTask.agent') === 'codex' ? 'codex' : 'claude' } catch { return 'claude' } }

// The + on the tab strip: a task for a new tab. The server opens the tab beside this one, starts the agent with the
// task, and the user stays here (server/delegate.py). This tab can review each finished turn, and lend its chat as context.
export function NewTask({ api, pane, at, onClose, onEmpty, onCreated }: { api: Api; pane: Pane; at: Anchor; onClose: () => void; onEmpty: () => void; onCreated: (pane: string) => void }) {
  const modal = useModal(true, () => { if (!pending) onClose() })
  const popover = usePopover(modal.ref, at, onClose)
  const [text, setText] = useState('')
  const [agent, setAgent] = useState<Worker>(lastWorker)
  const [review, setReview] = useState(false)
  const [context, setContext] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const field = useRef<HTMLTextAreaElement>(null)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])
  // A phone keyboard would cover the popover: focus the field only with a mouse or trackpad.
  useEffect(() => { if (typeof matchMedia === 'function' && matchMedia('(pointer: fine)').matches) field.current?.focus() }, [])
  const agentPane = pane.agent === 'claude' || pane.agent === 'codex'
  const canContext = agentPane && !!pane.thread && UUID.test(pane.thread)
  const canReview = agentPane && pane.writable
  const here = agentNames[pane.agent] ?? 'this tab'
  async function create(event?: React.FormEvent) {
    event?.preventDefault()
    if (pending) return
    const message = text.trim()
    if (!message) { setError('Type a task first.'); field.current?.focus(); return }
    setPending(true); setError('')
    const abort = new AbortController()
    controller.current = abort
    try {
      const result = await api<{ pane: string; name: string }>('delegate', { session: pane.id, thread: pane.thread, agent, message, context: context && canContext, review: review && canReview }, abort.signal)
      try { localStorage.setItem('tether.newTask.agent', agent) } catch { /* private mode */ }
      onCreated(result.pane)
    } catch (error) {
      if (!abort.signal.aborted) { setError(errorText(error)); setPending(false) }
    }
  }
  return <dialog {...modal} {...popover} className={pending ? 'sheet popover task-pop working' : 'sheet popover task-pop'} aria-labelledby="task-title"
    onCancel={event => { event.stopPropagation(); if (!pending) modal.onCancel(event); else event.preventDefault() }}>
    <span className="task-sheen" aria-hidden="true" />
    <form onSubmit={create}>
      <div className="task-head"><h2 id="task-title">New tab with a task</h2><button type="button" className="task-empty" disabled={pending} onClick={onEmpty}>Empty tab</button></div>
      <div className="task-field">
        <textarea ref={field} value={text} rows={3} maxLength={20000} disabled={pending} aria-label="Task" placeholder="What should it work on?"
          onChange={event => { setText(event.target.value); if (error) setError('') }}
          onKeyDown={event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void create() } }} />
        <div className="task-agents" role="radiogroup" aria-label="Agent">
          {WORKERS.map(choice => <button type="button" key={choice} role="radio" aria-checked={agent === choice} disabled={pending} className={agent === choice ? 'on' : ''} onClick={() => setAgent(choice)}>
            <AgentIcon agent={choice} size={14} />{agentNames[choice]}
          </button>)}
        </div>
      </div>
      {agentPane && <div className="task-options">
        <label className={canReview ? 'task-switch' : 'task-switch off'}>
          <span><strong>{here} reviews</strong><small>This tab checks each finished turn.</small></span>
          <input type="checkbox" role="switch" checked={review && canReview} disabled={!canReview || pending} onChange={event => setReview(event.target.checked)} />
        </label>
        <label className={canContext ? 'task-switch' : 'task-switch off'}>
          <span><strong>Use this chat as context</strong><small>{canContext ? 'A short brief of recent turns, not the whole chat.' : 'Send a first message here first.'}</small></span>
          <input type="checkbox" role="switch" checked={context && canContext} disabled={!canContext || pending} onChange={event => setContext(event.target.checked)} />
        </label>
      </div>}
      {error && <p className="error task-error" role="alert">{error}</p>}
      <button className="primary task-create" disabled={pending}>
        <span>{pending ? `Starting ${agentNames[agent]}…` : 'Create'}</span>{pending ? <LoaderCircle size={16} className="task-spin" /> : <ArrowUp size={16} />}
      </button>
    </form>
  </dialog>
}
