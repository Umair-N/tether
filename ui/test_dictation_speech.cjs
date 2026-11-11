// Real dictation hook with fake audio and API calls. No terminal writes.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
function load(file, imports, globals = {}) {
  const exports = {}
  runInNewContext(ts.transpileModule(readFileSync(`src/${file}`, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, {
    exports, Error, Blob, AbortController, DOMException, ...globals,
    require(name) {
      if (name in imports) return imports[name]
      if (['react/jsx-runtime', 'lucide-react'].includes(name)) return require(name)
      throw new Error(`Unexpected import: ${name}`)
    },
  })
  return exports
}
function harness() {
  let cursor = 0, effects = []
  const slots = [], cleanups = []
  const hooks = {
    useState(initial) {
      const id = cursor++
      if (!(id in slots)) slots[id] = typeof initial === 'function' ? initial() : initial
      return [slots[id], value => { slots[id] = typeof value === 'function' ? value(slots[id]) : value }]
    },
    useRef(initial) { const id = cursor++; return slots[id] ??= { current: initial } },
    useMemo(fn, deps) {
      const id = cursor++, old = slots[id]
      if (!old || deps.some((dep, at) => dep !== old.deps[at])) slots[id] = { deps, value: fn() }
      return slots[id].value
    },
    useEffect(fn, deps) {
      const id = cursor++, old = slots[id]
      if (!old || deps.some((dep, at) => dep !== old[at])) {
        slots[id] = deps; effects.push(() => { cleanups[id]?.(); cleanups[id] = fn() })
      }
    },
  }
  hooks.useLayoutEffect = hooks.useEffect
  return {
    hooks,
    render(fn) { cursor = 0; const result = fn(); const pending = effects; effects = []; pending.forEach(fn => fn()); return result },
    unmount() { cleanups.forEach(fn => fn?.()) },
  }
}
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve() }
const deferred = () => { let resolve; const promise = new Promise(ok => { resolve = ok }); return { promise, resolve } }
function environment(label = '') {
  let now = 0, nextTimer = 1
  const timers = new Map(), records = [], calls = [], streams = []
  const response = text => ({ ok: true, headers: { get: () => 'application/json' }, json: async () => ({ text }) })
  const env = { calls, records, streams, timers, result: '', focus: 0, chimes: [], spoken: 0, fine: true }
  const stream = () => {
    const track = { label, stops: 0, stop() { this.stops++ } }
    const value = { track, getAudioTracks: () => [track], getTracks: () => [track] }
    streams.push(value); return value
  }
  class Recorder {
    static isTypeSupported() { return true }
    constructor(stream, options) { this.stream = stream; this.mimeType = options.mimeType; this.state = 'inactive'; records.push(this) }
    start() { this.state = 'recording' }
    data() { this.ondataavailable?.({ data: new Blob(['audio']) }) }
    stop() { assert.notEqual(this.state, 'inactive'); this.state = 'inactive'; queueMicrotask(() => this.onstop?.()) }
  }
  const setTimer = (fn, ms) => { const id = nextTimer++; timers.set(id, { fn, ms }); return id }
  env.globals = {
    Date: { now: () => now }, crypto: { randomUUID: () => 'fake-recording' }, MediaRecorder: Recorder,
    navigator: { mediaDevices: { getUserMedia: async () => env.acquire ? env.acquire.promise : stream() } },
    window: { setInterval: setTimer, setTimeout: setTimer, clearInterval: id => timers.delete(id), clearTimeout: id => timers.delete(id) },
    matchMedia: () => ({ matches: env.fine }), localStorage: { getItem() {}, setItem() {} },
    fetch: async (path, options) => {
      calls.push({ path, options })
      if (options.headers['X-Live']) return response('rough preview')
      if (env.failure) throw env.failure
      if (env.delayed) return new Promise((resolve, reject) => {
        env.delayed.promise.then(text => resolve(response(text)))
        options.signal.addEventListener('abort', () => reject(new DOMException('Cancelled', 'AbortError')))
      })
      return response(env.result)
    },
  }
  env.advance = ms => { now += ms }
  env.stream = stream
  return env
}
function setup(env, options = {}) {
  const h = harness()
  let text = options.text || '', done = 0
  const { useDictation } = load('useDictation.ts', {
    react: h.hooks, './api': { errorText: error => error.message },
    './sound': { prepareChime: () => env.chimes.push('prepare'), playChime: async () => env.chimes.push('play') },
    './speech': { stopSpeaking: () => env.spoken++ },
  }, env.globals)
  const props = { text, setText: next => { text = next }, chime: false, onHandsFreeDone: () => done++, onFinished: () => env.focus++, ...options }
  return { h, useDictation, render: () => h.render(() => useDictation({ ...props, text })), text: () => text, done: () => done }
}
function speechEnvironment({ webkit = false, language = 'en-GB' } = {}) {
  const env = environment()
  env.recognitions = []
  class Recognition {
    constructor() { env.recognitions.push(this); this.starts = 0; this.stops = 0; this.aborts = 0 }
    start() { this.starts++; if (this.failStart) throw new Error('Cannot restart') }
    stop() { this.stops++; queueMicrotask(() => this.onend?.()) }
    abort() { this.aborts++; queueMicrotask(() => this.onerror?.({ error: 'aborted' })) }
    result(...parts) { this.onresult?.({ results: parts.map(([transcript, isFinal]) => ({ 0: { transcript }, isFinal })) }) }
    error(error) { this.onerror?.({ error }) }
    end() { this.onend?.() }
  }
  env.globals.window[webkit ? 'webkitSpeechRecognition' : 'SpeechRecognition'] = Recognition
  env.globals.navigator.language = language
  env.fire = ms => {
    const found = [...env.timers.entries()].filter(([, timer]) => timer.ms === ms)
    assert(found.length, `No ${ms} ms timer`)
    for (const [id, timer] of found) {
      if (ms !== 250) env.timers.delete(id)
      timer.fn()
    }
  }
  return env
}
async function main() {
  for (const webkit of [false, true]) {
    const env = speechEnvironment({ webkit, language: webkit ? '' : 'en-GB' })
    const s = setup(env, { text: 'Existing draft ', chime: true })
    assert.equal(s.render().canRecord, true)
    await s.render().toggle()
    const speech = env.recognitions[0], final = s.render().finalText()
    assert(speech, 'Use browser speech before MediaRecorder')
    assert.equal(speech.continuous, true); assert.equal(speech.interimResults, true)
    assert.equal(speech.lang, webkit ? 'en-US' : 'en-GB'); assert.equal(speech.maxAlternatives, 1)
    assert.deepEqual(env.chimes, ['prepare', 'play']); assert.equal(env.spoken, 1)
    assert(s.render().listening && s.render().active()); assert.equal(s.render().transcribing, false)
    speech.result([' src ', true], ['slash App.tsx', false])
    assert.equal(s.text(), 'Existing draft src/App.tsx')
    speech.result([' src SLASH App.tsx ', true], [' and tests ', false])
    assert.equal(s.text(), 'Existing draft src/App.tsx and tests')
    env.advance(2250); env.fire(250); assert.equal(s.render().seconds, 2)
    if (webkit) await s.render().toggle()
    else s.render().stop()
    assert.equal(s.render().transcribing, false)
    assert.equal(await final, 'Existing draft src/App.tsx and tests')
    assert.equal(s.render().transcribing, false); assert.equal(s.render().listening, false)
    assert.equal(s.render().active(), false); assert.equal(s.render().finalText(), null)
    assert.equal(env.calls.length, 0); assert.equal(env.records.length, 0)
    assert.equal(env.timers.size, 0); assert.equal(env.focus, 1); assert.equal(s.done(), 0)
    speech.end(); assert.equal(env.focus, 1)
  }
  console.log('Speech: standard/WebKit setup, interim streaming, append, slash tidy, stop/toggle and instant final text without fetch pass')

  for (const throws of [false, true]) {
    const env = speechEnvironment(), s = setup(env)
    await s.render().toggle()
    const first = env.recognitions[0], final = s.render().finalText()
    first.result([' first ', true], ['segment', false]); first.failStart = throws; first.end()
    const next = env.recognitions.at(-1)
    assert.equal(env.recognitions.length, throws ? 2 : 1)
    assert.equal(next.starts, throws ? 1 : 2)
    assert(s.render().listening && s.render().active())
    next.result(['second', true], ['segment', false])
    assert.equal(s.text(), 'first segment second segment')
    s.render().stop(); assert.equal(await final, 'first segment second segment')
    assert.equal(env.timers.size, 0); assert.equal(env.focus, 1)
  }
  const cappedEnv = speechEnvironment(), capped = setup(cappedEnv)
  await capped.render().toggle()
  const cappedFinal = capped.render().finalText(), cappedSpeech = cappedEnv.recognitions[0]
  for (let n = 0; n <= 20; n++) { cappedSpeech.result([`word${n}`, false]); cappedSpeech.end() }
  assert.equal(cappedSpeech.starts, 21)
  assert.equal(await cappedFinal, Array.from({ length: 21 }, (_, n) => `word${n}`).join(' '))
  assert.equal(cappedEnv.timers.size, 0); assert.equal(cappedEnv.focus, 1)
  console.log('Speech: unexpected end keeps earlier words, restarts with replacement on throw, and caps restarts at 20 pass')

  const handsEnv = speechEnvironment(), hands = setup(handsEnv, { handsFree: true, text: 'Base' })
  await hands.render().toggle()
  const handsFinal = hands.render().finalText(), handsSpeech = handsEnv.recognitions[0]
  assert.equal(handsEnv.streams.length, 0)
  handsSpeech.result(['first', false]); handsEnv.advance(1000)
  const oldTimer = [...handsEnv.timers.keys()].find(id => handsEnv.timers.get(id).ms === 1800)
  handsSpeech.result(['final speech', false]); assert.equal(handsEnv.timers.has(oldTimer), false)
  assert.equal([...handsEnv.timers.values()].some(timer => timer.ms === 8000), false)
  handsEnv.advance(1800); handsEnv.fire(1800); await flush()
  assert.equal(await handsFinal, 'Base final speech'); assert.equal(hands.done(), 1)
  handsSpeech.end(); assert.equal(hands.done(), 1); assert.equal(handsEnv.timers.size, 0)
  const silenceEnv = speechEnvironment(), silence = setup(silenceEnv, { handsFree: true, text: 'Base ' })
  await silence.render().toggle(); const silenceFinal = silence.render().finalText()
  silenceEnv.advance(8000); silenceEnv.fire(8000); await flush()
  assert.equal(await silenceFinal, 'Base'); assert.equal(silence.text(), 'Base')
  assert.equal(silence.done(), 0); assert.equal(silence.render().error, ''); assert.equal(silenceEnv.timers.size, 0)
  const noSpeechEnv = speechEnvironment(), noSpeech = setup(noSpeechEnv)
  await noSpeech.render().toggle(); const noSpeechFinal = noSpeech.render().finalText()
  noSpeechEnv.recognitions[0].error('no-speech'); noSpeech.render().stop()
  assert.equal(await noSpeechFinal, ''); assert.equal(noSpeech.render().error, 'No speech heard. Tap the mic and speak.')
  console.log('Speech: hands-free pause resets and sends once, hands-free silence stays quiet, manual silence shows an error pass')

  for (const error of ['service-not-allowed', 'network', 'language-not-supported']) {
    const env = speechEnvironment(), s = setup(env, { text: 'Base' })
    env.result = 'fallback words'
    await s.render().toggle(); const final = s.render().finalText()
    env.recognitions[0].error(error); await flush()
    assert.equal(env.recognitions[0].aborts, 1); assert.equal(env.records.length, 1)
    assert.equal(s.render().listening, true); assert.equal(s.render().error, '')
    env.advance(1000); env.records[0].data(); s.render().stop()
    assert.equal(await final, 'Base fallback words')
    assert.equal(env.calls.length, 1); assert.equal(env.timers.size, 0)
    await s.render().toggle(); assert.equal(env.recognitions.length, 1); assert.equal(env.records.length, 2)
    s.h.unmount(); await flush(); assert.equal(env.timers.size, 0)
  }
  for (const error of ['not-allowed', 'audio-capture']) {
    const env = speechEnvironment(), s = setup(env, { text: 'Base ' })
    await s.render().toggle(); const final = s.render().finalText()
    env.recognitions[0].error(error)
    assert.equal(await final, 'Base'); assert.equal(s.text(), 'Base')
    assert.equal(s.render().error, error === 'not-allowed'
      ? 'Allow microphone access for this site, then try again.'
      : 'Could not start the microphone. Close other apps that use it, then try again.')
    assert.equal(s.render().active(), false); assert.equal(env.records.length, 0); assert.equal(env.timers.size, 0)
    await s.render().toggle()
    assert.equal(env.recognitions.length, error === 'not-allowed' ? 2 : 1)
    assert.equal(env.records.length, error === 'not-allowed' ? 0 : 1)
    s.h.unmount(); await flush()
  }
  for (const error of ['network', 'not-allowed', 'service-not-allowed', 'audio-capture']) {
    const env = speechEnvironment(), s = setup(env)
    await s.render().toggle(); const final = s.render().finalText()
    env.recognitions[0].result(['keep these words', false]); env.recognitions[0].error(error)
    assert.equal(await final, 'keep these words'); assert.equal(env.records.length, 0)
    assert.equal(env.timers.size, 0); assert.equal(env.focus, 1)
  }
  console.log('Speech: same-toggle fallback, later toggles skip speech, permission/capture errors and partial text preservation pass')

  for (const close of [false, true]) {
    const env = speechEnvironment(), s = setup(env, { text: 'Base' })
    env.acquire = deferred()
    await s.render().toggle(); const final = s.render().finalText()
    env.recognitions[0].error('service-not-allowed'); await flush()
    if (close) {
      s.h.unmount(); env.acquire.resolve(env.stream())
    } else {
      // A rejected getUserMedia request must also settle an existing submit promise.
      env.acquire.resolve(Promise.reject(new DOMException('Denied', 'NotAllowedError')))
    }
    assert.equal(await final, 'Base'); assert.equal(env.timers.size, 0)
    assert.equal(env.records.length, 0)
    if (close) assert.equal(env.streams[0].track.stops, 1)
    else assert.equal(s.render().error, 'Allow microphone access for this site, then try again.')
  }
  console.log('Speech: fallback permission denial and unmount during microphone acquisition settle final text and release resources pass')

  const closedEnv = speechEnvironment(), closed = setup(closedEnv, { text: 'Base' })
  await closed.render().toggle(); const closedFinal = closed.render().finalText(), speech = closedEnv.recognitions[0]
  speech.result(['discard me', false]); closed.h.unmount(); await flush()
  assert.equal(await closedFinal, 'Base'); assert.equal(speech.aborts, 1)
  assert.equal(speech.onresult, null); assert.equal(speech.onend, null); assert.equal(speech.onerror, null)
  assert.equal(closedEnv.timers.size, 0); assert.equal(closedEnv.focus, 0); assert.equal(closedEnv.records.length, 0)
  const chimeEnv = speechEnvironment(), chime = setup(chimeEnv, { chime: true })
  const starting = chime.render().toggle(); chime.h.unmount(); await starting
  assert.equal(chimeEnv.recognitions.length, 0); assert.equal(chimeEnv.records.length, 0)
  const limitEnv = speechEnvironment(), limit = setup(limitEnv)
  await limit.render().toggle(); const limitFinal = limit.render().finalText()
  limitEnv.recognitions[0].result(['three minutes', false]); limitEnv.advance(180000); limitEnv.fire(250)
  assert.equal(await limitFinal, 'three minutes'); assert.equal(limitEnv.recognitions[0].stops, 1)
  assert.equal(limitEnv.timers.size, 0)
  const onlySpeechEnv = speechEnvironment(); delete onlySpeechEnv.globals.MediaRecorder; delete onlySpeechEnv.globals.navigator.mediaDevices
  assert.equal(setup(onlySpeechEnv).render().canRecord, true)
  console.log('Speech: unmount aborts and detaches, timers clear, chime disposal guard, 180 s limit and speech-only capability pass')
}
let finished = false
main().then(() => { finished = true }, error => { finished = true; console.error(error); process.exitCode = 1 })
process.on('beforeExit', () => {
  if (!finished) { console.error('Speech tests stopped with an unresolved promise'); process.exitCode = 1 }
})
