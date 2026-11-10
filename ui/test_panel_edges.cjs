const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
// Panel handle rules (src/panelEdges.ts): which side of a panel faces the chat, sizes, and where a closed panel reopens.
const code = ts.transpileModule(readFileSync('src/panelEdges.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } }).outputText
const mod = {}
runInNewContext(code, { exports: mod })
const { handles, dragSize, closedSide, sizeVars, sizeKey } = mod
const plain = value => JSON.parse(JSON.stringify(value))
const r = (left, right, top, bottom) => ({ left, right, top, bottom })
const chat = r(300, 1100, 0, 900)
const layout = { columns: [{ main: 'sessions', top: 'waiting' }, { main: 'chat' }, { main: 'status' }] }
// Sessions left, status right, waiting on top of the sessions column.
const found = [{ name: 'sessions', rect: r(56, 300, 200, 900) }, { name: 'waiting', rect: r(56, 300, 0, 200) }, { name: 'status', rect: r(1100, 1340, 0, 900) }]
assert.deepEqual(plain(handles(found, chat, layout).map(item => [item.name, item.edge])), [['sessions', 'right'], ['waiting', 'bottom'], ['status', 'left']])
assert.equal(sizeKey({ name: 'waiting', edge: 'bottom' }), 'bottom')
assert.equal(sizeKey({ name: 'status', edge: 'left' }), 'status')
const [sessions, waiting, status] = handles(found, chat, layout)
assert.equal(dragSize(sessions, 50, 0, 900), 294, 'column grows towards the chat')
assert.equal(dragSize(status, 50, 0, 900), 190, 'right column shrinks when dragged right')
assert.equal(dragSize(status, -900, 0, 900), 640, 'width cap')
assert.equal(dragSize(sessions, -900, 0, 900), 180, 'width floor')
assert.equal(dragSize(waiting, 0, 60, 900), 260, 'edge height grows')
assert.equal(dragSize(waiting, 0, 900, 900), 540, 'edge height cap is 60% of the shell')
const grid = { columns: [{ main: 'sessions', top: 'waiting' }, { main: 'chat', bottom: 'status' }, { main: 'activity' }] }
assert.equal(closedSide(grid, 'waiting'), 'left')
assert.equal(closedSide(grid, 'status'), 'bottom')
assert.equal(closedSide(grid, 'activity'), 'right')
assert.deepEqual(plain(sizeVars({ sessions: 300.4, top: 120, 'bad;name': 9, status: -1 })), { '--lw-sessions': '300px', '--lh-top': '120px', '--lw-badname': '9px' })
// The sessions column closed: waiting fills the column alone and becomes a column.
assert.equal(handles([{ name: 'waiting', rect: r(56, 300, 0, 900) }], chat, layout)[0].edge, 'right')
console.log('panel edges ok')
