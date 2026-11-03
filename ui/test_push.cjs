// Exercise notification bodies with a mock service worker; no browser writes.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const { join } = require('node:path')

async function main() {
  const handlers = new Map(), displayed = [], requests = []
  let shown = []
  const self = {
    addEventListener(name, callback) { handlers.set(name, callback) },
    location: { origin: 'https://tether.test' },
    registration: {
      async getNotifications(filter = {}) { return shown.filter(item => !filter.tag || item.tag === filter.tag) },
      async showNotification(title, options) { displayed.push({ title, ...options }) },
    },
  }
  runInNewContext(readFileSync(join(__dirname, 'public/sw.js'), 'utf8'), { self, URL, fetch: async (url, options) => { requests.push({ url, ...options }); return { ok: true } } })
  async function push(payload) {
    let work
    handlers.get('push')({ data: { json: () => payload }, waitUntil(promise) { work = promise } })
    await work
    return displayed.at(-1)
  }
  const body = 'Finished: ' + 'A reply with details. '.repeat(8)
  const single = await push({ title: 'Pane one', body, session: '%1', tag: 'session-%1' })
  assert.equal(single.body, body, 'single notification preserves the full snippet')
  assert.deepEqual(Array.from(single.actions, action => action.action), ['mute', 'agents'])
  assert.equal(single.silent, false)
  assert.equal(single.actions[0].title, 'Urgent only')
  const quiet = await push({ title: 'Background', session: '%8', priority: 'low', silent: true })
  assert.equal(quiet.silent, true)
  // A second pane gets its own notification; nothing folds into a summary.
  let closed = 0
  shown = [{ ...single, close() { closed++ } }]
  const second = await push({ title: 'Pane two', body, session: '%2', tag: 'session-%2' })
  assert.equal(second.title, 'Pane two')
  assert.equal(second.tag, 'session-%2')
  assert.equal(second.body, body)
  assert.equal(closed, 0, 'other panes stay on screen')
  // A leftover summary from the old worker is closed.
  shown = [{ title: '2 agent updates', tag: 'tether-summary', data: { go: 'agents' }, close() { closed++ } }]
  await push({ title: 'Pane three', body, session: '%3', tag: 'session-%3' })
  assert.equal(closed, 1, 'old summary closed')
  shown = []
  const approval = await push({ title: 'Approval', body, session: '%4', tag: 'session-%4', approval: 'yes-no' })
  assert.equal(approval.body, body, 'approval keeps its own full notification')
  assert.deepEqual(Array.from(approval.actions, action => action.action), ['approve', 'deny'])
  // "All agents" opens the Agents page; a plain tap opens the pane.
  const opened = []
  self.clients = { async matchAll() { return [] }, async openWindow(url) { opened.push(url) } }
  async function click(notification, action = '') {
    let work
    handlers.get('notificationclick')({ notification: { ...notification, close() {} }, action, waitUntil(promise) { work = promise } })
    await work
  }
  await click(second, 'mute')
  assert.equal(requests[0].url, '/mobile/api/push/level')
  assert.deepEqual(JSON.parse(requests[0].body), { session: '%2', level: 'urgent' })
  assert.equal(requests[0].headers['X-Codex-Mobile'], '1')
  await click(second, 'agents')
  await click(second)
  assert.deepEqual(opened, ['/chat/?go=agents', '/chat/?session=%252'])
  // The legacy worker ignores priority and still records its badge and pane URL.
  const legacyHandlers = new Map(), legacyShown = [], cached = new Map()
  let legacyClosed = 0
  const legacySelf = {
    addEventListener(name, callback) { legacyHandlers.set(name, callback) },
    navigator: { async setAppBadge(count) { assert.equal(count, 1) }, async clearAppBadge() {} },
    registration: {
      async showNotification(title, options) { legacyShown.push({ title, ...options }) },
      async getNotifications() { return [{ close() { legacyClosed++ } }] },
    },
  }
  const cache = { async put(key, value) { cached.set(key, value) }, async keys() { return [...cached.keys()] }, async delete(key) { cached.delete(key) } }
  runInNewContext(readFileSync(join(__dirname, '../server/sw.js'), 'utf8'), {
    self: legacySelf, URL, Response, encodeURIComponent, caches: { async open() { return cache } },
  })
  let legacyWork
  legacyHandlers.get('push')({ data: { json: () => ({ title: 'Legacy', body, priority: 'urgent', silent: false, tag: 'session-%9', url: '/mobile/#session=%259' }) }, waitUntil(promise) { legacyWork = promise } })
  await legacyWork
  assert.equal(legacyShown[0].body, body)
  assert.equal(legacyShown[0].data.url, '/mobile/#session=%259')
  assert.equal(legacyShown[0].silent, true, 'legacy badge-only delivery stays quiet')
  assert.equal(legacyClosed, 1)
  console.log('Push worker checks passed: separate pane notifications, All agents action, old summary cleanup, approval actions, silent delivery, Urgent only action, legacy compatibility.')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
