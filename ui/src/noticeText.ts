// Claude Code's footer line when auto mode's safety check stops a tool call:
// "bash blocked by auto mode · /permissions · Auto mode: No reason provided"
// "Bash(git push --force) blocked by auto mode · /permissions · Auto mode: [Data Exfiltration]"
export interface AutoBlock { title: string; reason: string }
export function autoBlock(note: string): AutoBlock | null {
  const match = note.match(/^(.+?) blocked by auto mode(?:\s*·\s*\/permissions)?(?:\s*·\s*auto mode:\s*(.*))?$/i)
  if (!match) return null
  const [, what, raw = ''] = match
  const call = what.match(/^(\w+)\((.+)\)$/)
  const tool = (call ? call[1] : what).trim()
  const name = /^bash$/i.test(tool) ? 'Bash' : tool.replace(/^\w/, letter => letter.toUpperCase())
  const reason = raw.replace(/^\[(.*)\]$/, '$1').trim()
  return { title: call ? `Auto mode blocked ${call[2].trim()}` : `Auto mode blocked ${name}`, reason: /^no reason provided\.?$/i.test(reason) ? '' : reason }
}
