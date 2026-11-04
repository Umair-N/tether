export type Agent = 'claude' | 'codex' | 'antigravity' | 'shell'
export interface Pane {
  id: string
  tmux_session: string
  label: string
  agent: Agent
  dir: string
  state: string
  writable: boolean
  thread: string | null
  approval: unknown
  pane_name?: string
  pane_icon?: string  // tmux option @chat_icon: a lucide icon name, or svg-<name> for an icon Claude drew (Sidebar.tsx AgentIcon)
  agent_name?: string  // the Claude or Codex session name (/rename); shown when the pane has no Tether name
  codex_profile?: string | null  // a Codex pane started with `codex -p foundry`: 'foundry'. null = the ChatGPT plan
  since?: number | null
  finished?: number | null
  tasks?: string[]  // background work from Claude's footer: "1 shell", "1 monitor"
  attention?: Attention | null
  needs?: Needs | null  // a one-line summary while the pane waits on you (the desktop "Needs you" list)
}
export interface Needs { kind: 'approval' | 'plan' | 'question'; text: string }
// images: how many images the tool result holds (Claude's Read of a screenshot). GET tool-image serves them.
export interface ToolSummary { id: string; name: string; title: string; is_error: boolean; status: 'pending' | 'complete'; images?: number }
export interface ToolDetail extends ToolSummary { detail: string; format: 'text' | 'diff'; truncated: boolean }
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  kind?: 'actions' | 'terminal' | 'notice' | 'peer'
  title?: string
  tools?: ToolSummary[]
  count?: string
  time: string
}
export interface ChatData {
  login_result?: { phase: 'done' | 'failed' | 'unknown'; message: string }
  preview?: { text: string }
  // A `!` shell command that is still running in Claude Code, read from the screen (the transcript gets it only at the end).
  bang?: { command: string; output: string; seconds: number | null }
  messages: ChatMessage[]
  busy: boolean
  thread: string | null
  agent: Agent
  writable: boolean
  session: string
  earlier: number
  limit: number
  state: string
  approval: unknown
  plan?: { id: string; text: string } | null
  claude_name?: string | null  // Claude's own session name (/rename); null when it has none
  attention?: Attention | null
  footer?: Footer
}
// free: Claude's own-answer row ("Type something."). The answer goes in the message box, not a tap.
export interface Choice { id: number; text: string; detail?: string; selected?: boolean; free?: boolean }
export interface Approval { id: string | null; text: string; actionable: boolean; options?: Choice[] }
export interface AskQuestion { question: string; header: string; multi: boolean; options: { label: string; description: string }[] }
/** ask: Claude's open question from the transcript, when the pane is too short to show all of it. */
export interface PickerItem { text: string; selected: boolean }
/** A key that a read-only panel's own footer offers. The server keeps the real key; the browser sends only the id. */
export interface PromptAction { id: string; label: string; key: string; enabled: boolean; disabledReason?: string }
/** Visible read state only. IDs are frame-local; focus indexes items. -1 means an unknown overflow count. */
export interface ChoiceState {
  selection: 'single' | 'multi'
  items: { id: number; text: string; detail?: string; selected: boolean; checked?: boolean; disabled?: boolean }[]
  focus: number | null
  filter: string | null
  tabs: { label: string; active: boolean }[] | null
  more_above: number
  more_below: number
  hints: string[]
}
export interface Attention { id: string; text: string; reason: 'menu' | 'prompt' | 'no-input-box' | 'panel' | 'picker' | 'info';
  promptId?: string; revision?: string; requiresResponse?: boolean; body?: string; hints?: string[]; actions?: PromptAction[]; title?: string; subtitle?: string; items?: PickerItem[]; more_above?: number; more_below?: number; hint?: string; question?: string; options?: Choice[]; choice?: ChoiceState; ask?: { id: string; questions: AskQuestion[] }; login?: { url: string; code: boolean; user_code?: string; message?: string } }
export interface PastChat { id: string; title: string; dir: string | null; time: string; live: boolean }
export interface HistoryData { chats: PastChat[] }
export interface UsageWindow { label: string; percent: number; resets_at: number | null }
export interface UsageReport { windows: UsageWindow[]; reported_at: string | number | null; source?: 'live' }
export interface SavedAccount { number: number; email: string | null; org?: string | null; plan?: string | null; active: boolean; disabled?: boolean; windows: { label: string; percent: number; resets_at: string | null }[] }
export interface SavedAccounts { available: boolean; claude: SavedAccount[]; codex: SavedAccount[] }
// The provider for new Codex panes. ready: Foundry's profile and key command look set up on the server; hint says what is missing.
type SpendTokens = Record<'input' | 'cached' | 'cache_write' | 'output' | 'requests', number>
type SpendWindow = { short: SpendTokens; long: SpendTokens }
export interface FoundrySpend {
  month: string; budget: number
  estimate: { tokens: SpendWindow; today_tokens: SpendWindow; sessions: number; usd: number | null; today_usd: number | null }
  bill: { state: 'ok'; usd: number; currency: string; checked: number } | { state: 'not-set-up' } | { state: 'error'; hint: string }
}
export interface CodexProvider { provider: 'plan' | 'foundry'; profile: string; model: string | null; ready: boolean; hint: string | null; everywhere?: boolean }
export interface UsageAccount { email: string | null; plan: string | null }
export interface UsageData { claude: UsageReport | null; codex: UsageReport | null; accounts?: { claude: UsageAccount | null; codex: UsageAccount | null }; live_errors?: { claude?: string; codex?: string } }
export interface SessionsData { sessions: Pane[]; default_thread: string | null }
export class ApiError extends Error {
  status: number
  draft: boolean  // the agent's own input box has text that blocks this action
  constructor(status: number, message: string, draft = false) { super(message); this.status = status; this.draft = draft }
}
/** timeout: milliseconds before the request gives up (10 s). A broadcast types into many panes one after another, so it waits longer. */
export type Api = <T>(path: string, body?: unknown, signal?: AbortSignal, method?: 'DELETE', timeout?: number) => Promise<T>
export async function request<T>(path: string, body?: unknown, signal?: AbortSignal, method?: 'DELETE', timeout = 10000): Promise<T> {
  const response = await fetch(`/mobile/api/${path}`, {
    method: method ?? (body === undefined ? 'GET' : 'POST'),
    credentials: 'same-origin',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(timeout)]) : AbortSignal.timeout(timeout),
  })
  const data = await response.json().catch(error => {
    if (response.ok && path.split('?')[0] === 'footer') throw error
    return {}
  })
  if (!response.ok) throw new ApiError(response.status, data.error || `Request failed (${response.status}). Please try again.`, data.draft === true)
  return data as T
}
/** tmux session names: spaces are fine; tmux turns . and : into _, so they are refused. Same rule as the server. */
export const SESSION_NAME = /^[A-Za-z0-9](?:[A-Za-z0-9 _-]{0,62}[A-Za-z0-9_-])?$/
export const SESSION_NAME_HELP = 'Use 1–64 letters, numbers, spaces, hyphens, or underscores. Start with a letter or number.'

/** A chat the agent can copy with its history: Claude or Codex, after its first message. */
export const canFork = (pane: Pane) => (pane.agent === 'claude' || pane.agent === 'codex') && !!pane.thread

export function errorText(error: unknown) {
  return error instanceof Error ? error.message : 'Could not connect. Please try again.'
}
export interface CatalogItem { name: string; description: string; kind: 'builtin' | 'skill' | 'command'; aliases?: string[] }
export interface Catalog { commands: CatalogItem[]; plugins: { name: string; source: string; enabled: boolean }[]; mcp: { name: string; source: string }[]; note: string }

export interface FooterAgent { name: string; task: string; detail: string; main: boolean }
export interface FooterTask { id: string; type?: string; status?: string; description?: string; command?: string; started?: number }
export const usageLabel = (label: string) => ({ '5h': '5-hour', '168h': 'Weekly', '7d': 'Weekly' })[label] ?? label
export const creditsLabel = (credits?: Footer['credits']) => credits?.unlimited ? 'Unlimited' : credits?.balance != null ? String(credits.balance) : ''
// The strip under Claude's input box: its suggested next prompt, notices, mode, status line and background agents.
export interface Footer { observed?: boolean; suggestion?: string; notices?: string[]; effort?: string; mode?: string; status?: string; agents?: FooterAgent[]; update?: string
  model?: string; context?: { percent: number; tokens?: number | null; limit?: number | null }; windows?: UsageWindow[]
  credits?: { balance?: string | number | null; unlimited?: boolean; has_credits?: boolean }
  goal?: string; tasks?: string[]; task_details?: FooterTask[]; ide?: string
  spinner?: { verb: string; detail: string } | null
  running?: { title: string; elapsed: string; command: string } | null
  denied?: { text: string; reason: string } | null
  heads_up?: { text: string; state?: 'offer' | 'explained' | 'making' | 'ready' | 'failed'; detail?: string[]; url?: string | null; more?: number; full?: string; note?: string; options: { key: string; label: string }[] } | null }
// The /goal and the background shells and monitors of the pane's Claude conversation, from its transcript.
export interface LiveGoal { condition: string; met: boolean; reason?: string; set_at?: number | null; checked_at?: number | null }
export interface LiveTask { id: string; kind: 'shell' | 'monitor'; title: string; command: string; started: number | null; output: string | null; state: 'running' | 'ended' }
export interface LiveState { goal: LiveGoal | null; tasks: LiveTask[] }
export interface ModelOptions { agent: string; models: { id: string; label: string; current: boolean; effort?: boolean }[]; efforts: string[]; current_effort: string | null }

/** One pane's outcome of a broadcast (server/broadcast.py). sent_busy: the agent was working and took the typed text;
 * Tether keeps no queue. handed_off: in the Claude master's target list. */
export interface BroadcastRow { pane: string; name: string; agent: Agent | null; result: 'sent' | 'sent_busy' | 'skipped' | 'failed' | 'handed_off'; reason?: string }
export interface BroadcastResult { via: 'direct' | 'claude'; results: BroadcastRow[]; master?: BroadcastRow }
