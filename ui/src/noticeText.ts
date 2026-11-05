// Claude Code's footer line when auto mode's safety check stops a tool call:
// "bash blocked by auto mode · /permissions · Auto mode: No reason provided"
// "Bash(git push --force) blocked by auto mode · /permissions · Auto mode: [Data Exfiltration]"
// The title says what Claude tried in words; the raw command goes to `command`, shown only when the card opens.
export interface AutoBlock { title: string; reason: string; command: string }
export interface BlockFacts { tool?: string; description?: string; command?: string; reason?: string }
const NOUNS: Record<string, string> = { bash: 'a shell command', powershell: 'a shell command', webfetch: 'a web request', websearch: 'a web search', write: 'a file write', edit: 'a file edit', notebookedit: 'a notebook edit', read: 'a file read' }
export function autoBlock(note: string, facts?: BlockFacts | null): AutoBlock | null {
  const match = note.match(/^(.+?) blocked by auto mode(?:\s*·\s*\/permissions)?(?:\s*·\s*auto mode:\s*(.*))?$/i)
  if (!match) return null
  const [, what, raw = ''] = match
  const call = what.match(/^(\w+)\((.+)\)$/)
  const tool = (call ? call[1] : what).trim()
  const noun = NOUNS[tool.toLowerCase()] ?? tool.replace(/^\w/, letter => letter.toUpperCase())
  const reason = (raw || facts?.reason || '').replace(/^auto mode:\s*/i, '').replace(/^\[(.*)\]$/, '$1').trim()
  const description = facts?.description?.trim()
  return { title: description ? `Auto mode blocked: ${description}` : `Auto mode blocked ${noun}`, reason: /^(no reason provided|blocked by classifier)\.?$/i.test(reason) ? '' : reason, command: facts?.command?.trim() || (call ? call[2].trim() : '') }
}
