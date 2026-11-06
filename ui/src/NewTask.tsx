import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowUp, LoaderCircle, Paperclip, X } from 'lucide-react'
import { errorText } from './api'
import type { Api, Pane } from './api'
import { useModal, usePopover } from './hooks'
import type { Anchor } from './hooks'
import { uploadFile } from './Composer'
import { ChatPicker, resumeChat } from './ChatPicker'
import { AgentIcon, agentNames } from './Sidebar'
import './NewTask.css'

type Worker = 'claude' | 'codex'
type Review = 'off' | 'source' | 'new'
interface TaskFile { path: string; name: string; url?: string }
const WORKERS: Worker[] = ['claude', 'codex']
const REVIEWS: [Review, string][] = [['off', 'Off'], ['source', 'This tab reviews'], ['new', 'New tab reviews']]
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const lastWorker = () => { try { return localStorage.getItem('tether.newTask.agent') === 'codex' ? 'codex' : 'claude' } catch { return 'claude' } }

// The + on the tab strip: a task for a new tab. The server opens the tab beside this one, starts the agent with the
// task, and the user stays here (server/delegate.py). Either tab can review the other, and this tab can lend its chat as context.
export function NewTask({ api, pane, at, onClose, onEmpty, onCreated, onOpen, onMoved }: { api: Api; pane: Pane; at: Anchor; onClose: () => void; onEmpty: () => void; onCreated: (pane: string) => void; onOpen: (pane: Pane) => void; onMoved: (pane: Pane) => void }) {
  const modal = useModal(true, () => { if (!pending) onClose() })
  const [opening, setOpening] = useState(false)
  // Open existing is 100 px wider, and the box keeps its right edge under the +.
  const place = useMemo(() => opening ? { ...at, x: at.x - 100 } : at, [at, opening])
  const popover = usePopover(modal.ref, place, onClose)
  const [text, setText] = useState('')
  const [agent, setAgent] = useState<Worker>(lastWorker)
  const [review, setReview] = useState<Review>('off')
  const [context, setContext] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [files, setFiles] = useState<TaskFile[]>([])
  const [uploading, setUploading] = useState(0)
  const urls = useRef(new Set<string>())
  const field = useRef<HTMLTextAreaElement>(null)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])
  useEffect(() => { const made = urls.current; return () => made.forEach(url => URL.revokeObjectURL(url)) }, [])
  // A phone keyboard would cover the popover: focus the field only with a mouse or trackpad.
  useEffect(() => { if (typeof matchMedia === 'function' && matchMedia('(pointer: fine)').matches) field.current?.focus() }, [])
  const agentPane = pane.agent === 'claude' || pane.agent === 'codex'
  const canContext = agentPane && !!pane.thread && UUID.test(pane.thread)
  const canReview = agentPane && pane.writable
  const canReviewNew = canReview && canContext
  // Pasted files use the composer's upload. Their saved paths go in the task, as they do in a chat message.
  async function attach(list: File[]) {
    setError('')
    setUploading(count => count + list.length)
    for (const file of list) {
      try {
        const saved = await uploadFile(file, pane.id, pane.thread)
        const url = file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined
        if (url) urls.current.add(url)
        setFiles(current => [...current, { ...saved, url }])
      } catch (failure) { setError(errorText(failure)) }
      setUploading(count => count - 1)
    }
  }
  function remove(file: TaskFile) {
    if (file.url) { URL.revokeObjectURL(file.url); urls.current.delete(file.url) }
    setFiles(current => current.filter(item => item !== file))
  }
  async function create(event?: React.FormEvent) {
    event?.preventDefault()
    if (pending || uploading) return
    // The text goes first: the server names the tab from the first line.
    const message = [text.trim(), files.map(file => file.path).join(' ')].filter(Boolean).join('\n\n')
    if (!message && review !== 'new') { setError('Type a task first.'); field.current?.focus(); return }
    setPending(true); setError('')
    const abort = new AbortController()
    controller.current = abort
    try {
      const result = await api<{ pane: string; name: string }>('delegate', { session: pane.id, thread: pane.thread, agent, message, context: context && canContext, review: review === 'off' ? false : review, ...(!text.trim() && files.length ? { name: files[0].name } : {}) }, abort.signal)
      try { localStorage.setItem('tether.newTask.agent', agent) } catch { /* private mode */ }
      onCreated(result.pane)
    } catch (error) {
      if (!abort.signal.aborted) { setError(errorText(error)); setPending(false) }
    }
  }
  const modes = <div className="pick-modes" role="tablist" aria-label="New tab">
    <button type="button" role="tab" aria-selected={!opening} disabled={pending} onClick={() => setOpening(false)}>New task</button>
    <button type="button" role="tab" aria-selected={opening} disabled={pending} onClick={() => setOpening(true)}>Open existing</button>
  </div>
  const emptyTab = <button type="button" className="task-empty" disabled={pending} onClick={onEmpty}>Empty tab</button>
  return <dialog {...modal} {...popover} className={['sheet popover task-pop', pending && 'working', opening && 'opening'].filter(Boolean).join(' ')} aria-label="New tab"
    onCancel={event => { event.stopPropagation(); if (!pending) modal.onCancel(event); else event.preventDefault() }}>
    {opening && <div className="task-open">
      <div className="task-head">{modes}{emptyTab}</div>
      <ChatPicker api={api} exclude={pane.id} here={pane.tmux_session}
        onChat={async chat => onOpen(await resumeChat(api, chat, { after: pane.id }))} onTab={onOpen}
        onMove={async other => { await api('merge', { kind: 'pane', source: other.id, target: pane.tmux_session }); onMoved(other) }} />
    </div>}
    {!opening && <form onSubmit={create}>
      <div className="task-head">{modes}{emptyTab}</div>
      <div className="task-field">
        <textarea ref={field} value={text} rows={3} maxLength={20000} disabled={pending} aria-label="Task" placeholder={review === 'new' ? 'What should it check? Optional.' : 'What should it work on?'}
          onChange={event => { setText(event.target.value); if (error) setError('') }}
          onPaste={event => { if (event.clipboardData.files.length) { event.preventDefault(); void attach(Array.from(event.clipboardData.files)) } }}
          onKeyDown={event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void create() } }} />
        {files.length + uploading > 0 && <ul className="attachments task-files" aria-label="Attached files">
          {files.map(file => <li key={file.path} className="chip">
            {file.url ? <img className="task-thumb" src={file.url} alt="" /> : <Paperclip size={13} />}<span>{file.name}</span>
            <button type="button" aria-label={`Remove ${file.name}`} disabled={pending} onClick={() => remove(file)}><X size={14} /></button>
          </li>)}
          {uploading > 0 && <li className="chip task-uploading"><LoaderCircle size={13} className="task-spin" /><span>Uploading…</span></li>}
        </ul>}
        <div className="task-agents" role="radiogroup" aria-label="Agent">
          {WORKERS.map(choice => <button type="button" key={choice} role="radio" aria-checked={agent === choice} disabled={pending} className={agent === choice ? 'on' : ''} onClick={() => setAgent(choice)}>
            <AgentIcon agent={choice} size={14} />{agentNames[choice]}
          </button>)}
        </div>
      </div>
      {agentPane && <div className="task-options">
        <div className="task-review">
          <strong id="task-review-label">Review</strong>
          <div className="task-agents" role="radiogroup" aria-labelledby="task-review-label">
            {REVIEWS.map(([choice, label]) => { const usable = choice === 'off' || (choice === 'new' ? canReviewNew : canReview); return <button type="button" key={choice} role="radio" aria-checked={review === choice} disabled={pending || !usable} className={review === choice ? 'on' : ''} title={usable ? undefined : 'Send a first message here first.'} onClick={() => setReview(choice)}>{label}</button> })}
          </div>
          {review === 'source' && <small>This tab checks each finished turn of the new tab.</small>}
          {review === 'new' && <small>The new tab checks each finished turn here and sends feedback back.</small>}
        </div>
        {review !== 'new' && <label className={canContext ? 'task-switch' : 'task-switch off'}>
          <span><strong>Use this chat as context</strong><small>{canContext ? 'A short brief of recent turns, not the whole chat.' : 'Send a first message here first.'}</small></span>
          <input type="checkbox" role="switch" checked={context && canContext} disabled={!canContext || pending} onChange={event => setContext(event.target.checked)} />
        </label>}
      </div>}
      {error && <p className="error task-error" role="alert">{error}</p>}
      <button className="primary task-create" disabled={pending || uploading > 0}>
        <span>{pending ? `Starting ${agentNames[agent]}…` : 'Create'}</span>{pending ? <LoaderCircle size={16} className="task-spin" /> : <ArrowUp size={16} />}
      </button>
    </form>}
  </dialog>
}
