/** Plain text for terminal output and pasted logs. Unknown controls stay for input validation. */
export function terminalText(text: string): string {
  // Strip complete CSI, OSC (including hyperlinks), and terminal control strings, not their visible labels.
  // eslint-disable-next-line no-control-regex
  return text.replace(/(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)|(?:\x1b[P^_X]|[\x90\x98\x9e\x9f])[^\x1b\x9c]*(?:\x1b\\|\x9c)|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\r\n?/g, '\n')
}
