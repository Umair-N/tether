// Client for the Share file store: GET/POST /mobile/api/share/... (server/share_api.py).
import { ApiError, request } from './api'

export type ShareKind = 'image' | 'video' | 'audio' | 'pdf' | 'text' | 'other'
/** 'you' for your uploads, 'agent:NAME' for files an agent put in Share. */
export type ShareSource = 'you' | `agent:${string}`
export interface ShareFile {
  id: string; name: string; size: number; mime: string; kind: ShareKind
  created: string; modified: string; has_thumb: boolean; source: ShareSource
  width?: number; height?: number; duration?: number; deleted_at?: string
}
export interface ShareUsage { used: number; quota: number; count: number }
export interface ShareList { files: ShareFile[]; next: string | null; usage: ShareUsage }
export type ShareFilter = 'all' | 'image' | 'video' | 'audio' | 'doc' | 'other'

const BASE = '/mobile/api/share/'
export const rawUrl = (id: string, download = false) => `${BASE}raw/${encodeURIComponent(id)}${download ? '?download=1' : ''}`
export const thumbUrl = (file: ShareFile) => `${BASE}thumb/${encodeURIComponent(file.id)}?v=${encodeURIComponent(file.modified)}`

export function listShare(options: { kind?: ShareFilter; q?: string; source?: 'all' | 'you' | 'agent'; cursor?: string | null; limit?: number; trash?: boolean }, signal?: AbortSignal) {
  const query = new URLSearchParams({ kind: options.kind ?? 'all', limit: String(options.limit ?? 60) })
  if (options.q) query.set('q', options.q)
  if (options.source && options.source !== 'all') query.set('source', options.source)
  if (options.cursor) query.set('cursor', options.cursor)
  if (options.trash) query.set('trash', '1')
  return request<ShareList>(`share/list?${query}`, undefined, signal)
}

export const textPreview = async (id: string, signal?: AbortSignal) => {
  const response = await fetch(`${BASE}raw/${encodeURIComponent(id)}?text=1`, { credentials: 'same-origin', signal })
  if (!response.ok) throw new ApiError(response.status, 'Could not load the text.')
  return response.text()
}

export const renameShare = (id: string, name: string) => request<{ ok: true; files: ShareFile[] }>('share/rename', { id, name })
export const trashShare = (ids: string[]) => request<unknown>('share/delete', { ids })
export const restoreShare = (ids: string[]) => request<unknown>('share/restore', { ids })
/** Copies the file where the agent can read it. Returns the path for an @mention. */
export const shareToChat = (id: string) => request<{ path: string }>('share/to-chat', { id }, undefined, undefined, 60000)

const write = (xhr: XMLHttpRequest) => { xhr.withCredentials = true; xhr.setRequestHeader('X-Codex-Mobile', '1') }
const fail = (xhr: XMLHttpRequest) => {
  let message = `Upload failed (${xhr.status || 'no connection'}).`
  try { message = JSON.parse(xhr.responseText).error || message } catch { /* not JSON */ }
  return new ApiError(xhr.status, message)
}

export interface UploadMeta { width?: number; height?: number; duration?: number }
/** Sends one file as the raw request body. onProgress gets the bytes sent so far. */
export function uploadShare(file: File, meta: UploadMeta, onProgress: (sent: number) => void, signal?: AbortSignal) {
  return new Promise<ShareFile>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${BASE}upload`)
    write(xhr)
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name))
    xhr.setRequestHeader('X-File-Type', file.type || 'application/octet-stream')
    for (const [key, value] of Object.entries(meta)) if (value != null && Number.isFinite(value)) xhr.setRequestHeader(`X-${key[0].toUpperCase()}${key.slice(1)}`, String(Math.round(value * 1000) / 1000))
    xhr.upload.onprogress = event => onProgress(event.loaded)
    xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve(JSON.parse(xhr.responseText).file) : reject(fail(xhr))
    xhr.onerror = () => reject(fail(xhr))
    xhr.onabort = () => reject(new DOMException('Upload stopped.', 'AbortError'))
    signal?.addEventListener('abort', () => xhr.abort(), { once: true })
    xhr.send(file)
  })
}

export async function putThumb(id: string, blob: Blob) {
  const response = await fetch(`${BASE}thumb/${encodeURIComponent(id)}`, {
    method: 'POST', credentials: 'same-origin', body: blob,
    headers: { 'Content-Type': blob.type, 'X-Codex-Mobile': '1' },
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new ApiError(response.status, data.error || 'Could not save the preview.')
  return data.file as ShareFile
}

/** Reads the size and length of a photo or video, and draws a JPEG thumbnail (longest side 480 px). */
export async function inspect(file: File): Promise<{ meta: UploadMeta; thumb?: Blob }> {
  const url = URL.createObjectURL(file)
  try {
    if (file.type.startsWith('image/')) {
      const bitmap = await createImageBitmap(file).catch(() => null)
      if (!bitmap) return { meta: {} }
      const meta = { width: bitmap.width, height: bitmap.height }
      return { meta, thumb: await draw(bitmap, bitmap.width, bitmap.height) ?? undefined }
    }
    if (file.type.startsWith('video/') || file.type.startsWith('audio/')) {
      const video = document.createElement(file.type.startsWith('video/') ? 'video' : 'audio')
      video.preload = 'metadata'; video.muted = true; video.src = url
      await once(video, 'loadedmetadata')
      const meta: UploadMeta = { duration: Number.isFinite(video.duration) ? video.duration : undefined }
      if (!(video instanceof HTMLVideoElement) || !video.videoWidth) return { meta }
      meta.width = video.videoWidth; meta.height = video.videoHeight
      video.currentTime = Math.min(1, (video.duration || 0) / 3)
      await once(video, 'seeked')
      return { meta, thumb: await draw(video, video.videoWidth, video.videoHeight) ?? undefined }
    }
    if (file.type === 'application/pdf' || /\.pdf$/i.test(file.name)) {
      const { pdfThumb } = await import('./sharePdf')
      return { meta: {}, thumb: await pdfThumb(url) ?? undefined }
    }
    return { meta: {} }
  } catch {
    return { meta: {} }
  } finally {
    URL.revokeObjectURL(url)
  }
}

function once(element: HTMLMediaElement, name: string) {
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), 8000)
    element.addEventListener(name, () => { clearTimeout(timer); resolve() }, { once: true })
    element.addEventListener('error', () => { clearTimeout(timer); reject(new Error('media')) }, { once: true })
  })
}

function draw(source: CanvasImageSource, width: number, height: number) {
  const scale = Math.min(1, 480 / Math.max(width, height))
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(width * scale)); canvas.height = Math.max(1, Math.round(height * scale))
  canvas.getContext('2d')?.drawImage(source, 0, 0, canvas.width, canvas.height)
  return new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.8))
}

// Resumable uploads: POST share/uploads, then 8 MiB chunks to share/uploads/<id>?offset=N (server/share_api.py).
export interface ShareUpload { id: string; name: string; size: number; offset: number; mime: string; created: string; expires: number }
export const CHUNK = 8 * 1024 * 1024
export const listUploads = (signal?: AbortSignal) => request<{ uploads: ShareUpload[] }>('share/uploads', undefined, signal).then(r => r.uploads)
export const cancelUpload = (id: string) => request<{ ok: true }>(`share/uploads/${encodeURIComponent(id)}/cancel`, {}).then(r => { forget(id); return r })
export type Wait = 'offline' | 'retrying' | null

const sleep = (ms: number, signal?: AbortSignal) => new Promise<void>((resolve, reject) => {
  const t = setTimeout(resolve, ms)
  signal?.addEventListener('abort', () => { clearTimeout(t); reject(new DOMException('Upload stopped.', 'AbortError')) }, { once: true })
})
const online = (signal?: AbortSignal) => navigator.onLine ? Promise.resolve() : new Promise<void>((resolve, reject) => {
  window.addEventListener('online', () => resolve(), { once: true })
  signal?.addEventListener('abort', () => reject(new DOMException('Upload stopped.', 'AbortError')), { once: true })
})

// A file with the same name and size is not always the same file. Each session keeps a fingerprint in this browser
// (modified time and a hash of the first 64 KiB), so Resume joins only the same file.
const PRINT = 'tether.shareUpload.'
async function fingerprint(file: File) {
  const head = new Uint8Array(await file.slice(0, 64 * 1024).arrayBuffer())
  let hash = 0x811c9dc5 // FNV-1a: works without a secure context, unlike crypto.subtle.
  for (const byte of head) hash = Math.imul(hash ^ byte, 0x01000193)
  return `${file.lastModified}:${file.size}:${(hash >>> 0).toString(16)}`
}
const remember = (id: string, print: string) => { try { localStorage.setItem(PRINT + id, print) } catch { /* no storage */ } }
const recall = (id: string) => { try { return localStorage.getItem(PRINT + id) } catch { return null } }
const forget = (id: string) => { try { localStorage.removeItem(PRINT + id) } catch { /* no storage */ } }
/** True when `file` may continue `session`: same name and size, and the same fingerprint when this browser has one. */
export async function sameFile(session: ShareUpload, file: File) {
  if (session.name !== file.name || session.size !== file.size) return false
  const known = recall(session.id)
  return known == null || known === await fingerprint(file)
}

function sendChunk(id: string, offset: number, body: Blob, onProgress: (sent: number) => void, signal?: AbortSignal) {
  return new Promise<{ status: number; data: { upload?: ShareUpload; file?: ShareFile; offset?: number; error?: string } }>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${BASE}uploads/${encodeURIComponent(id)}?offset=${offset}`)
    write(xhr)
    xhr.upload.onprogress = event => onProgress(event.loaded)
    const done = () => { let data = {}; try { data = JSON.parse(xhr.responseText) } catch { /* not JSON */ } resolve({ status: xhr.status, data }) }
    xhr.onload = done; xhr.onerror = done; xhr.ontimeout = done
    xhr.onabort = () => reject(new DOMException('Upload stopped.', 'AbortError'))
    signal?.addEventListener('abort', () => xhr.abort(), { once: true })
    xhr.send(body)
  })
}

/**
 * Sends a file in chunks and survives dropped connections and server restarts: it waits for the network, asks the
 * server for its offset, and goes on from there. Pass `resume` to continue a session from an earlier page load.
 * Returns null when the server has no resumable route (an older server), so the caller can use uploadShare.
 */
export async function uploadResumable(file: File, meta: UploadMeta, onProgress: (sent: number) => void, onWait: (wait: Wait) => void,
  signal?: AbortSignal, resume?: ShareUpload): Promise<ShareFile | null> {
  const print = await fingerprint(file)
  // A different file under the same name starts its own upload. The old session stays, so its own file can still resume it.
  let session = resume && (recall(resume.id) ?? print) === print ? resume : undefined
  if (!session) {
    try {
      session = (await request<{ upload: ShareUpload }>('share/uploads', { name: file.name, size: file.size, mime: file.type || 'application/octet-stream', ...meta }, signal)).upload
      remember(session.id, print)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null
      throw e
    }
  }
  let offset = session.offset, tries = 0
  onProgress(offset)
  while (true) {
    await online(signal)
    const end = Math.min(file.size, offset + CHUNK)
    const { status, data } = await sendChunk(session.id, offset, file.slice(offset, end), sent => onProgress(offset + sent), signal)
    if (status === 200 && data.file) { onWait(null); forget(session.id); return data.file }
    if (status === 200 && data.upload) { offset = data.upload.offset; tries = 0; onWait(null); onProgress(offset); continue }
    // 409 with a new offset: go on from there. With the same offset, an earlier chunk still holds the session: wait below.
    if (status === 409 && typeof data.offset === 'number' && data.offset !== offset) { offset = data.offset; onProgress(offset); continue }
    // Lost connection, a restart (502/503) or a busy disk: wait and ask again. Anything else is final.
    if (status === 0 || status >= 500 || status === 409) {
      onWait(navigator.onLine ? 'retrying' : 'offline')
      await sleep(Math.min(30_000, 1000 * 2 ** Math.min(tries++, 5)), signal)
      if (navigator.onLine) {
        const fresh = await request<{ upload: ShareUpload }>(`share/uploads/${encodeURIComponent(session.id)}`, undefined, signal).catch((e: unknown) => e)
        if (signal?.aborted) throw new DOMException('Upload stopped.', 'AbortError')
        if (fresh instanceof ApiError && fresh.status === 404) { forget(session.id); throw new ApiError(404, 'The server lost this upload. Add the file again.') }
        if (fresh && typeof fresh === 'object' && 'upload' in fresh) { offset = (fresh as { upload: ShareUpload }).upload.offset; onProgress(offset) }
      }
      continue
    }
    throw new ApiError(status, data.error || `Upload failed (${status}).`)
  }
}

/** One thumbnail at a time for files that have none (files an agent added). Failed ids are not tried again. */
const tried = new Set<string>()
let chain: Promise<unknown> = Promise.resolve()
export function backfillThumb(file: ShareFile): Promise<ShareFile | null> {
  if (file.has_thumb || tried.has(file.id)) return Promise.resolve(null)
  if (!(file.kind === 'pdf' || file.kind === 'video' || (file.kind === 'image' && file.size > 400_000))) return Promise.resolve(null)
  tried.add(file.id)
  const job = chain.then(async () => {
    const url = rawUrl(file.id)
    let blob: Blob | null = null
    if (file.kind === 'pdf') blob = await (await import('./sharePdf')).pdfThumb(url)
    else if (file.kind === 'image') {
      const bitmap = await fetch(url, { credentials: 'same-origin' }).then(r => r.ok ? r.blob() : null).then(b => b && createImageBitmap(b)).catch(() => null)
      if (bitmap) { blob = await draw(bitmap, bitmap.width, bitmap.height); bitmap.close() }
    } else {
      const video = document.createElement('video')
      video.preload = 'metadata'; video.muted = true; video.src = url
      try {
        await once(video, 'loadedmetadata')
        video.currentTime = Math.min(1, (video.duration || 0) / 3)
        await once(video, 'seeked')
        if (video.videoWidth) blob = await draw(video, video.videoWidth, video.videoHeight)
      } catch { /* the browser cannot play this video */ } finally { video.removeAttribute('src'); video.load() }
    }
    return blob ? await putThumb(file.id, blob).catch(() => null) : null
  })
  chain = job.catch(() => null)
  return job.catch(() => null)
}
