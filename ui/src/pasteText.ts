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
// Stop: Claude puts a stopped message back in its box, but a long one shows as "[Pasted text #12 +3 lines]".
// Swap each placeholder, in order, for the latest long messages Tether sent. Unknown ones stay as they are.
const PLACEHOLDER = /\[Pasted text #\d+(?: \+\d+ lines?)?\]/g
export function expandPlaceholders(restored: string, sent: string[]) {
  const count = restored.match(PLACEHOLDER)?.length ?? 0
  if (!count) return restored
  const long = sent.filter((text, at) => longPaste(text) && sent.lastIndexOf(text) === at)
  const texts = long.slice(-count)
  const offset = count - texts.length
  let at = 0
  return restored.replace(PLACEHOLDER, match => { const text = at >= offset ? texts[at - offset] : match; at++; return text })
}
