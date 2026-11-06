import { useRef, useState } from 'react'
import { Eye, X } from 'lucide-react'
import { errorText } from './api'
import type { Api, Pane } from './api'
import { useModal, usePopover } from './hooks'
import type { Anchor } from './hooks'
import { AgentIcon, needsYou, paneTitle } from './Sidebar'
import './ReviewLinks.css'

const POP_WIDTH = 300
const plural = (count: number) => `${count} ${count === 1 ? 'tab' : 'tabs'}`

interface Link { worker: string; reviewer: string; other: Pane | undefined; otherId: string }

// The eye in the chat header: which tabs this tab reviews, and which tabs review it. It shows only when a link exists.
// Each row opens that tab. Its X stops that link.
export function ReviewLinks({ api, pane, panes, onOpen, onChanged, onToast }: { api: Api; pane: Pane; panes: Pane[]; onOpen: (pane: Pane) => void; onChanged: () => void; onToast: (text: string) => void }) {
  const [at, setAt] = useState<Anchor | null>(null)
  const button = useRef<HTMLButtonElement>(null)
  const byId = new Map(panes.map(item => [item.id, item]))
  const reviewing: Link[] = panes.filter(item => item.reviewers?.includes(pane.id)).map(item => ({ worker: item.id, reviewer: pane.id, other: item, otherId: item.id }))
  const reviewers: Link[] = (pane.reviewers ?? []).map(id => ({ worker: pane.id, reviewer: id, other: byId.get(id), otherId: id }))
  const total = reviewing.length + reviewers.length
  if (!total) return null
  const label = [reviewing.length && `Reviewing ${plural(reviewing.length)}`, reviewers.length && `Reviewed by ${plural(reviewers.length)}`].filter(Boolean).join(' · ')
  return <>
    <button ref={button} type="button" className="icon-button review-toggle" aria-label={label} title={label} aria-haspopup="dialog" aria-expanded={!!at}
      onClick={() => { const box = button.current!.getBoundingClientRect(); setAt({ x: box.right - POP_WIDTH, y: box.bottom + 4 }) }}>
      <Eye size={20} />{total > 1 && <span className="review-count" aria-hidden="true">{total}</span>}
    </button>
    {at && <ReviewPop api={api} at={at} reviewing={reviewing} reviewers={reviewers} onClose={() => { setAt(null); requestAnimationFrame(() => button.current?.focus()) }}
      onOpen={target => { setAt(null); onOpen(target) }} onChanged={onChanged} onToast={onToast} />}
  </>
}

function ReviewPop({ api, at, reviewing, reviewers, onClose, onOpen, onChanged, onToast }: { api: Api; at: Anchor; reviewing: Link[]; reviewers: Link[]; onClose: () => void; onOpen: (pane: Pane) => void; onChanged: () => void; onToast: (text: string) => void }) {
  const modal = useModal(true, onClose)
  const popover = usePopover(modal.ref, at, onClose)
  const [stopping, setStopping] = useState('')
  async function stop(link: Link) {
    setStopping(link.otherId)
    try { await api('review-stop', { worker: link.worker, reviewer: link.reviewer }); onToast('Review stopped.'); onChanged() } catch (failure) { onToast(`Review not stopped: ${errorText(failure)}`) }
    setStopping('')
  }
  const row = (link: Link) => {
    const name = link.other ? paneTitle(link.other) : link.otherId
    const state = link.other?.state
    return <li key={link.otherId} className="review-row">
      <button type="button" className="review-go" disabled={!link.other} onClick={() => link.other && onOpen(link.other)}>
        {link.other && <AgentIcon agent={link.other.agent} icon={link.other.pane_icon} size={14} />}
        <span className="review-name" title={name}>{name}</span>
        {state === 'Working' || needsYou(state ?? '') ? <span className={`state-dot ${state === 'Working' ? 'working' : 'approval'}`} aria-label={state === 'Working' ? 'Working' : 'Needs you'} /> : null}
      </button>
      <button type="button" className="review-stop" disabled={stopping === link.otherId} aria-label={`Stop review link with ${name}`} title="Stop this review" onClick={() => void stop(link)}><X size={14} /></button>
    </li>
  }
  return <dialog {...modal} {...popover} className="sheet popover review-pop" aria-label="Review links">
    {reviewing.length > 0 && <section><h2>Reviewing</h2><ul>{reviewing.map(row)}</ul></section>}
    {reviewers.length > 0 && <section><h2>Reviewed by</h2><ul>{reviewers.map(row)}</ul></section>}
  </dialog>
}
