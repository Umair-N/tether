// Sent prompts, newest last, kept on this device per pane. Up and Down in the composer walk them, as in the Claude CLI.
const LIMIT = 200

function key(kind: string) { return `tether-history-${kind}` }

export function loadHistory(kind: string): string[] {
  try { const list = JSON.parse(localStorage.getItem(key(kind)) ?? '[]'); return Array.isArray(list) ? list.filter(item => typeof item === 'string') : [] } catch { return [] }
}

export function remember(kind: string, text: string) {
  const list = loadHistory(kind).filter(item => item !== text)
  list.push(text)
  try { localStorage.setItem(key(kind), JSON.stringify(list.slice(-LIMIT))) } catch { /* private mode */ }
}

// One list, oldest first, without repeats: the chat's prompts, then local ones. A repeat keeps its newest place.
export function history(chat: string[], local: string[]): string[] {
  const all = [...chat, ...local].map(item => item.trim()).filter(Boolean)
  return all.filter((item, at) => all.lastIndexOf(item) === at)
}
