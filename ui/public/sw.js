// Tether service worker: notifications and files from Android's Share sheet.
// The manifest share_target posts them to /chat/share; we keep them in a cache until the app attaches them.
const INBOX = 'tether-share-inbox'
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()))
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url)
  if (event.request.method !== 'POST' || url.pathname !== '/chat/share') return
  event.respondWith((async () => {
    const form = await event.request.formData()
    const cache = await caches.open(INBOX)
    let index = 0
    for (const file of form.getAll('media')) {
      if (!(file instanceof File)) continue
      const key = `/chat/share-inbox/${Date.now()}-${index++}`
      await cache.put(key, new Response(file, { headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-Name': encodeURIComponent(file.name || 'shared') } }))
    }
    return Response.redirect('/chat/?shared=1', 303)
  })())
})

// Android folds 4+ notifications of one app into a group whose tap only starts the app on its start page.
// So Tether keeps its own summary instead: from the second plain update on, the updates fold into one notification
// (tag tether-summary) whose tap opens the Agents page. A yes/no prompt stays on its own, for its Approve and Deny.
const SUMMARY = 'tether-summary'
const ICONS = { icon: '/chat/icon-192.png', badge: '/chat/badge-96.png' }
function summaryText(text) {
  const line = text.replace(/\s+/g, ' ').trim()
  return line.length <= 80 ? line : line.slice(0, 79).trimEnd() + '…'
}
self.addEventListener('push', event => {
  let payload = {}
  try { payload = event.data?.json() || {} } catch { /* still show a notification for an empty push */ }
  event.waitUntil(notify(payload))
})
async function notify(payload) {
  const title = payload.title || 'Tether', body = payload.body || ''
  if (!payload.approval && payload.session) {
    const shown = (await self.registration.getNotifications()).filter(item => item.tag === SUMMARY || (item.data?.session && !item.data?.approval && item.tag !== payload.tag))
    // One line per pane: a newer update of the same pane replaces its line.
    const lines = shown.flatMap(item => item.tag === SUMMARY ? item.data?.lines || [] : [{ tag: item.tag, text: `${item.title} ${item.data?.body || item.body || ''}`.trim() }])
      .filter(line => line.tag !== payload.tag)
    if (lines.length) {
      const all = [{ tag: payload.tag, text: `${title} ${body}`.trim() }, ...lines].slice(0, 20)
        .map(line => ({ ...line, text: summaryText(line.text) }))
      for (const item of shown) item.close()
      await self.registration.showNotification(`${all.length} agent updates`, { ...ICONS, tag: SUMMARY, renotify: true,
        body: all.map(line => line.text).join('\n'), data: { go: 'agents', lines: all } })
      return
    }
  }
  await self.registration.showNotification(title, { ...ICONS, body, tag: payload.tag,
    data: { session: payload.session, thread: payload.thread, approval: payload.approval, body: payload.body },
    // A plain yes/no prompt gets Approve and Deny (the server picked it as safe to answer from here); others get Mute.
    actions: payload.approval ? [{ action: 'approve', title: 'Approve' }, { action: 'deny', title: 'Deny' }]
      : payload.session ? [{ action: 'mute', title: 'Mute this pane' }] : [],
  })
}
// Answer the prompt by its id. The server checks the prompt on screen is still that one, so an old notification cannot
// approve a newer request. The result replaces the notification quietly; a refusal says why and opens the pane on tap.
async function answer(notification, approve) {
  const { session, thread, approval, body } = notification.data || {}
  let error = ''
  try {
    const response = await fetch('/mobile/api/approval', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' },
      body: JSON.stringify({ session, thread, approval, action: approve ? 'approve' : 'reject' }) })
    if (!response.ok) error = (await response.json().catch(() => ({}))).error || `Error ${response.status}`
  } catch { error = 'Could not reach the server.' }
  const title = error ? `${notification.title}: not ${approve ? 'approved' : 'denied'}` : `${notification.title}: ${approve ? 'Approved' : 'Denied'}`
  await self.registration.showNotification(title, { body: error ? `${error} Tap to open.` : (body || ''), tag: notification.tag, silent: !error,
    icon: '/chat/icon-192.png', badge: '/chat/badge-96.png', data: { session } })
  if (!error) {
    await new Promise(resolve => setTimeout(resolve, 4000))
    for (const shown of await self.registration.getNotifications({ tag: notification.tag })) if (shown.title === title) shown.close()
  }
}
self.addEventListener('notificationclick', event => {
  event.notification.close()
  const session = event.notification.data?.session
  if ((event.action === 'approve' || event.action === 'deny') && event.notification.data?.approval) {
    event.waitUntil(answer(event.notification, event.action === 'approve'))
    return
  }
  if (event.action === 'mute' && session) {
    // One tap from the notification: no window opens.
    event.waitUntil(fetch('/mobile/api/push/mute', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' }, body: JSON.stringify({ session, muted: true }) }))
    return
  }
  event.waitUntil(event.notification.data?.go === 'agents' ? openAgents() : openPane(session))
})
// The summary opens the Agents page: in the open Tether window when there is one, else in a new window.
async function openAgents() {
  const chat = await lastWindow()
  if (chat) {
    chat.postMessage({ go: 'agents' })
    try { await chat.focus(); return } catch { /* fall through and open a window */ }
  }
  await self.clients.openWindow('/chat/?go=agents')
}
async function lastWindow() {
  const windows = (await self.clients.matchAll({ type: 'window', includeUncontrolled: true })).filter(client => {
    const at = new URL(client.url)
    return at.origin === self.location.origin && at.pathname.startsWith('/chat/')
  })
  return windows.find(client => client.focused) ?? windows.find(client => client.visibilityState === 'visible') ?? windows[0]
}
// Open the pane from the notification. Prefer the Tether window the person last used: focused, then visible, then any.
// Send the pane first: focus() can be refused (another macOS Space, or Chrome in the background), and a refusal must not
// leave the window on the old pane. If no window takes focus, open a new one on the pane.
async function openPane(session) {
  const url = '/chat/' + (session ? `?session=${encodeURIComponent(session)}` : '')
  const chat = await lastWindow()
  if (chat) {
    if (session) chat.postMessage({ session })
    try { await chat.focus(); return } catch { /* fall through and open a window */ }
  }
  await self.clients.openWindow(url)
}
