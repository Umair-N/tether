// A long paste becomes a badge above the message box instead of a wall of text in it.
// Same rule as Claude Code 2.1: more than 800 characters, or 4 lines or more.
export interface Paste { id: number; text: string; first: boolean }

export function longPaste(text: string) {
  const clean = text.replace(/\r\n?/g, '\n')
  return clean.length > 800 || clean.split('\n').length >= 4
}

export function pasteOf(text: string, first: boolean): Paste {
  return { id: Date.now() + Math.random(), text: text.replace(/\r\n?/g, '\n').replace(/\n+$/, ''), first }
}

// The text that goes to the agent. A paste made with the caret at the start of the box goes before the typed text.
export function joinPastes(body: string, pastes: Paste[]) {
  return [...pastes.filter(item => item.first).map(item => item.text), body, ...pastes.filter(item => !item.first).map(item => item.text)]
    .filter(part => part.trim()).join('\n\n')
}

// The box text after "Edit as text": the paste goes back where it would have been sent.
export function inlinePaste(draft: string, paste: Paste) {
  if (!draft.trim()) return paste.text
  return paste.first ? `${paste.text}\n\n${draft}` : `${draft.replace(/\s+$/, '')}\n\n${paste.text}`
}
