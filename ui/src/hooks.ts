import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { errorText } from './api'

// One request at a time. Abort obsolete requests and pause polling in hidden tabs.
// `initial` shows saved data at once (no loading state) while the first fetch runs.
export function usePolling<T>(fetcher: (signal: AbortSignal) => Promise<T>, interval: number, streamPath?: string, initial?: T) {
  const [data, setData] = useState<T | undefined>(initial)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(initial === undefined)
  const active = useRef<AbortController | null>(null)
  const refresh = useCallback(async () => {
    active.current?.abort()
    const controller = new AbortController()
    active.current = controller
    try {
      const result = await fetcher(controller.signal)
      if (!controller.signal.aborted) { setData(result); setError('') }
    } catch (error) {
      if (!controller.signal.aborted) setError(errorText(error))
    } finally {
      if (!controller.signal.aborted) setLoading(false)
    }
  }, [fetcher])
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>
    let stopped = false, received = 0
    let stream: EventSource | null = null
    const connect = () => {
      if (!streamPath || !('EventSource' in window) || document.hidden || stopped) return
      stream = new EventSource(`/mobile/api/${streamPath}`)
      stream.onmessage = event => {
        try {
          const result = JSON.parse(event.data) as T
          active.current?.abort()  // an older poll must not overwrite a pushed update
          received = Date.now(); setData(result); setError(''); setLoading(false)
        } catch { received = 0 }
      }
      stream.addEventListener('idle', () => { if (received) received = Date.now() })
      stream.addEventListener('unavailable', () => { received = 0 })
      stream.onerror = () => { received = 0 }
    }
    const tick = async () => {
      if (stopped || document.hidden) return
      if (!received || Date.now() - received > 25000) {
        if (received) { stream?.close(); stream = null; received = 0 }
        if (!stream) connect()
        await refresh()
      }
      if (!stopped) timer = setTimeout(tick, interval)
    }
    const visibility = () => {
      clearTimeout(timer); stream?.close(); stream = null; received = 0
      if (document.hidden) active.current?.abort()
      else void tick()
    }
    void tick()
    document.addEventListener('visibilitychange', visibility)
    return () => {
      stopped = true
      clearTimeout(timer); stream?.close()
      active.current?.abort()
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [refresh, interval, streamPath])
  return { data, error, loading, refresh }
}

export function useModal(open: boolean, close: () => void) {
  const ref = useRef<HTMLDialogElement>(null)
  const opened = useRef(0)
  useEffect(() => {
    const dialog = ref.current
    if (open) { dialog?.showModal(); opened.current = Date.now() }
    else dialog?.close()
  }, [open])
  const start = useRef<{ y: number; x: number } | null>(null)
  return {
    ref,
    onCancel: (event: React.SyntheticEvent) => { event.preventDefault(); close() },
    // A tap on the dimmed backdrop lands on the dialog itself, outside its box.
    onClick: (event: React.MouseEvent<HTMLDialogElement>) => {
      const box = event.currentTarget.getBoundingClientRect()
      if (Date.now() - opened.current > 400 && event.target === event.currentTarget && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) close()
    },
    // A swipe down closes the sheet, unless the finger started in content that is scrolled down.
    onTouchStart: (event: React.TouchEvent<HTMLDialogElement>) => {
      let node = event.target as HTMLElement | null, scrolled = false
      for (; node && node !== event.currentTarget.parentElement; node = node.parentElement) if (node.scrollTop > 0) scrolled = true
      start.current = scrolled ? null : { x: event.touches[0].clientX, y: event.touches[0].clientY }
    },
    onTouchEnd: (event: React.TouchEvent<HTMLDialogElement>) => {
      const from = start.current; start.current = null
      if (!from) return
      const dy = event.changedTouches[0].clientY - from.y, dx = event.changedTouches[0].clientX - from.x
      if (dy > 80 && Math.abs(dx) < dy * 0.6) close()
    },
  }
}

/** A screen point a menu opens from: the pointer for a right-click, or the button that opened it. */
export type Anchor = { x: number; y: number }
export const anchorOf = (event: React.MouseEvent): Anchor => {
  if (event.clientX || event.clientY) return { x: event.clientX, y: event.clientY }
  const box = event.currentTarget.getBoundingClientRect()  // keyboard: no pointer position
  return { x: box.left, y: box.bottom + 4 }
}

/**
 * Show a dialog as a popover next to `at`, inside the window, instead of as a sheet.
 * It moves again when its content changes size (for example a rename form). Arrow keys move between its buttons.
 */
export function usePopover(ref: RefObject<HTMLDialogElement | null>, at: Anchor | undefined, close: () => void) {
  useLayoutEffect(() => {
    const box = ref.current
    if (!box || !at) return
    const place = () => {
      const width = box.offsetWidth, height = box.offsetHeight
      box.style.left = `${Math.max(8, Math.min(at.x, innerWidth - width - 8))}px`
      box.style.top = `${at.y + height > innerHeight - 8 ? Math.max(8, at.y - height) : at.y}px`
    }
    place()
    const observer = new ResizeObserver(place)
    observer.observe(box)
    // Focus the menu itself, so no item shows a focus ring until the arrow keys move to it.
    const timer = setTimeout(() => { if (!box.querySelector('input')) box.focus() })
    return () => { observer.disconnect(); clearTimeout(timer) }
  }, [ref, at])
  if (!at) return {}
  return {
    tabIndex: -1,
    onKeyDown: (event: React.KeyboardEvent<HTMLDialogElement>) => {
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
      const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('.action-list button:not(:disabled)')]
      if (!items.length) return
      event.preventDefault()
      const at = items.indexOf(document.activeElement as HTMLButtonElement)
      items[(at + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus()
    },
    // A right-click outside the menu closes it, like a click does.
    onContextMenu: (event: React.MouseEvent<HTMLDialogElement>) => {
      if (event.target !== event.currentTarget) return
      event.preventDefault(); close()
    },
  }
}

/** A row shows its full name as a tooltip only when the name is cut off. */
export const clipTitle = (event: React.MouseEvent<HTMLElement>) => {
  const name = event.currentTarget.querySelector<HTMLElement>('.session-title, .pane-child-name, .pane-tab-name')
  event.currentTarget.title = name && name.scrollWidth > name.clientWidth ? name.textContent ?? '' : ''
}
