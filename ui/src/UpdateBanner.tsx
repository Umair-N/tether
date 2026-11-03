import { useSyncExternalStore } from 'react'
import { X } from 'lucide-react'
import { request } from './api'

interface Update { latest: string | null; available: boolean; url: string; mode: 'notify' | 'auto' | 'off'; checking: boolean }
let update: Update | null = null
let started = false
let dismissed: string | null = null
const listeners = new Set<() => void>()
const notify = () => listeners.forEach(listener => listener())

function load(retries = 0) {
  void request<Update>('update').then(data => {
    update = data
    try { dismissed = localStorage.getItem('tether-dismissed-update') } catch { /* Storage can be disabled. */ }
    notify()
    if (data.checking && data.mode !== 'off' && retries < 3) {
      setTimeout(() => load(retries + 1), 15000)
    }
  }, () => { /* Retry only when the server reports an active check. */ })
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  if (!started) {
    started = true
    load()
  }
  return () => { listeners.delete(listener) }
}

export function UpdateBanner() {
  const available = useSyncExternalStore(subscribe,
    () => update?.available && update.mode !== 'off' && update.latest !== dismissed ? update : null,
    () => null)
  if (!available?.latest || !available.url.startsWith('https://')) return null
  const version = 'v' + available.latest.replace(/^v/, '')
  return <div className="release-update-banner">
    <a href={available.url} target="_blank" rel="noopener noreferrer">Update available: {version}</a>
    <button type="button" className="icon-button" aria-label={`Dismiss update ${version}`} onClick={() => {
      dismissed = available.latest
      try { localStorage.setItem('tether-dismissed-update', dismissed ?? '') } catch { /* Keep dismissal for this page. */ }
      notify()
    }}><X size={16} aria-hidden="true" /></button>
  </div>
}
