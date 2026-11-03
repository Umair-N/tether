import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Blocks, Clock3, Layers, Check, Copy, Share2, ChevronRight, X, TextSelect, MessageSquare, Info, ShieldAlert, Square, Volume2 } from 'lucide-react'
import { MessagePrimitive } from '@assistant-ui/react'
import type { ThreadMessageLike } from '@assistant-ui/react'
import { MarkdownTextPrimitive } from '@assistant-ui/react-markdown'
import remarkGfm from 'remark-gfm'
import type { Api, ChatMessage } from './api'
import { useModal } from './hooks'
import { ToolGroup } from './ToolGroup'
import { InlineCode, Link, remarkPaths } from './MediaLink'
import { canSpeak, speak, stopSpeaking, useSpeaking } from './speech'
import type { ToolSummary } from './api'















const URL_PATTERN = /https?:\/\/[^\s<>"'`]+/g
export function Linked({ text }: { text: string }) {
  const parts: React.ReactNode[] = []
  let last = 0
  for (const match of text.matchAll(URL_PATTERN)) {
    const url = match[0].replace(/[).,;:!?\]}]+$/, ''), at = match.index ?? 0
    parts.push(text.slice(last, at), <a key={at} href={url} target="_blank" rel="noreferrer">{url}</a>)
    last = at + url.length
  }
  parts.push(text.slice(last))
  return <>{parts}</>
}
// A terminal panel wraps on a phone. Each line keeps its indent as a hanging indent, so wrapped parts line up under their line.
export function PanelText({ text }: { text: string }) {
  return <>{text.split('\n').map((line, index) => {
    const indent = line.length - line.trimStart().length
    return <div key={index} className="panel-line" style={{ paddingLeft: `${Math.min(indent, 12) + 2}ch` }}>{line.trim() ? <Linked text={line.trim()} /> : '\u00a0'}</div>
  })}</>
}
// A read-only terminal panel (Claude's /usage): keep its columns and bars, scroll sideways on a narrow phone.
export function PanelPre({ text }: { text: string }) {
  // The tab row and "Esc to cancel" repeat the card's own title and Close button. Bars shrink to 40% so the numbers fit a phone.
  const lines = text.split('\n').filter(line => !/^\s*Settings\s+Status\s+Config\b|^\s*Esc to (?:cancel|close)\s*$/.test(line))
    .map(line => line.replace(/[█▉▊▋▌▍▎▏]+/g, bar => '█'.repeat(Math.max(1, Math.round(bar.length * .4)))))
  const indent = Math.min(...lines.filter(line => line.trim()).map(line => line.length - line.trimStart().length))
  return <pre className="panel-pre" tabIndex={0} aria-label="Panel text">{lines.map(line => line.slice(indent).replace(/(\S) {4,}/g, '$1   ')).join('\n').trim()}</pre>
}
// A "!" command's result that Tether ran itself (shell_log.py): "~/dir $ cmd", the output, then "[exit 2]", "[stopped after 120 s]" or "→ ~/new".
// Only these carry a prompt and an exit status; an agent's own "!" output is the output alone.
function shellParts(text: string) {
  const lines = text.split('\n')
  const prompt = /^(\S.*?) \$ (.*)$/.exec(lines[0] ?? '')
  if (!prompt) return null
  let end = lines.length, exit = 0, stopped = '', moved = ''
  for (;;) {
    const line = lines[end - 1] ?? ''
    if (end > 1 && line.startsWith('→ ')) { moved = line.slice(2); end-- }
    else if (end > 1 && /^\[exit \d+\]$/.test(line)) { exit = Number(line.slice(6, -1)); end-- }
    else if (end > 1 && /^\[stopped after \d+ s\]$/.test(line)) { stopped = line.slice(1, -1); end-- }
    else break
  }
  return { folder: prompt[1], command: prompt[2], output: lines.slice(1, end).join('\n'), exit, stopped, moved }
}
function TerminalSnapshot({ text, shell }: { text: string; shell: boolean }) {
  const ref = useRef<HTMLPreElement>(null)
  useLayoutEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight }, [text])
  const parts = shell ? shellParts(text) : null
  if (parts) return <div className="terminal-block shell-run">
    <div className="shell-run-head"><code className="shell-run-prompt"><span className="shell-folder">{parts.folder}</span> $ {parts.command}</code>
      <span className={`exit-chip ${parts.stopped || parts.exit ? 'failed' : 'ok'}`}>{parts.stopped ? 'Stopped' : parts.exit ? `Exit ${parts.exit}` : 'Exit 0'}</span></div>
    {parts.output && <pre ref={ref} className="terminal-snapshot" tabIndex={0} aria-label="Command output"><code><Linked text={parts.output} /></code></pre>}
    {(parts.moved || parts.stopped) && <p className="shell-run-note">{parts.stopped ? `${parts.stopped[0].toUpperCase()}${parts.stopped.slice(1)}. ` : ''}{parts.moved && `Now in ${parts.moved}`}</p>}
    <CopyButton text={parts.output || text} label="Copy command output" className="copy-chip" />
  </div>
  return <div className="terminal-block">
    <pre ref={ref} className="terminal-snapshot" tabIndex={0} aria-label="Terminal output"><code><Linked text={text} /></code></pre>
    <CopyButton text={text} label="Copy terminal output" className="copy-chip" />
  </div>
}
export function Message({ id, role, kind, title, text, count, time, tools, api, session, actions = true }: { actions?: boolean; id: string; role: string; kind?: unknown; title?: string; text: string; count?: unknown; time?: string; tools?: ToolSummary[]; api: Api; session: string }) {
  // A line Claude Code prints between turns: a hook that blocked it, a "while you were away" recap, a notice.
  if (kind === 'notice' && title) {
    const icon = title.endsWith('hook blocked') ? <ShieldAlert size={15} /> : title === 'While you were away' ? <Clock3 size={15} /> : <Info size={15} />
    if (text.includes('\n') || text.length > 240) return <details className="tool-row compact-row notice-row" data-msg-id={id}><summary>{icon}{title}<ChevronRight size={15} /></summary><pre><Linked text={text} /></pre></details>
    return <p className="notice-line" data-msg-id={id}>{icon}<span><strong>{title}</strong> · <Linked text={text} /></span></p>
  }
  // A message from another Claude session, or a subagent's final report: Claude's input, but not the user's words.
  if (kind === 'peer' && title) {
    const report = title === 'Subagent report'
    return <details className="peer-message" data-msg-id={id} open={!report}>
      <summary>{report ? <Blocks size={15} /> : <MessageSquare size={15} />}<strong>{title}</strong>{report && <span className="peer-preview">{text.split('\n').find(line => line.trim())?.replace(/^#+\s*/, '')}</span>}<ChevronRight size={15} /></summary>
      <LongPressMenu text={text}>{press => <MessagePrimitive.Root className="assistant-message" {...press}><MessagePrimitive.Parts components={{ Text: Markdown }} /><MessageActions text={text} time={time} /></MessagePrimitive.Root>}</LongPressMenu>
    </details>
  }
  if (kind === 'actions') {
    return <ToolGroup id={id} text={text} count={count} tools={tools} api={api} session={session} />
  }
  if (kind === 'terminal') return <div data-msg-id={id}><TerminalSnapshot text={text} shell={id.startsWith('shell:')} /></div>
  // A "!" line the person sent: the agent ran it in its terminal. Show it as the prompt line of the output below it.
  const command = role === 'user' && !kind ? /^!\s*(\S[\s\S]*)$/.exec(text) : null
  if (command) return <div className="shell-command" data-msg-id={id} role="group" aria-label="Shell command"><span className="shell-sigil" aria-hidden="true">$</span><code>{command[1]}</code></div>
  // Claude puts the whole summary in a user message after /compact or an auto-compact. Fold it like a tool row.
  if (role === 'user' && text.startsWith('This session is being continued from a previous conversation')) {
    return <details className="tool-row compact-row" data-msg-id={id}><summary><Layers size={15} />Conversation compacted<ChevronRight size={15} /></summary><pre>{text}</pre></details>
  }
  return <LongPressMenu text={text}>{press => <MessagePrimitive.Root className={role === 'user' ? 'user-message' : 'assistant-message'} data-msg-id={id} {...press}>
    {role === 'user' ? <MessagePrimitive.Parts /> : <><MessagePrimitive.Parts components={{ Text: Markdown }} />{actions && <MessageActions text={text} time={time} />}</>}
  </MessagePrimitive.Root>}</LongPressMenu>
}
// Long-press a message: Copy, Share or Select text (touch screens select nothing by themselves, so a scroll never starts a selection).
type PressHandlers = Pick<React.HTMLAttributes<HTMLElement>, 'onTouchStart' | 'onTouchMove' | 'onTouchEnd' | 'onTouchCancel' | 'onContextMenu'>
function LongPressMenu({ text, children }: { text: string; children: (handlers: PressHandlers) => React.ReactNode }) {
  const [open, setOpen] = useState<'' | 'menu' | 'select'>('')
  const timer = useRef(0)
  const start = useRef<{ x: number; y: number } | null>(null)
  const touched = useRef(0)
  const cancel = () => { clearTimeout(timer.current); start.current = null }
  const handlers: PressHandlers = {
    onTouchStart: event => {
      touched.current = Date.now()
      if (event.touches.length !== 1 || (event.target as HTMLElement).closest('button, a, summary, dialog, pre, code')) return
      start.current = { x: event.touches[0].clientX, y: event.touches[0].clientY }
      timer.current = window.setTimeout(() => { start.current = null; navigator.vibrate?.(8); setOpen('menu') }, 480)
    },
    onTouchMove: event => { const at = start.current; if (at && Math.hypot(event.touches[0].clientX - at.x, event.touches[0].clientY - at.y) > 10) cancel() },
    onTouchEnd: cancel, onTouchCancel: cancel,
    onContextMenu: event => { if (Date.now() - touched.current < 1500) event.preventDefault() },  // keep the right-click menu on a desktop
  }
  useEffect(() => () => clearTimeout(timer.current), [])
  return <>{children(handlers)}{open && <MessageMenu text={text} mode={open} onSelect={() => setOpen('select')} onClose={() => setOpen('')} />}</>
}
// Select text shows what the message reads as, not its Markdown marks: no **, backticks or # heading marks.
const readableText = (text: string) => text
  .replace(/^```[^\n]*\n?/gm, '')
  .replace(/^#{1,6}\s+/gm, '')
  .replace(/\*\*(.+?)\*\*/g, '$1').replace(/__(.+?)__/g, '$1')
  .replace(/`([^`\n]+)`/g, '$1')
  .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '$1 ($2)')
function MessageMenu({ text, mode, onSelect, onClose }: { text: string; mode: 'menu' | 'select'; onSelect: () => void; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const [note, setNote] = useState('')
  return <dialog {...modal} className="sheet message-menu" aria-label={mode === 'select' ? 'Select text' : 'Message options'}>
    {mode === 'select'
      ? <><div className="sheet-heading"><h2>Select text</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div><div className="select-text" tabIndex={0}>{readableText(text)}</div></>
      : <div className="action-list">
          <button type="button" onClick={() => navigator.clipboard.writeText(text).then(onClose, () => setNote('Could not copy. Use Select text.'))}><Copy size={18} />Copy</button>
          {typeof navigator.share === 'function' && <button type="button" onClick={() => { navigator.share({ text }).catch(() => {}); onClose() }}><Share2 size={18} />Share</button>}
          <button type="button" onClick={onSelect}><TextSelect size={18} />Select text</button>
          {note && <p className="error" role="alert">{note}</p>}
        </div>}
  </dialog>
}
// Copy with a short "Copied" confirmation. The Clipboard API needs HTTPS, which Tailscale Serve gives us.
export function CopyButton({ text, label, className = 'icon-button' }: { text: string; label: string; className?: string }) {
  const [done, setDone] = useState<'' | 'ok' | 'fail'>('')
  useEffect(() => { if (!done) return; const timer = setTimeout(() => setDone(''), 1500); return () => clearTimeout(timer) }, [done])
  return <button type="button" className={className} aria-label={done === 'ok' ? 'Copied' : label} title={label}
    onClick={() => navigator.clipboard.writeText(text).then(() => setDone('ok'), () => setDone('fail'))}>
    {done === 'ok' ? <Check size={16} /> : <Copy size={16} />}{className !== 'icon-button' && <span>{done === 'ok' ? 'Copied' : done === 'fail' ? 'Failed' : 'Copy'}</span>}
  </button>
}
// Time of the reply, as the CLI shows it: "9:36 PM" today, "Fri 9:36 PM" this week, else the date.
function stamp(value?: string) {
  const at = value ? new Date(value) : null
  if (!at || Number.isNaN(at.getTime())) return ''
  const days = (Date.now() - at.getTime()) / 86400000
  const clock = at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  if (at.toDateString() === new Date().toDateString()) return clock
  return days < 6 ? `${at.toLocaleDateString(undefined, { weekday: 'short' })} ${clock}` : `${at.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })} ${clock}`
}
function MessageActions({ text, time }: { text: string; time?: string }) {
  const canShare = typeof navigator.share === 'function'
  const when = stamp(time)
  return <div className="message-actions">
    <CopyButton text={text} label="Copy message" />
    <SpeakButton text={text} />
    {canShare && <button type="button" className="icon-button" aria-label="Share message" title="Share message" onClick={() => navigator.share({ text }).catch(() => {})}><Share2 size={16} /></button>}
    {when && <time className="message-time" dateTime={time}>{when}</time>}
  </div>
}
function SpeakButton({ text }: { text: string }) {
  // The text is the key, so the button also shows a reply that is read aloud by itself at the end of a turn.
  const reading = useSpeaking() === text
  if (!canSpeak) return null
  return <button type="button" className="icon-button" aria-label={reading ? 'Stop reading' : 'Read aloud'} title={reading ? 'Stop reading' : 'Read aloud'} aria-pressed={reading} onClick={() => reading ? stopSpeaking() : speak(text, text)}>{reading ? <Square size={14} /> : <Volume2 size={16} />}</button>
}
function CodeHeader({ language, code }: { language: string | undefined; code: string }) {
  return <div className="code-header"><span>{language || 'code'}</span><CopyButton text={code} label="Copy code" className="code-copy" /></div>
}
export function Markdown() { return <MarkdownTextPrimitive remarkPlugins={[remarkGfm, remarkPaths]} smooth={false} components={{ CodeHeader, code: InlineCode, a: Link }} /> }
export const convertMessage = (message: ChatMessage): ThreadMessageLike => ({
  id: message.id, role: message.role, content: message.text,
  metadata: { custom: { kind: message.kind, title: message.title, count: message.count, tools: message.tools, text: message.text, time: message.time } },
})

// Phone photos are large and carry location data: re-draw JPEG/HEIC photos (this drops the metadata) and shrink big images.
