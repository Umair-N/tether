import { terminalText } from './terminalText'

/** Decode the shell envelope produced by server.display_text, preserving literal command/output text. */
export function shellCommand(text: string): { command: string; output: string; exit: number } | null {
  const inline = /^! (`+) ([^\n]*) \1(?:\n\n([\s\S]*))?$/.exec(text)
  const block = /^!\n\n(`{3,})sh\n([\s\S]*?)\n\1(?:\n\n([\s\S]*))?$/.exec(text)
  const match = inline ?? block
  if (!match) return null
  const result = match[3] ?? ''
  const output = /^(`{3,})text\n([\s\S]*?)\n\1(?:\n\nExit code: (-?\d+))?$/.exec(result)
  const exit = /^Exit code: (-?\d+)$/.exec(result)
  if (result && !output && !exit) return null
  return { command: match[2], output: terminalText(output?.[2] ?? ''), exit: Number(output?.[3] ?? exit?.[1] ?? 0) }
}
