// Run: node test_background_resume.cjs
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const key = 'tether.backgroundJob.v1'
const item = { id: 'a'.repeat(32), url: `/mobile/api/backgrounds/${'a'.repeat(32)}.webp`, source: 'ai', created: 1 }
const code = ts.transpileModule(readFileSync('src/backgrounds.ts', 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText

function fixture(age = 10000) {
  let now = 1000000
  let nextTimer = 0
  const timers = new Map()
  const storage = new Map(age === null ? [] : [[key, JSON.stringify({ job: 'job /1', startedAt: now - age })]])
  const localStorage = {
    getItem: name => storage.get(name) ?? null,
    setItem: (name, value) => storage.set(name, value),
    removeItem: name => storage.delete(name),
  }
  const setTimeout = (callback, delay) => {
    const id = ++nextTimer
    timers.set(id, { callback, at: now + delay })
    return id
  }
  const exports = {}
  runInNewContext(code, {
    exports, localStorage, AbortController, DOMException, Date: { now: () => now },
    setTimeout, clearTimeout: id => timers.delete(id),
    AbortSignal: {
      any: signals => AbortSignal.any(signals),
      timeout: delay => {
        const controller = new AbortController()
        setTimeout(() => controller.abort(new DOMException('Timed out', 'TimeoutError')), delay)
        return controller.signal
      },
    },
  })
  const tick = async delay => {
    now += delay
    for (const [id, timer] of timers) {
      if (timer.at <= now) { timers.delete(id); timer.callback() }
    }
    await new Promise(resolve => setImmediate(resolve))
  }
  return { ...exports, storage, localStorage, tick }
}

async function main() {
  const resumed = fixture()
  const calls = []
  const handle = resumed.resumeBackground(async (...args) => {
    calls.push(args)
    return calls.length === 1 ? { state: 'running' } : { state: 'done', item }
  })
  assert(handle)
  assert.equal(handle.resumed, true)
  assert.equal(resumed.backgroundJob(), handle, 'Settings finds the resumed handle')
  assert.equal(resumed.resumeBackground(() => assert.fail('Duplicate poll')), null)
  assert.equal(resumed.startBackground(() => assert.fail('Duplicate POST'), 'new', true), handle)
  await resumed.tick(2000)
  assert(resumed.storage.has(key), 'A running job stays stored')
  await resumed.tick(2000)
  assert.equal(await handle.promise, item)
  assert.equal(calls[0][0], 'backgrounds/generate?job=job%20%2F1')
  assert.equal(calls[0][1], undefined, 'Resume never POSTs a new job')
  assert.equal(resumed.storage.has(key), false)
  assert.equal(resumed.backgroundJob(), null)

  for (const failure of [Object.assign(new Error('Not found'), { status: 404 }), { state: 'failed', error: 'Generation failed' }]) {
    const failed = fixture()
    const handle = failed.resumeBackground(async () => {
      if (failure instanceof Error) throw failure
      return failure
    })
    const rejected = assert.rejects(handle.promise, /Not found|Generation failed/)
    await failed.tick(2000)
    await rejected
    assert.equal(failed.storage.has(key), false, '404 and failed jobs clear storage')
    assert.equal(failed.backgroundJob(), null)
  }

  const stale = fixture(330001)
  assert.equal(stale.resumeBackground(() => assert.fail('Stale job polled')), null)
  assert.equal(stale.storage.has(key), false)

  const aborted = fixture()
  const stopped = aborted.resumeBackground(() => assert.fail('Stop waiting must stop polling'))
  const rejected = assert.rejects(stopped.promise, { name: 'AbortError' })
  stopped.controller.abort()
  await rejected
  assert.equal(aborted.storage.has(key), false)
  assert.equal(aborted.backgroundJob(), null)

  const timed = fixture(319000)
  const timeout = timed.resumeBackground(() => assert.fail('The original time limit must apply'))
  const expired = assert.rejects(timeout.promise, { name: 'TimeoutError' })
  await timed.tick(1000)
  await expired
  assert.equal(timed.storage.has(key), false)

  const started = fixture(null)
  const fresh = started.startBackground(async (path, body) => {
    if (body) { assert.equal(path, 'backgrounds/generate'); return { job: 'new-job' } }
    return { state: 'done', item }
  }, 'forest', true)
  await started.tick(0)
  assert.deepEqual(JSON.parse(started.storage.get(key)), { job: 'new-job', startedAt: 1000000 })
  await started.tick(2000)
  assert.equal(await fresh.promise, item)
  assert.equal(started.storage.has(key), false)

  const blocked = fixture(null)
  for (const method of ['getItem', 'setItem', 'removeItem']) blocked.localStorage[method] = () => { throw new Error('Storage blocked') }
  assert.equal(blocked.resumeBackground(() => assert.fail('No stored job')), null)
  const unstored = blocked.startBackground(async (_path, body) => body ? { job: 'unstored' } : { state: 'done', item }, 'forest', true)
  await blocked.tick(0)
  await blocked.tick(2000)
  assert.equal(await unstored.promise, item)

  console.log('Background resume: stored job, one job, 404, failure, stale job, abort, original timeout, persistence and blocked storage pass')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
