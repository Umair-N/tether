const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const source = ts.transpileModule(readFileSync('src/settingsIndex.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const output = {}
runInNewContext(source, { exports: output, require })
const match = output.matchesSetting
const folder = { id: 'folder', group: 'workspace', title: 'Default folder', keywords: 'working directory home path' }
const mic = { id: 'microphone', group: 'voice', title: 'Voice button', keywords: 'mic microphone dictate' }
const colors = { id: 'colors', group: 'appearance', title: 'Colors', keywords: 'Moss Rosé palette' }
assert(match(folder, '  DEFAULT   directory  '))
assert(match(folder, 'sessions folder'))
assert(!match(folder, 'directory microphone'))
assert(match(mic, 'microphone'))
assert(!match(mic, 'notifications'), 'Category descriptions must not return unrelated controls')
assert(match(colors, 'rose'), 'Search accepts unaccented palette names')
assert(match(colors, 'appearance'))
assert(!match(colors, 'no-such-setting'))
assert.equal(output.SETTINGS_GROUPS.length, 5)
console.log('Settings search: case, whitespace, multiword intersection, aliases, categories, accents and unrelated-result exclusion pass')
