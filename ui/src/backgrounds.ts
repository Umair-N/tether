import type { Api } from './api'

export interface BackgroundItem {
  id: string
  url: string
  source: 'upload' | 'ai'
  prompt?: string
  created: number
  width?: number
  height?: number
}
export type BackgroundSize = 'landscape' | 'square'
export type BackgroundJob = { state: 'running' } | { state: 'done'; item: BackgroundItem } | { state: 'failed'; error: string }

export async function listBackgrounds(api: Api): Promise<BackgroundItem[]> {
  return (await api<{ items: BackgroundItem[] }>('backgrounds')).items
}

export function uploadBackground(api: Api, file: File): Promise<BackgroundItem> {
  return api<BackgroundItem>('backgrounds/upload', file, undefined, undefined, 60000)
}

export async function deleteBackground(api: Api, id: string): Promise<void> {
  await api('backgrounds/delete', { id })
}

function pause(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    signal.throwIfAborted()
    const abort = () => { clearTimeout(timer); reject(signal.reason) }
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, 2000)
    signal.addEventListener('abort', abort, { once: true })
  })
}

/** Abort stops polling. The server job still finishes and keeps its image in the library. */
export async function generateBackground(api: Api, prompt: string, tuned: boolean, size: BackgroundSize = 'landscape', signal?: AbortSignal): Promise<BackgroundItem> {
  const stop = signal ? AbortSignal.any([signal, AbortSignal.timeout(320000)]) : AbortSignal.timeout(320000)
  stop.throwIfAborted()
  const { job } = await api<{ job: string }>('backgrounds/generate', { prompt, tuned, size }, stop)
  for (;;) {
    await pause(stop)
    const result = await api<BackgroundJob>(`backgrounds/generate?job=${encodeURIComponent(job)}`, undefined, stop)
    if (result.state === 'done') return result.item
    if (result.state === 'failed') throw new Error(result.error)
  }
}

export interface BackgroundJobHandle { promise: Promise<BackgroundItem>; controller: AbortController }
let running: BackgroundJobHandle | null = null

/** The image job in this page. It keeps polling after Settings closes, so the finished image still applies. */
export function backgroundJob(): BackgroundJobHandle | null {
  return running
}

export function startBackground(api: Api, prompt: string, tuned: boolean, size: BackgroundSize = 'landscape'): BackgroundJobHandle {
  if (running) return running
  const controller = new AbortController()
  const handle: BackgroundJobHandle = { controller, promise: generateBackground(api, prompt, tuned, size, controller.signal) }
  running = handle
  const clear = () => { if (running === handle) running = null }
  handle.promise.then(clear, clear)
  return handle
}
