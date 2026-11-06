import { request } from './api'

export type FileState = 'pending' | 'open' | 'closed' | 'unknown'
type Listener = () => void
type Entry = { pane: string; path: string; state: FileState; listeners: Set<Listener>; queued: boolean }
type Reply = { results: Record<string, boolean> }
type Options = {
  post?: (pane: string, paths: string[]) => Promise<Reply>
  later?: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>
  cancel?: (timer: ReturnType<typeof setTimeout>) => void
  visible?: () => boolean
  onVisibility?: (listener: Listener) => () => void
}

export function createFileChecks({
  post = (session, paths) => request<Reply>('file-check', { session, paths }),
  later = setTimeout,
  cancel = clearTimeout,
  visible = () => document.visibilityState === 'visible',
  onVisibility = listener => {
    document.addEventListener('visibilitychange', listener)
    return () => document.removeEventListener('visibilitychange', listener)
  },
}: Options = {}) {
  const entries = new Map<string, Entry>()
  const waiting = new Set<Entry>()
  const batches: Entry[][] = []
  let debounce: ReturnType<typeof setTimeout> | undefined
  let recheck: ReturnType<typeof setTimeout> | undefined
  let offVisibility: (() => void) | undefined
  let subscribers = 0
  let inFlight = 0
  const keyOf = (pane: string, path: string) => pane + '\0' + path

  function forget() {
    for (const [key, entry] of entries) {
      if (entries.size <= 2000) break
      if (!entry.listeners.size) entries.delete(key)
    }
  }

  function drain() {
    while (inFlight < 2 && batches.length) {
      const batch = batches.shift()!.filter(entry => {
        if (entry.listeners.size) return true
        entry.queued = false
        return false
      })
      if (!batch.length) continue
      inFlight++
      void (async () => {
        let results: Reply['results'] | undefined
        try { results = (await post(batch[0].pane, batch.map(entry => entry.path))).results }
        catch { /* The opener reports failed checks. */ }
        for (const entry of batch) {
          entry.queued = false
          const state = typeof results?.[entry.path] === 'boolean' ? (results[entry.path] ? 'open' : 'closed') : 'unknown'
          if (entry.state === state) continue
          entry.state = state
          entry.listeners.forEach(listener => listener())
        }
        inFlight--
        drain()
      })()
    }
  }

  function enqueue(entry: Entry) {
    if (entry.queued) return
    entry.queued = true
    waiting.add(entry)
    if (debounce !== undefined) return
    debounce = later(() => {
      debounce = undefined
      const panes = new Map<string, Entry[]>()
      for (const item of waiting) {
        if (!item.listeners.size) { item.queued = false; continue }
        const group = panes.get(item.pane) ?? []
        group.push(item)
        panes.set(item.pane, group)
      }
      waiting.clear()
      for (const group of panes.values()) {
        for (let i = 0; i < group.length; i += 200) batches.push(group.slice(i, i + 200))
      }
      drain()
    }, 150)
  }

  function scheduleRecheck() {
    if (recheck !== undefined) cancel(recheck)
    recheck = undefined
    if (!subscribers || !visible()) return
    recheck = later(() => {
      recheck = undefined
      refresh()
    }, 30000)
  }

  function refresh() {
    if (visible()) {
      for (const entry of entries.values()) if (entry.listeners.size) enqueue(entry)
    }
    scheduleRecheck()
  }

  return {
    stateOf(pane: string, path: string): FileState {
      return entries.get(keyOf(pane, path))?.state ?? 'pending'
    },
    subscribe(pane: string, path: string, listener: Listener) {
      const key = keyOf(pane, path)
      let entry = entries.get(key)
      if (!entry) {
        entry = { pane, path, state: 'pending', listeners: new Set(), queued: false }
        entries.set(key, entry)
      }
      // Each subscription owns its listener, even if callbacks are shared.
      const notify = () => listener()
      entry.listeners.add(notify)
      subscribers++
      if (entry.state === 'pending') enqueue(entry)
      if (subscribers === 1) {
        offVisibility = onVisibility(refresh)
        scheduleRecheck()
      }
      forget()
      return () => {
        if (!entry.listeners.delete(notify)) return
        subscribers--
        if (!subscribers) {
          if (debounce !== undefined) cancel(debounce)
          if (recheck !== undefined) cancel(recheck)
          debounce = recheck = undefined
          waiting.forEach(item => { item.queued = false })
          waiting.clear()
          for (const batch of batches) for (const item of batch) item.queued = false
          batches.length = 0
          offVisibility?.()
          offVisibility = undefined
        }
        forget()
      }
    },
  }
}

export const fileChecks = createFileChecks()
