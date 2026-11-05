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
