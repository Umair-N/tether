import { VoiceField } from './VoiceField'
import { useEffect, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'
import { ClipboardPlus, X } from 'lucide-react'
import { errorText } from './api'
import type { Api, Pane } from './api'
import { useModal } from './hooks'
import './Backlog.css'

const CHAT = /^[A-Za-z0-9_-]{1,80}$/
/** Who added the item, in the form agents write (agent_backlog_cli.py source), so the item links back to this chat. */
function sourceOf(pane: Pane): string {
  if (pane.agent !== 'claude' && pane.agent !== 'codex') return ''
  return [pane.agent, /^%[0-9]+$/.test(pane.id) && `pane ${pane.id}`, pane.thread && CHAT.test(pane.thread) && `chat ${pane.thread}`].filter(Boolean).join(' · ')
}
const oneLine = (text: string) => text.split(/\s+/).filter(Boolean).join(' ')
const tabName = (pane: Pane) => pane.pane_name || pane.agent_name || pane.label || pane.id

/** Text selected in a chat becomes a backlog item. The person can reword it and add details, one per line. */
export function BacklogAddSheet({ api, text, pane, onClose, onAdded }: { api: Api; text: string; pane?: Pane; onClose: () => void; onAdded: (message: string) => void }) {
  const [item, setItem] = useState(() => oneLine(text))
  const [details, setDetails] = useState('')
  const [titleVoice, setTitleVoice] = useState(false)
  const [detailsVoice, setDetailsVoice] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const modal = useModal(true, onClose)
  const detailsRef = useRef<HTMLTextAreaElement>(null)
  // The dialog focuses its first button when it opens. The item is already filled, so the details are next.
  useEffect(() => { detailsRef.current?.focus() }, [])
  const source = pane ? sourceOf(pane) : ''
  const add = (event?: FormEvent) => {
    event?.preventDefault()
    if (busy || titleVoice || detailsVoice || !oneLine(item)) return
    setBusy(true); setError('')
    const lines = details.split('\n').map(oneLine).filter(Boolean)
    api<{ ref: string }>('projects/backlog-add', { text: item, details: lines, ...source ? { source } : {} }).then(added => {
      window.getSelection()?.removeAllRanges()
      onAdded(added.ref ? `Added ${added.ref} to the backlog.` : 'Added to the backlog.')
      onClose()
    }, failure => { setBusy(false); setError(errorText(failure)) })
  }
  const keys = (event: KeyboardEvent) => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) add() }
  return <dialog {...modal} className="sheet settings-sheet backlog-add-sheet" aria-labelledby="backlog-add-title">
    <form onSubmit={add} onKeyDown={keys}>
      <div className="sheet-heading">
        <h2 id="backlog-add-title">Add to backlog</h2>
        <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button>
      </div>
      {pane && <p className="sheet-sub">From {tabName(pane)}</p>}
      <label htmlFor="backlog-add-item">Item</label>
      <VoiceField label="backlog title" onBusyChange={setTitleVoice} value={item} setValue={setItem} multiline field={{ id: 'backlog-add-item', className: 'backlog-add-item', rows: 3, maxLength: 2000, disabled: busy,
        onKeyDown: event => { if (event.key === 'Enter' && !event.shiftKey && !event.metaKey && !event.ctrlKey) { event.preventDefault(); add() } } }} />
      <label htmlFor="backlog-add-details">Details <span className="backlog-add-hint">optional, one per line</span></label>
      <VoiceField label="backlog details" onBusyChange={setDetailsVoice} value={details} setValue={setDetails} multiline fieldRef={detailsRef} field={{ id: 'backlog-add-details', className: 'backlog-add-details', rows: 4, maxLength: 4000, disabled: busy,
        placeholder: 'Why it matters, where to look, what done means' }} />
      {error && <p className="error" role="alert">{error}</p>}
      <div className="sheet-buttons">
        <button type="button" className="secondary" onClick={onClose}>Cancel</button>
        <button type="submit" className="primary" disabled={busy || titleVoice || detailsVoice || !oneLine(item)}><ClipboardPlus size={16} aria-hidden="true" />{busy ? 'Adding…' : 'Add'}</button>
      </div>
    </form>
  </dialog>
}
