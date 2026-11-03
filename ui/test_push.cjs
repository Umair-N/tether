// Exercise notification bodies with a mock service worker; no browser writes.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const { join } = require('node:path')

async function main() {
  const handlers = new Map(), displayed = []
  let shown = []
  const self = {
    addEventListener(name, callback) { handlers.set(name, callback) },
    location: { origin: 'https://tether.test' },
    registration: {
      async getNotifications(filter = {}) { return shown.filter(item => !filter.tag || item.tag === filter.tag) },
      async showNotification(title, options) { displayed.push({ title, ...options }) },
    },
  }
  runInNewContext(readFileSync(join(__dirname, 'public/sw.js'), 'utf8'), { self, URL })
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
  await click(second, 'agents')
  await click(second)
  assert.deepEqual(opened, ['/chat/?go=agents', '/chat/?session=%252'])
  console.log('Push worker checks passed: separate pane notifications, All agents action, old summary cleanup, approval actions.')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
