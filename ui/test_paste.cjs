// Long-paste badges: when a paste collapses, and the text that goes to the agent. Run: node test_paste.cjs
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const code = ts.transpileModule(readFileSync('src/pasteText.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const moduleExports = {}
runInNewContext(code, { exports: moduleExports, Date, Math })
const { longPaste, pasteOf, joinPastes, inlinePaste } = moduleExports

// Claude Code 2.1.318, checked in a tmux pane: 3 lines and 800 characters stay inline; 4 lines or 801 characters collapse.
assert.equal(longPaste('a\nb\nc'), false)
assert.equal(longPaste('a\nb\nc\nd'), true)
assert.equal(longPaste('a\r\nb\r\nc'), false)
assert.equal(longPaste('y'.repeat(800)), false)
assert.equal(longPaste('y'.repeat(801)), true)
assert.equal(longPaste(''), false)
console.log('paste: collapses at the same size as Claude Code')

const log = pasteOf('one\r\ntwo\r\nthree\r\nfour\n\n', false)
assert.equal(log.text, 'one\ntwo\nthree\nfour')
assert.notEqual(pasteOf('x', true).id, pasteOf('x', true).id)
console.log('paste: line endings normalised, trailing blank lines dropped, ids unique')

const before = pasteOf('LOG', true), after = pasteOf('TAIL', false)
assert.equal(joinPastes('why does this fail?', [after]), 'why does this fail?\n\nTAIL')
assert.equal(joinPastes('why does this fail?', [before]), 'LOG\n\nwhy does this fail?')
assert.equal(joinPastes('q', [after, before]), 'LOG\n\nq\n\nTAIL')
assert.equal(joinPastes('', [after]), 'TAIL')
assert.equal(joinPastes('  ', [before, after]), 'LOG\n\nTAIL')
assert.equal(joinPastes('only text', []), 'only text')
console.log('paste: message order follows the caret at paste time')

assert.equal(inlinePaste('', after), 'TAIL')
assert.equal(inlinePaste('question  \n', after), 'question\n\nTAIL')
assert.equal(inlinePaste('question', before), 'LOG\n\nquestion')
console.log('paste: "Edit as text" puts the text where it would have been sent')

const { expandPlaceholders } = moduleExports
const four = 'line one\nline two\nline three\nline four'
assert.equal(expandPlaceholders('short message', [four]), 'short message')
assert.equal(expandPlaceholders('[Pasted text #12 +3 lines]', ['old', four]), four)
assert.equal(expandPlaceholders('[Pasted text #9]', ['y'.repeat(900)]), 'y'.repeat(900))
assert.equal(expandPlaceholders('[Pasted text #3 +3 lines]\n[Pasted text #4 +3 lines]', [four + '!', 'short', four, four + '!']), `${four}\n${four}!`)
assert.equal(expandPlaceholders('[Pasted text #1 +3 lines] and [Pasted text #2 +3 lines]', [four]), `[Pasted text #1 +3 lines] and ${four}`)
console.log('paste: Stop swaps Claude placeholders for the long messages Tether sent')
