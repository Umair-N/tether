// Quiet watch cycles. Run: node test_watch_cycles.cjs
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const code = ts.transpileModule(readFileSync('src/watchCycles.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const moduleExports = {}
runInNewContext(code, { exports: moduleExports, require: () => ({}) })
const { foldWatchCycles } = moduleExports

const quiet = (id, watch = 'CI-card') => ({ id, role: 'assistant', kind: 'notice', quiet: 'watch', watch, text: 'Watch expired', time: id })
const rearm = (id, tool = {}) => ({ id, role: 'assistant', kind: 'actions', text: '', time: id, tools: [{ id: 'monitor', name: 'Monitor', status: 'complete', is_error: false, ...tool }] })
const reply = (id, text = 'The watch expired, so I started it again.\n\nNeeds you: no') => ({ id, role: 'assistant', text, time: id })
const user = { id: 'user', role: 'user', text: 'Next task', time: 'user' }

const run = [quiet('q1'), rearm('r1'), reply('a1'), quiet('q2'), rearm('r2'), reply('a2')]
const messages = [...run, user]
const snapshot = JSON.stringify(messages)
const folded = foldWatchCycles(messages)
assert.equal(folded.length, 2)
assert.deepEqual({ ...folded[0], members: [...folded[0].members], watches: [...folded[0].watches] }, {
  id: 'watch:q1', role: 'assistant', kind: 'watch', text: '', time: 'a2',
  members: run, watches: ['CI-card'], cycles: 2, rearmed: true,
})
assert.equal(folded[1], user)
assert.equal(JSON.stringify(messages), snapshot)
run.forEach((message, index) => assert.equal(folded[0].members[index], message))

const needsYou = reply('yes', 'Needs you: yes')
const needsResult = foldWatchCycles([run[0], run[1], needsYou])
assert.equal(needsResult[0].members.length, 2)
assert.equal(needsResult[1], needsYou)

const invalid = [
  { ...quiet('notice'), quiet: undefined },
  rearm('bash', { name: 'Bash' }),
  rearm('pending', { status: 'pending' }),
  rearm('error', { is_error: true }),
  { ...rearm('empty'), tools: [] },
  { ...rearm('missing'), tools: undefined },
  { ...rearm('mixed'), tools: [...rearm('monitor').tools, ...rearm('bash', { name: 'Bash' }).tools] },
]
assert.equal(foldWatchCycles(invalid), invalid)
for (const message of invalid) {
  const result = foldWatchCycles([quiet('q'), message])
  assert.equal(result.length, 2)
  assert.equal(result[0].members.length, 1)
  assert.equal(result[0].rearmed, false)
  assert.equal(result[1], message)
}

const twoWatches = foldWatchCycles([quiet('x', 'X'), quiet('y', 'Y'), rearm('r'), reply('a')])[0]
assert.deepEqual([...twoWatches.watches], ['X', 'Y'])
assert.equal(twoWatches.cycles, 2)

const plain = [rearm('r'), reply('a'), user]
assert.equal(foldWatchCycles(plain), plain)
const empty = []
assert.equal(foldWatchCycles(empty), empty)

for (const boundary of [reply('question', 'Restart it?'), reply('long', 'x'.repeat(401)), user]) {
  const result = foldWatchCycles([quiet('q'), boundary])
  assert.equal(result[0].members.length, 1)
  assert.equal(result[1], boundary)
}
for (const boundary of [reply('second'), rearm('second')]) {
  const result = foldWatchCycles([quiet('q'), reply('first'), boundary, quiet('next')])
  assert.equal(result.length, 3)
  assert.equal(result[0].members.length, 2)
  assert.equal(result[1], boundary)
  assert.equal(result[2].kind, 'watch')
}
assert.equal(foldWatchCycles([quiet('q'), reply('limit', '  ' + 'x'.repeat(400) + '  ')])[0].members.length, 2)
assert.equal(foldWatchCycles([quiet('q'), reply('case', 'NEEDS YOU:   YES')])[0].members.length, 1)
assert.deepEqual([...foldWatchCycles([quiet('x', 'X'), quiet('y', 'Y'), quiet('x2', 'X')])[0].watches], ['X', 'Y'])

console.log('watch cycles: quiet runs fold; boundaries, labels, identity, and input purity pass')
