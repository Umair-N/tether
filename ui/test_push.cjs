// Exercise notification bodies with a mock service worker; no browser writes.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const { join } = require('node:path')

async function main() {
  const handlers = new Map(), displayed = []
  let shown = []
  runInNewContext(readFileSync(join(__dirname, 'public/sw.js'), 'utf8'), {
    self: {
      addEventListener(name, callback) { handlers.set(name, callback) },
      registration: {
        async getNotifications() { return shown },
        async showNotification(title, options) { displayed.push({ title, ...options }) },
      },
    },
  })
  async function push(payload) {
    let work
    handlers.get('push')({ data: { json: () => payload }, waitUntil(promise) { work = promise } })
    await work
    return displayed.at(-1)
  }
  const body = 'Finished: ' + 'A reply with details. '.repeat(8)
  const single = await push({ title: 'Pane one', body, session: '%1', tag: 'session-%1' })
  assert.equal(single.body, body, 'single notification preserves the full snippet')
  let closed = 0
  shown = [{ ...single, close() { closed++ } }]
  const summary = await push({ title: 'Pane two', body: body + '\nMore', session: '%2', tag: 'session-%2' })
  assert.equal(summary.title, '2 agent updates')
  assert.equal(closed, 1)
  const lines = summary.body.split('\n')
  assert.equal(lines.length, 2, 'one line per pane even with multiline bodies')
  assert(lines.every(line => line.length <= 80 && line.endsWith('…')))
  assert(lines[0].startsWith('Pane two Finished:'))
  assert(lines[1].startsWith('Pane one Finished:'))
  shown = [{ ...summary, close() { closed++ } }]
  const update = await push({ title: 'Pane one', body: 'Needs your input: Which file?', session: '%1', tag: 'session-%1' })
  assert.equal(update.data.lines.length, 2, 'same pane replaces its summary line')
  assert.equal(update.body.split('\n')[0], 'Pane one Needs your input: Which file?')
  const approval = await push({ title: 'Approval', body, session: '%3', tag: 'session-%3', approval: 'yes-no' })
  assert.equal(approval.body, body, 'approval keeps its own full notification')
  assert.equal(approval.actions[0].action, 'approve')
  console.log('Push worker checks passed: full single bodies, bounded summary lines, pane replacement, approval actions.')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
