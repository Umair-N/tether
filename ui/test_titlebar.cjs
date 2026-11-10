const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
// The window controls inset rules (src/titlebar.ts), loaded without React.
const code = ts.transpileModule(readFileSync('src/titlebar.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } }).outputText
const mod = {}
runInNewContext(code, { exports: mod, require: () => ({ useEffect: () => {} }) })
const { controlInsets } = mod
const plain = value => JSON.parse(JSON.stringify(value))
const box = (left, right, top = 0, bottom = 900) => ({ left, right, top, bottom })
// Windows/Linux: controls on the right, 138px wide, strip 32px high, free area 0..1302 of 1440.
const win = { x: 0, width: 1302, height: 32 }
assert.deepEqual(plain(controlInsets(box(1100, 1440), win, 1440)), { left: 0, right: 138, height: 32 }, 'right column under the controls')
assert.deepEqual(plain(controlInsets(box(300, 1100), win, 1440)), { left: 0, right: 0, height: 0 }, 'middle panel is clear')
assert.deepEqual(plain(controlInsets(box(1320, 1430, 8), win, 1440)), { left: 0, right: 110, height: 24 }, 'a floating card 8px down: the whole card width, 24px down')
assert.deepEqual(plain(controlInsets(box(1100, 1440, 40), win, 1440)), { left: 0, right: 0, height: 0 }, 'below the strip')
// macOS: controls on the left, 80px; free area 80..1440.
const mac = { x: 80, width: 1360, height: 40 }
assert.deepEqual(plain(controlInsets(box(60, 300), mac, 1440)), { left: 20, right: 0, height: 40 }, 'sidebar next to the rail')
assert.deepEqual(plain(controlInsets(box(0, 60), mac, 1440)), { left: 60, right: 0, height: 40 }, 'the rail')
assert.deepEqual(plain(controlInsets(box(300, 1440), mac, 1440)), { left: 0, right: 0, height: 0 })
// Overlay hidden (height 0) or a panel with no size: nothing.
assert.deepEqual(plain(controlInsets(box(1100, 1440), { x: 0, width: 0, height: 0 }, 1440)), { left: 0, right: 0, height: 0 })
assert.deepEqual(plain(controlInsets(box(1100, 1440, 0, 0), win, 1440)), { left: 0, right: 0, height: 0 })
console.log('titlebar ok')
