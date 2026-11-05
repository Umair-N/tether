// Auto mode block notices: what the card says. Run: node test_notice.cjs
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const code = ts.transpileModule(readFileSync('src/noticeText.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const moduleExports = {}
runInNewContext(code, { exports: moduleExports })
const { autoBlock } = moduleExports

assert.deepEqual({ ...autoBlock('bash blocked by auto mode · /permissions · Auto mode: No reason provided') }, { title: 'Auto mode blocked Bash', reason: '' })
assert.deepEqual({ ...autoBlock('Bash(git push --force) blocked by auto mode · /permissions · Auto mode: [Data Exfiltration]') }, { title: 'Auto mode blocked git push --force', reason: 'Data Exfiltration' })
assert.deepEqual({ ...autoBlock('webfetch blocked by auto mode') }, { title: 'Auto mode blocked Webfetch', reason: '' })
assert.equal(autoBlock('5% until auto-compact'), null)
assert.equal(autoBlock('Auto mode on (shift+tab to cycle)'), null)
console.log('notice: auto mode blocks get a plain title; "No reason provided" is dropped')
