const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const terminal = {}
runInNewContext(ts.transpileModule(readFileSync('src/terminalText.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, { exports: terminal })
const source = ts.createSourceFile('Chat.tsx', readFileSync('src/Chat.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
const names = ['squash', 'sameCommand', 'matches']
const declarations = source.statements.filter(node => ts.isVariableStatement(node) && node.declarationList.declarations.some(item => names.includes(item.name.getText(source))))
const decoder = {}
runInNewContext(ts.transpileModule(readFileSync('src/shellCommand.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText, { exports: decoder, require: name => { assert.equal(name, './terminalText'); return terminal } })
const context = { shellCommand: decoder.shellCommand }
const compile = text => ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText
runInNewContext(compile(declarations.map(node => node.getText(source)).join('\n') + '\nglobalThis.matches = matches;'), context)
const command = 'systemctl --user restart tether.service'
const text = '! ` ' + command + ' `\n\n```text\nFailed to connect to bus: No medium found\n```\n\nExit code: 1'
assert.equal(context.matches([{ role: 'user', text }], '!' + command), 1, 'An executed command must leave the pending queue')
console.log('Executed Codex command matches the pending input')
const { shellCommand } = decoder
const values = value => JSON.parse(JSON.stringify(value))
assert.deepEqual(values(shellCommand(text)), { command, output: 'Failed to connect to bus: No medium found', exit: 1 })
assert.deepEqual(values(shellCommand('! ` pwd `')), { command: 'pwd', output: '', exit: 0 })
assert.deepEqual(values(shellCommand('!\n\n```sh\necho one\necho two\n```\n\n```text\none\ntwo\n```')), { command: 'echo one\necho two', output: 'one\ntwo', exit: 0 })
assert.deepEqual(values(shellCommand('! `` echo `pwd` ``\n\n````text\nliteral ``` code <b>&\n````\n\nExit code: -1')), { command: 'echo `pwd`', output: 'literal ``` code <b>&', exit: -1 })
assert.equal(shellCommand('! echo `pwd`'), null)
assert.equal(shellCommand('! ` pwd `\n\nUnrecognized result'), null)
assert.equal(context.matches([{ role: 'assistant', text }], '!' + command), 0)
assert.equal(context.matches([{ role: 'user', text }, { role: 'user', text }], '!' + command), 2)
assert.equal(context.matches([{ role: 'user', text: '! ls' }], '!ls'), 1)
assert.equal(context.matches([{ role: 'user', text }], '!different-command'), 0)
console.log('Decoder: multiline commands, literal backticks, escaped output, exit codes, repeat counts and Claude command fallback pass')

const colored = '! ` git push origin main `\n\n```text\n\x1b[90m8:15AM\x1b[0m \x1b[32mINF\x1b[0m no leaks found\n```\n\nExit code: 1'
assert.equal(shellCommand(colored).output, '8:15AM INF no leaks found', 'Command output must not expose ANSI color controls')

const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const output = {}
let copied
runInNewContext(compile(readFileSync('src/Messages.tsx', 'utf8')), {
  exports: output,
  require(name) {
    if (['react', 'react/jsx-runtime', 'lucide-react'].includes(name)) return require(name)
    if (name === './shellCommand') return decoder
    if (name === './terminalText') return terminal
    if (name === './clipboard') return { copyText: async text => { copied = text } }
    return {}
  },
})
const render = value => renderToStaticMarkup(React.createElement(output.Message, { id: 'result', role: 'user', text: value, api() {}, session: '%fixture' }))
const coloredHtml = render(colored)
assert(coloredHtml.includes('8:15AM INF no leaks found'))
assert(!coloredHtml.includes('[90m'))
assert(!coloredHtml.includes('\x1b'))
const html = render(text)
assert(html.includes('Shell command result'))
assert(html.includes(command))
assert(html.includes('Exit 1'))
assert(html.includes('Failed to connect to bus: No medium found'))
assert(!html.includes('```'))
assert(!html.includes('` ' + command + ' `'))
const literal = render('! ` echo hello `\n\n```text\n<script>alert(1)</script> & x\n```')
assert(literal.includes('&lt;script&gt;alert(1)&lt;/script&gt; &amp; x'))
assert(!literal.includes('<script>'))
assert(render('! ls').includes('aria-label="Shell command"'))
const copy = output.CopyButton
const originalState = React.useState, originalEffect = React.useEffect
try {
  React.useState = () => ['', () => {}]
  React.useEffect = () => {}
  const button = copy({ text: output.Message({ id: 'result', role: 'user', text: colored }).props.children.at(-1).props.text, label: 'Copy command output' })
  button.props.onClick().then(() => {
    assert.equal(copied, '8:15AM INF no leaks found')
    console.log('Message: completed output uses existing terminal styles, preserves literal text, and copies correctly')
  }).catch(error => { console.error(error); process.exitCode = 1 })
} finally {
  React.useState = originalState; React.useEffect = originalEffect
}
