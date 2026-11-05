// Auto mode block notices: what the card says. Run: node test_notice.cjs
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const code = ts.transpileModule(readFileSync('src/noticeText.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const moduleExports = {}
runInNewContext(code, { exports: moduleExports })
const { autoBlock } = moduleExports

assert.deepEqual({ ...autoBlock('bash blocked by auto mode · /permissions · Auto mode: No reason provided') }, { title: 'Auto mode blocked a shell command', reason: '', command: '' })
assert.deepEqual({ ...autoBlock('Bash(git push --force) blocked by auto mode · /permissions · Auto mode: [Data Exfiltration]') }, { title: 'Auto mode blocked a shell command', reason: 'Data Exfiltration', command: 'git push --force' })
assert.deepEqual({ ...autoBlock('webfetch blocked by auto mode') }, { title: 'Auto mode blocked a web request', reason: '', command: '' })
assert.deepEqual({ ...autoBlock('Mcp__x blocked by auto mode') }, { title: 'Auto mode blocked Mcp__x', reason: '', command: '' })
// The hook's facts: the tool's own description is the title, the full command waits behind the toggle.
assert.deepEqual({ ...autoBlock('Bash(S=/tmp/x; cd ~/app && node refs.mjs) blocked by auto mode · /permissions · Auto mode: [Bot Check Circumvention]', { tool: 'Bash', description: 'Screenshot Dribbble search pages', command: 'S=/tmp/x; cd ~/app && node refs.mjs', reason: '[Bot Check Circumvention]' }) },
  { title: 'Auto mode blocked: Screenshot Dribbble search pages', reason: 'Bot Check Circumvention', command: 'S=/tmp/x; cd ~/app && node refs.mjs' })
assert.equal(autoBlock('bash blocked by auto mode', { reason: 'Blocked by classifier' }).reason, '')
assert.equal(autoBlock('5% until auto-compact'), null)
assert.equal(autoBlock('Auto mode on (shift+tab to cycle)'), null)
console.log('notice: auto mode blocks say what Claude tried; the command waits behind the toggle; "No reason provided" is dropped')
