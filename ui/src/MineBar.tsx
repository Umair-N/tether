import { useEffect, useMemo, useState, type RefObject } from 'react'
import { ArrowUp, History } from 'lucide-react'
import type { ChatMessage } from './api'

// The bar covers the top of the chat: a message that starts under it counts as out of view.
export const MINE_BAR_SPACE = 52

const oneLine = (text: string) => text.replace(/\s+/g, ' ').trim()

/**
 * A bar at the top of the chat that names your own message nearest above what you see now.
 * A tap goes to that message; the bar then names the one before it, and so on.
 * At the oldest loaded message it offers to load earlier ones.
 */
export function MineBar({ view, messages, earlier, onReveal, onEarlier }: {
  view: RefObject<HTMLDivElement | null>
  messages: ChatMessage[]
  earlier: number
  onReveal: (id: string) => void
  onEarlier: () => void
}) {
  const mine = useMemo(() => new Map(messages.filter(item => item.role === 'user' && !item.kind).map(item => [item.id, item])), [messages])
  const [above, setAbove] = useState<string | null>(null)
  useEffect(() => {
    const element = view.current
    if (!element) return
    let frame = 0
    const measure = () => {
      frame = 0
      const line = element.getBoundingClientRect().top + MINE_BAR_SPACE - 8
      let found: string | null = null
      for (const node of element.querySelectorAll<HTMLElement>('[data-msg-id]')) {
        const id = node.dataset.msgId
        // Only bubbles you typed: compaction summaries, review cards and shell runs are user records too.
        if (!id || !mine.has(id) || !node.classList.contains('user-message')) continue
        if (node.getBoundingClientRect().top < line) found = id
        else break
      }
      setAbove(found)
    }
    const schedule = () => { if (!frame) frame = requestAnimationFrame(measure) }
    schedule()
    element.addEventListener('scroll', schedule, { passive: true })
    return () => { element.removeEventListener('scroll', schedule); if (frame) cancelAnimationFrame(frame) }
  }, [view, mine])
  const message = above ? mine.get(above) : undefined
  if (message) {
    const text = oneLine(message.text) || (message.images ? 'Image' : 'Message')
    return <div className="mine-bar"><button type="button" title={text} aria-label={`Go to your message: ${text}`} onClick={() => onReveal(message.id)}>
      <ArrowUp size={14} aria-hidden="true" /><span className="mine-bar-who">You</span><span className="mine-bar-text">{text}</span>
    </button></div>
  }
  if (!mine.size || !earlier) return null
  return <div className="mine-bar"><button type="button" onClick={onEarlier}>
    <History size={14} aria-hidden="true" /><span className="mine-bar-text">Load earlier messages · {earlier}</span>
  </button></div>
}

/** "8 Nov, 1:59 PM"; the year shows only when it is not this year. */
export function startedLabel(iso: string | null | undefined) {
  if (!iso) return null
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return null
  const year = date.getFullYear() !== new Date().getFullYear()
  return date.toLocaleString(undefined, { day: 'numeric', month: 'short', ...year ? { year: 'numeric' } : {}, hour: 'numeric', minute: '2-digit' })
}
