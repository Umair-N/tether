// Notice cards and the work timer, adapted from T3 Code (github.com/pingdotgg/t3code):
// ComposerBannerStack, ComposerBanner and AgentElapsed. MIT License, Copyright (c) 2026 T3 Tools Inc.
// Tether keeps plain CSS and a tap-to-expand row in place of T3's hover popover.
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ChevronDown, X } from 'lucide-react'
import './Notices.css'

export type NoticeVariant = 'default' | 'info' | 'warning' | 'error'
// The machine comes first, then what needs you. The order is fixed, so rows never trade places.
export type NoticeGroup = 'environment' | 'attention'
export interface NoticeItem {
  id: string
  variant?: NoticeVariant
  group?: NoticeGroup
  icon: ReactNode
  title: ReactNode
  detail?: ReactNode  // muted text after the title: what happens next
  more?: ReactNode  // shown under the row only while it is open, such as the full command
  action?: { label: string; onClick: () => void; disabled?: boolean; hint?: string }
  dismissLabel?: string
  onDismiss?: () => void
  live?: boolean  // a status that changes, read by screen readers as it changes
}

const GROUPS: NoticeGroup[] = ['environment', 'attention']

// One line per notice. Text that does not fit gets a "Show all" toggle; the toggle stays while it is open.
function NoticeCard({ item }: { item: NoticeItem }) {
  const text = useRef<HTMLSpanElement>(null)
  const [overflow, setOverflow] = useState(false)
  const [open, setOpen] = useState(false)
  useLayoutEffect(() => {
    const element = text.current
    if (!element) return
    const measure = () => setOverflow(element.scrollWidth > element.clientWidth + 1)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [item.title, item.detail])
  const variant = item.variant ?? 'default'
  return <div className={`notice-card ${variant} ${open ? 'open' : ''}`} role={variant === 'error' || variant === 'warning' ? 'alert' : item.live ? 'status' : undefined}>
    <span className="notice-icon" aria-hidden="true">{item.icon}</span>
    <span ref={text} className="notice-text">{item.detail ? <><span className="notice-title">{item.title}</span><span className="notice-detail">{item.detail}</span></> : item.title}</span>
    {item.action && <button type="button" className="notice-action" disabled={item.action.disabled} title={item.action.hint} onClick={item.action.onClick}>{item.action.label}</button>}
    {(overflow || open || item.more) && <button type="button" className="notice-button" aria-expanded={open} aria-label={open ? 'Show less' : 'Show all'} onClick={() => setOpen(!open)}><ChevronDown size={15} /></button>}
    {item.onDismiss && <button type="button" className="notice-button" aria-label={item.dismissLabel ?? 'Hide notice'} onClick={item.onDismiss}><X size={14} /></button>}
    {open && item.more && <div className="notice-more">{item.more}</div>}
  </div>
}

export function NoticeStack({ items }: { items: NoticeItem[] }) {
  const ordered = GROUPS.flatMap(group => items.filter(item => (item.group ?? 'attention') === group))
  if (!ordered.length) return null
  return <div className="notice-stack">{ordered.map(item => <NoticeCard key={item.id} item={item} />)}</div>
}

// T3's formatElapsedSeconds: 42s, 3m 05s, 1h 07m.
export function formatElapsed(totalSeconds: number) {
  const seconds = Math.max(0, Math.floor(totalSeconds))
  const minutes = Math.floor(seconds / 60)
  if (minutes === 0) return `${seconds}s`
  const hours = Math.floor(minutes / 60)
  if (hours === 0) return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`
  return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`
}

// Time since `since` (ms), updated each second without a React render.
export function Elapsed({ since }: { since: number }) {
  const ref = useRef<HTMLSpanElement>(null)
  useEffect(() => {
    const tick = () => { if (ref.current) ref.current.textContent = formatElapsed((Date.now() - since) / 1000) }
    tick()
    const timer = setInterval(tick, 1000)
    return () => clearInterval(timer)
  }, [since])
  return <span ref={ref} className="elapsed" />
}
