const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const output = {}
runInNewContext(ts.transpileModule(readFileSync('src/paneSelection.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, { exports: output })
const { selectPane } = output
const pane = (id, session) => ({ id, tmux_session: session, thread: id })
const session = (id, ids) => ({ id, panes: ids.map(value => pane(value, id)) })
const before = [session('other', ['x']), session('work', ['a', 'b', 'c', 'd'])]
const pick = (ids, selected = 'c', old = before, last = {}) => selectPane(ids, selected, { sessions: old, selected }, 'x', last)?.id
assert.equal(pick([before[0], session('work', ['a', 'b', 'd'])]), 'b', 'Closing active tab selects its left neighbor, not default chat in another session')
assert.equal(pick([before[0], session('work', ['b', 'c', 'd'])], 'a'), 'b', 'Leftmost selects right neighbor')
assert.equal(pick([before[0], session('work', ['a', 'b', 'c'])], 'd'), 'c', 'Rightmost selects left')
assert.equal(pick([before[0], session('work', ['a', 'c', 'd'])]), 'c', 'Inactive close keeps selection')
assert.equal(pick([before[0], session('work', ['a', 'd'])]), 'a', 'Concurrent close skips missing neighbor')
assert.equal(pick([before[0], session('work', ['d'])]), 'd', 'Uses surviving right neighbor if all left neighbors ended')
const reordered = [before[0], session('work', ['d', 'b', 'a', 'c'])]
assert.equal(pick([before[0], session('work', ['d', 'b', 'c'])], 'a', reordered), 'b', 'Saved visual order determines neighbor')
const groups = [session('left', ['l1', 'l2']), session('middle', ['m']), session('right', ['r'])]
assert.equal(pick([groups[0], groups[2]], 'm', groups, { left: 'l2' }), 'l2', 'Last tab selects previous session and its remembered tab')
assert.equal(pick([groups[1], groups[2]], 'l1', groups), 'm', 'First session selects next session')
assert.equal(pick([], 'm', groups), undefined, 'Last session shows empty state')
assert.equal(pick([session('work', ['a'])], 'a', [session('work', ['a', 'b'])]), 'a', 'Single remaining tab stays selected')
assert.equal(pick(before, 'new', [], {}), 'x', 'Missing selection at startup uses default')
assert.equal(selectPane(before, null, { sessions: [], selected: undefined }, 'c', {})?.id, 'c')
console.log('Tab close: left/right neighbors, inactive, reordered, concurrent, last tab/session, startup pass')
assert.equal(selectPane([before[0], session('work', ['a', 'b', 'd'])], null, { sessions: before, selected: 'c' }, 'x', {})?.id, 'b', 'A default-selected tab also recovers locally after closing')
assert.equal(selectPane(before, 'b', { sessions: before, selected: 'c' }, 'x', {})?.id, 'b', 'Explicit user navigation wins over old snapshot')
console.log('Tab close: implicit initial selection and explicit navigation pass')
