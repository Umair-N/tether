// Composer + sheet: it opens in the top layer and sits above the + button. No terminal writes.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const code = ts.transpileModule(readFileSync('src/attachLayer.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } }).outputText
const mod = {}
runInNewContext(code, { exports: mod, Math })
const { placeAttachLayer } = mod

function fakeLayer(withPopover = true) {
  const calls = [], vars = {}
  const layer = { style: { setProperty: (name, value) => { vars[name] = value } } }
  if (withPopover) layer.showPopover = () => calls.push('showPopover')
  return { layer, calls, vars }
}
const anchor = (left, top) => ({ getBoundingClientRect: () => ({ left, top }) })

// It enters the top layer, then sits 8px above the + button, at the button's left edge.
let f = fakeLayer()
placeAttachLayer(f.layer, anchor(389.4, 640), 800)
assert.deepEqual(f.calls, ['showPopover'])
assert.equal(f.vars['--attach-left'], '389px')
assert.equal(f.vars['--attach-bottom'], '168px')

// It never leaves the screen: an off-screen anchor clamps to an 8px margin.
f = fakeLayer()
placeAttachLayer(f.layer, anchor(-30, 900), 800)
assert.equal(f.vars['--attach-left'], '8px')
assert.equal(f.vars['--attach-bottom'], '8px')

// A browser without the Popover API still gets a placed sheet.
f = fakeLayer(false)
placeAttachLayer(f.layer, anchor(20, 500), 600)
assert.equal(f.vars['--attach-bottom'], '108px')

// The menu renders only inside the manual popover layer, and the layer CSS adds no z-index race.
const composer = readFileSync('src/Composer.tsx', 'utf8')
const layerAt = composer.indexOf('<div className="attach-layer" popover="manual" ref={layer}><div className="attach-backdrop"')
assert.ok(layerAt > 0, 'the sheet opens in a manual popover layer')
assert.ok(composer.indexOf('className="attach-menu"') > layerAt, 'the menu is inside the layer')
assert.equal((composer.match(/className="attach-menu"/g) || []).length, 1)
const css = readFileSync('src/App.css', 'utf8')
const rule = css.match(/\.attach-layer > \.attach-menu \{[^}]*\}/)
assert.ok(rule, 'layer menu rule')
assert.doesNotMatch(rule[0], /z-index/)
console.log('attach layer: top layer, placement, clamps and markup pass')
