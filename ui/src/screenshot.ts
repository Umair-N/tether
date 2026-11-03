// "Latest screenshot": the user picks the Screenshots folder once; later taps attach its newest image.
// Chrome on Android has showDirectoryPicker from version 132. The folder handle is kept in IndexedDB.
interface Folder {
  kind: 'directory'
  queryPermission(options: { mode: 'read' }): Promise<PermissionState>
  requestPermission(options: { mode: 'read' }): Promise<PermissionState>
  values(): AsyncIterable<{ kind: 'file' | 'directory'; name: string; getFile?: () => Promise<File> }>
}
type Picker = (options: { id?: string; mode?: 'read'; startIn?: string }) => Promise<Folder>
const IMAGE = /\.(png|jpe?g|webp|gif|heic|heif)$/i
const STAMP = /(\d{8})[-_]?(\d{6})/

export const canPickFolder = () => typeof (window as unknown as { showDirectoryPicker?: Picker }).showDirectoryPicker === 'function'

function store<T>(mode: IDBTransactionMode, run: (objects: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open('tether-files', 1)
    open.onupgradeneeded = () => open.result.createObjectStore('handles')
    open.onerror = () => reject(open.error)
    open.onsuccess = () => {
      const request = run(open.result.transaction('handles', mode).objectStore('handles'))
      request.onsuccess = () => { resolve(request.result); open.result.close() }
      request.onerror = () => { reject(request.error); open.result.close() }
    }
  })
}

async function savedFolder(): Promise<Folder | undefined> {
  const folder = await store<Folder | undefined>('readonly', objects => objects.get('screenshots')).catch(() => undefined)
  if (!folder) return undefined
  let state = await folder.queryPermission({ mode: 'read' }).catch(() => 'denied' as PermissionState)
  if (state === 'prompt') state = await folder.requestPermission({ mode: 'read' }).catch(() => 'denied' as PermissionState)
  return state === 'granted' ? folder : undefined
}

async function newest(folder: Folder): Promise<File | null> {
  const entries: { name: string; getFile: () => Promise<File> }[] = []
  for await (const entry of folder.values()) if (entry.kind === 'file' && entry.getFile && IMAGE.test(entry.name)) entries.push({ name: entry.name, getFile: entry.getFile.bind(entry) })
  if (!entries.length) return null
  // Screenshot names carry the time (Screenshot_20261101-083242_Chrome.png), so read only the newest few by name.
  const stamped = entries.every(entry => STAMP.test(entry.name))
  const key = (name: string) => { const match = STAMP.exec(name); return match ? match[1] + match[2] : '' }
  const candidates = stamped ? [...entries].sort((a, b) => key(b.name).localeCompare(key(a.name))).slice(0, 5) : entries
  let best: File | null = null
  for (const entry of candidates) { const file = await entry.getFile(); if (!best || file.lastModified > best.lastModified) best = file }
  return best
}

// Returns the newest image, or null when the user cancels the folder picker.
export async function latestScreenshot(): Promise<File | null> {
  const saved = await savedFolder()
  const found = saved && await newest(saved)
  if (found) return found
  const pick = (window as unknown as { showDirectoryPicker: Picker }).showDirectoryPicker
  let folder: Folder
  try { folder = await pick({ id: 'screenshots', mode: 'read', startIn: 'pictures' }) } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') return null
    throw error
  }
  await store('readwrite', objects => objects.put(folder, 'screenshots')).catch(() => undefined)
  const file = await newest(folder)
  if (!file) throw new Error('That folder has no images. Tap Latest screenshot again and pick the Screenshots folder.')
  return file
}

// Forget the saved folder, so the next tap asks again.
export function forgetFolder() { return store('readwrite', objects => objects.delete('screenshots')).catch(() => undefined) }
