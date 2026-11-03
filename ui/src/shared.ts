// Files shared to Tether from another app wait in the service worker's cache (see public/sw.js).
const INBOX = 'tether-share-inbox'
const taking = new Set<string>()

export interface SharedFile { file: File; url: string }

// Read the waiting files. A file stays in the inbox until doneShared() is called after its upload works,
// so a failed upload is tried again the next time a composer opens.
export async function takeShared(): Promise<SharedFile[]> {
  if (!('caches' in window)) return []
  const cache = await caches.open(INBOX)
  const shared: SharedFile[] = []
  for (const request of await cache.keys()) {
    if (taking.has(request.url)) continue
    const response = await cache.match(request)
    if (!response) continue
    taking.add(request.url)
    const name = decodeURIComponent(response.headers.get('X-Name') || 'shared')
    shared.push({ url: request.url, file: new File([await response.blob()], name, { type: response.headers.get('Content-Type') || '' }) })
  }
  if (location.search.includes('shared=')) history.replaceState(null, '', location.pathname)
  return shared
}

export async function doneShared(item: SharedFile) {
  await (await caches.open(INBOX)).delete(item.url)
  taking.delete(item.url)
}

// Give back files that did not upload.
export function releaseShared(items: SharedFile[]) { for (const item of items) taking.delete(item.url) }

export function registerShareWorker() {
  if ('serviceWorker' in navigator) void navigator.serviceWorker.register('/chat/sw.js', { scope: '/chat/' }).catch(() => undefined)
}
