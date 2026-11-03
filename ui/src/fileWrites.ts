// The Files explorer's writes and Tether hand-offs (POST /mobile/api/files/*, contract in .work-notes/2026-11-02/explorer/API.md).
import { useSyncExternalStore } from 'react'
import type { Api } from './api'

/** "@~/a @~/b " for a chat draft. A path with a space goes in quotes, so the mention stays one token. */
export function mentionText(paths: string[]) {
  return paths.map(path => /\s/.test(path) ? `@"${path}"` : `@${path}`).join(' ') + ' '
}

export const UPLOAD_LIMIT = 2 * 1024 ** 3
export interface Done { from: string; path: string }
export interface Failed { path: string; error: string }
export interface Transfer { done: Done[]; failed: Failed[]; skipped?: { secret: number } }
export interface Trashed { done: { from: string; trash_name: string }[]; failed: Failed[] }
export interface SizeInfo { files: number; folders: number; bytes: number; partial: boolean; note: string | null }

const q = encodeURIComponent
const pathQuery = (list: string[]) => list.map(path => `path=${q(path)}`).join('&')
export const writes = {
  mkdir: (api: Api, path: string, name: string) => api<{ path: string; name: string }>('files/mkdir', { path, name }),
  rename: (api: Api, path: string, name: string) => api<{ from: string; path: string; name: string }>('files/rename', { path, name }),
  duplicate: (api: Api, path: string) => api<{ from: string; path: string; name: string; skipped?: { secret: number } }>('files/duplicate', { path }),
  copy: (api: Api, list: string[], to: string) => api<Transfer>('files/copy', { paths: list, to }),
  move: (api: Api, list: string[], to: string) => api<Transfer>('files/move', { paths: list, to }),
  trash: (api: Api, list: string[]) => api<Trashed>('files/delete', { paths: list }),
  size: (api: Api, list: string[], signal?: AbortSignal) => api<SizeInfo>(`files/size?${pathQuery(list)}`, undefined, signal),
  /** A browser URL: the streamed zip. Call size first; errors come back as JSON before any bytes. */
  zipUrl: (list: string[]) => `/mobile/api/files/zip?${pathQuery(list)}`,
}

/** One line for a toast: what worked, what the server left out, and the first failure. */
export function summary(verb: string, result: { done: unknown[]; failed: Failed[]; skipped?: { secret: number } }) {
  const done = result.done.length, failed = result.failed.length
  const worked = done ? `${verb} ${done} ${done === 1 ? 'item' : 'items'}.` : ''
  const secret = result.skipped?.secret ? ` ${result.skipped.secret} secret ${result.skipped.secret === 1 ? 'item was' : 'items were'} left out.` : ''
  const first = result.failed[0]
  const bad = first ? ` ${failed === 1 ? `Could not do ${first.path.split('/').pop()}: ${first.error}` : `${failed} items failed. First: ${first.error}`}` : ''
  return `${worked}${secret}${bad}`.trim()
}

// Uploads. They live outside the Files screen, so they keep going (and keep their progress) when the person switches to a chat.
export interface Upload { id: number; name: string; folder: string; size: number; sent: number; state: 'waiting' | 'sending' | 'done' | 'failed'; error?: string; path?: string }
let uploads: Upload[] = []
let nextId = 1
let running = false
const listeners = new Set<() => void>()
const queue = new Map<number, File>()
let current: XMLHttpRequest | null = null
const emit = () => listeners.forEach(listener => listener())
const change = (id: number, patch: Partial<Upload>) => { uploads = uploads.map(item => item.id === id ? { ...item, ...patch } : item); emit() }
/** Called after each finished file: the explorer reloads the folder it shows. */
let onFinished: ((upload: Upload) => void) | null = null
export const setUploadDone = (callback: ((upload: Upload) => void) | null) => { onFinished = callback }

function send(upload: Upload, file: File) {
  return new Promise<void>(resolve => {
    const xhr = new XMLHttpRequest()
    current = xhr
    xhr.open('POST', '/mobile/api/files/upload')
    xhr.withCredentials = true
    xhr.setRequestHeader('X-Codex-Mobile', '1')
    xhr.setRequestHeader('X-Folder', upload.folder)
    xhr.setRequestHeader('X-Filename', encodeURIComponent(upload.name))
    xhr.setRequestHeader('Content-Type', 'application/octet-stream')
    let last = 0
    xhr.upload.onprogress = event => { if (event.timeStamp - last > 150 || event.loaded === event.total) { last = event.timeStamp; change(upload.id, { sent: event.loaded }) } }
    const fail = (error: string) => { change(upload.id, { state: 'failed', error }); resolve() }
    xhr.onerror = () => fail('The connection dropped. Try again.')
    xhr.onabort = () => fail('Stopped.')
    xhr.onloadend = () => { if (current === xhr) current = null }
    xhr.onload = () => {
      let data: { error?: string; path?: string; name?: string } = {}
      try { data = JSON.parse(xhr.responseText) } catch { /* not JSON */ }
      if (xhr.status !== 200) { fail(data.error || `Upload failed (${xhr.status}).`); return }
      change(upload.id, { state: 'done', sent: upload.size, path: data.path, name: data.name || upload.name })
      onFinished?.(uploads.find(item => item.id === upload.id) ?? upload)
      resolve()
    }
    change(upload.id, { state: 'sending' })
    xhr.send(file)
  })
}

async function run() {
  if (running) return
  running = true
  try {
    for (let next = uploads.find(item => item.state === 'waiting'); next; next = uploads.find(item => item.state === 'waiting')) {
      const file = queue.get(next.id)
      queue.delete(next.id)
      if (file) await send(next, file)
      else change(next.id, { state: 'failed', error: 'The file is gone.' })
    }
  } finally { running = false }
}

/** Queue files for the folder. A file over 2 GB fails at once, with no request. One file uploads at a time. */
export function startUploads(folder: string, files: File[]) {
  const added: Upload[] = files.map(file => {
    const id = nextId++
    const tooLarge = file.size > UPLOAD_LIMIT
    if (!tooLarge) queue.set(id, file)
    return { id, name: file.name, folder, size: file.size, sent: 0, state: tooLarge ? 'failed' : 'waiting', error: tooLarge ? 'Too large. The limit is 2 GB.' : undefined }
  })
  uploads = [...uploads.filter(item => item.state === 'waiting' || item.state === 'sending'), ...added]
  emit()
  void run()
}
/** Stop the file that is sending and drop the files that wait. */
export function stopUploads() {
  uploads = uploads.map(item => item.state === 'waiting' ? { ...item, state: 'failed', error: 'Stopped.' } : item)
  queue.clear(); emit()
  current?.abort()
}
/** Hide the finished rows. Rows still going stay. */
export function clearUploads() { uploads = uploads.filter(item => item.state === 'waiting' || item.state === 'sending'); emit() }
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } }
export const useUploads = () => useSyncExternalStore(subscribe, () => uploads)
