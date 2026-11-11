// Skeleton primitives: markup, screen-reader labels and remembered counts. No terminal writes.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const ts = require('typescript')
const compile = source => ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText
function load(file, imports = {}, globals = {}) {
  const exports = {}
  runInNewContext(compile(readFileSync(`src/${file}`, 'utf8')), {
    exports, Error, ...globals,
    require(name) {
      if (name in imports) return imports[name]
      if (['react', 'react/jsx-runtime'].includes(name)) return require(name)
      throw new Error(`Unexpected import: ${name}`)
    },
  })
  return exports
}
const LoaderCircle = props => React.createElement('svg', { 'data-icon': 'loader', ...props })
const { Bone, Skeleton, Loading, Spinner, SkeletonRows } = load('Skeleton.tsx', { 'lucide-react': { LoaderCircle }, './Skeleton.css': {} })
const html = node => renderToStaticMarkup(node)

// Bone: hidden from screen readers, sized when asked, round when asked.
let out = html(React.createElement(Bone, { w: '40%', round: true }))
assert.match(out, /^<span class="bone round" style="width:40%" aria-hidden="true"><\/span>$/)

// Skeleton: one status label for screen readers, busy area, bones inside.
out = html(React.createElement(Skeleton, { label: 'Loading chats', className: 'x' }, React.createElement(Bone)))
assert.match(out, /^<div class="skeleton x" aria-busy="true"><span class="sk-label" role="status">Loading chats<\/span><span class="bone"/)

// Loading: a status note with a hidden spinner; the host class stays; the tag can change.
out = html(React.createElement(Loading, { className: 'hint' }))
assert.match(out, /^<p class="loading-note hint" role="status"><svg[^>]*class="spinner"[^>]*aria-hidden="true"[^>]*><\/svg>Loading…<\/p>$/)
assert.match(html(React.createElement(Loading, { as: 'li', label: 'Wait' })), /^<li class="loading-note"[^>]*>.*Wait<\/li>$/)

// Spinner: hidden without a label; an image with its name with a label.
assert.match(html(React.createElement(Spinner)), /aria-hidden="true"/)
out = html(React.createElement(Spinner, { label: 'Opening' }))
assert.match(out, /aria-label="Opening"/); assert.match(out, /role="img"/); assert.doesNotMatch(out, /aria-hidden/)

// SkeletonRows: the asked count, icon and meta lines optional, row list hidden from screen readers.
out = html(React.createElement(SkeletonRows, { count: 3, label: 'Loading files', className: 'files-skeleton', icon: 14, meta: false }))
assert.equal(out.match(/class="sk-row"/g).length, 3)
assert.equal(out.match(/sk-icon/g).length, 3)
assert.doesNotMatch(out, /sk-meta/)
assert.match(out, /<ul aria-hidden="true">/)
assert.match(out, /class="skeleton sk-rows files-skeleton"/)
out = html(React.createElement(SkeletonRows, { count: 2, label: 'x', icon: false }))
assert.doesNotMatch(out, /sk-icon/); assert.equal(out.match(/sk-meta/g).length, 2)

// Remembered counts: fallback first, then the last real count, capped; storage errors fall back.
const store = new Map()
const localStorage = { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) }
const counts = load('skeletonCounts.ts', {}, { localStorage, Number, Math })
assert.equal(counts.rememberedCount('home.done', 2), 2)
counts.rememberCount('home.done', 3); assert.equal(counts.rememberedCount('home.done', 2), 3)
counts.rememberCount('home.done', 40); assert.equal(counts.rememberedCount('home.done', 2), 4)
counts.rememberCount('home.needs', 0); assert.equal(counts.rememberedCount('home.needs', 1), 0)
const broken = load('skeletonCounts.ts', {}, { localStorage: { getItem() { throw Error('blocked') }, setItem() { throw Error('blocked') } }, Number, Math })
assert.equal(broken.rememberedCount('home.done', 2), 2); broken.rememberCount('home.done', 1)

console.log('skeleton tests passed')
