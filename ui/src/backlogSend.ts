// Hands a backlog item to an agent: the Backlog sheet's Send panel and Friday's backlog_send use these.
import type { Api, ChatData, Pane } from './api'
import type { Item } from './backlogItems'

/** What a chat gets with an item: the item, its details, and how to close it. */
export function itemPrompt(item: Item, command?: string) {
  return [
    `Backlog item ${item.ref}: ${item.text}`,
    ...item.details.map(detail => `- ${detail.text}`),
    '',
    command ? `When it is finished, run \`${command} done ${item.ref} "what you did: the commit, PR or outcome"\`.`
      : `When it is finished, mark ${item.ref} done in the backlog with what you did.`,
  ].join('\n')
}

/** Send a message to a tab's current chat, as Friday does: the server refuses a chat that changed. */
export async function sendToTab(api: Api, pane: Pane, message: string) {
  const before = await api<ChatData>(`chat?session=${encodeURIComponent(pane.id)}&limit=4`).catch(() => null)
  const thread = before?.thread ?? pane.thread ?? null
  await api('message', { session: pane.id, thread, message })
  return thread
}

/** Start a new tab beside a chat, in its folder, with the item as its task. */
export async function startTab(api: Api, beside: Pane, message: string) {
  const made = await api<{ pane: string; name: string }>('delegate', { session: beside.id, thread: beside.thread, agent: beside.agent, message, context: false, review: false })
  return made
}

export const agentTabs = (panes: Pane[]) => panes.filter(pane => (pane.agent === 'claude' || pane.agent === 'codex') && pane.writable)
