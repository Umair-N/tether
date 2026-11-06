const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const ts = require('typescript')
const compile = text => ts.transpileModule(text, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve() }
const text = node => node == null || typeof node === 'boolean' ? '' : typeof node === 'string' || typeof node === 'number' ? String(node) : Array.isArray(node) ? node.map(text).join('') : text(node.props?.children)
const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)]
function harness() {
  let cursor = 0, values = [], tree, closed = 0, created
  const calls = [], props = { pane: { id: '%1', agent: 'claude', thread: 'thread' }, thread: 'thread', idle: true,
    onClose() { closed++ }, onCreated(id) { created = id }, api: (path, body) => new Promise((resolve, reject) => calls.push({ path, body, resolve, reject })) }
  const react = { ...React, useState(initial) { const at = cursor++; if (!(at in values)) values[at] = typeof initial === 'function' ? initial() : initial; return [values[at], value => { values[at] = typeof value === 'function' ? value(values[at]) : value }] } }
  const output = {}
  runInNewContext(compile(readFileSync('src/ContinueWith.tsx', 'utf8')), { exports: output, require(name) {
    if (name === 'react') return react
    if (name === 'react/jsx-runtime' || name === 'lucide-react') return require(name)
    if (name === './api') return { errorText: error => error.message }
    if (name === './hooks') return { useModal: (_, close) => ({ onCancel: event => { event.preventDefault(); close() } }) }
    throw Error(name)
  } })
  return { calls, props, render() { cursor = 0; tree = output.ContinueWith(props); return tree },
    button(label) { return nodes(tree).find(node => node.type === 'button' && (node.props['aria-label'] === label || text(node) === label)) },
    get tree() { return tree }, get closed() { return closed }, get created() { return created } }
}
const preview = { target: 'codex-plan', request: 'request-one', version: 'version-one', native: false, brief: '<script>reference & data</script>', characters: 200, estimated_tokens: 50, message_count: 4,
  choices: [{ id: 'claude', label: 'Claude', current: true, ready: true }, { id: 'codex-plan', label: 'Codex', current: false, ready: true }, { id: 'codex-foundry', label: 'Foundry', current: false, ready: false }] }
async function main() {
  const h = harness(); h.render()
  assert(h.button('Claudecurrent').props.disabled)
  h.button('Preview handoff').props.onClick(); h.render()
  assert.equal(h.calls.length, 1); assert.equal(h.calls[0].path, 'continue-preview')
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0].body)), { session: '%1', thread: 'thread', target: 'codex-plan' })
  assert(h.button('Close').props.disabled)
  let prevented = false; h.tree.props.onCancel({ preventDefault() { prevented = true } }); assert(prevented); assert.equal(h.closed, 0)
  h.calls[0].resolve(preview); await flush(); h.render()
  const html = renderToStaticMarkup(h.tree)
  assert(html.includes('&lt;script&gt;reference &amp; data&lt;/script&gt;'))
  assert(!html.includes('<script>'))
  assert(html.includes('50 tokens; estimate only'))
  assert(h.button('Codex · Foundrynot set up').props.disabled)
  assert.equal(h.calls.length, 1, 'No polling or model request during context review')
  h.props.idle = false; h.render(); assert(h.button('Continue').props.disabled)
  h.props.idle = true; h.render(); h.button('Continue').props.onClick(); h.render()
  assert.equal(h.calls[1].path, 'continue')
  assert.equal(h.calls[1].body.request, 'request-one')
  h.calls[1].reject(new Error('offline')); await flush(); h.render()
  assert(renderToStaticMarkup(h.tree).includes('offline'))
  h.button('Continue').props.onClick(); h.render()
  assert.equal(JSON.stringify(h.calls[1].body), JSON.stringify(h.calls[2].body), 'Retry keeps its idempotency key and preview')
  h.calls[2].resolve({ pane: '%2' }); await flush(); assert.equal(h.created, '%2')
  assert.equal(h.props.pane.id, '%1', 'Original chat identity stays intact')
  console.log('Continue UI: destination, two-stage preview, escaping, idle guard, busy close guard, readiness, error retry and pane selection pass')
  const native = harness(); native.render(); native.button('Preview handoff').props.onClick()
  native.calls[0].resolve({ ...preview, native: true, brief: '' }); await flush(); native.render()
  const nativeHtml = renderToStaticMarkup(native.tree)
  assert(nativeHtml.includes('native conversation history'))
  assert(!nativeHtml.includes('Review the context brief'))
  native.button('Refresh preview').props.onClick(); native.calls[1].resolve({ ...preview, request: 'request-two' }); await flush(); native.render()
  native.button('Continue').props.onClick(); assert.equal(native.calls[2].body.request, 'request-two')
  console.log('Continue UI: native fork omits duplicate brief; refreshed preview replaces the request key')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
