import { useEffect, useRef, useState } from 'react'
import { Maximize2, SquareTerminal } from 'lucide-react'
import { copyText } from './clipboard'



































type TtydTerm = { cols: number; rows: number; options: { fontSize: number }; element?: HTMLElement; fit: () => void; onTitleChange: (listener: (title: string) => void) => unknown
  buffer?: { active: { viewportY: number; getLine: (row: number) => { isWrapped: boolean; translateToString: (trim: boolean) => string } | undefined } } }
const TERM_FONT = 13, TERM_MIN_FONT = 9

// Read this client's viewport. No terminal input, tmux setting, or shared pane changes.
function terminalText(frame: HTMLIFrameElement): string | null {
  const term = (frame.contentWindow as (Window & { term?: TtydTerm }) | null)?.term
  const buffer = term?.buffer?.active
  if (!term || !buffer) return null
  let text = ''
  for (let row = buffer.viewportY; row < buffer.viewportY + term.rows; row++) {
    const line = buffer.getLine(row)
    if (!line) break
    text += (row > buffer.viewportY && !line.isWrapped ? '\n' : '') + line.translateToString(true)
  }
  return text.replace(/\n+$/, '')
}

// The pane's own width arrives in the terminal title ("tether-pane 80x24", from pane_view on the server).
// Make the font smaller until the pane fits the phone. Below 9 px, keep 9 px and scroll sideways.
function fitPane(frame: HTMLIFrameElement, box: HTMLElement, setWidth: (width: number | null) => void, tries = 20) {
  const term = (frame.contentWindow as (Window & { term?: TtydTerm }) | null)?.term
  if (!term?.onTitleChange || !term.fit) {  // ttyd makes the terminal a moment after the page loads
    if (tries > 0 && frame.isConnected) setTimeout(() => fitPane(frame, box, setWidth, tries - 1), 250)
    return
  }
  let cols = 0
  const fit = () => {
    if (!cols) return
    term.options.fontSize = TERM_FONT
    term.fit()
    const inner = term.element?.clientWidth || frame.clientWidth
    const cell = inner / Math.max(1, term.cols) / TERM_FONT  // the width of one column for each px of font size
    const chrome = Math.max(0, frame.clientWidth - inner)
    const available = box.clientWidth - chrome
    const size = Math.floor(available / (cols * cell) * 2) / 2
    if (size >= TERM_MIN_FONT) {
      setWidth(null)
      term.options.fontSize = Math.min(TERM_FONT, size)
    } else {
      setWidth(Math.ceil(cols * cell * TERM_MIN_FONT + chrome + 4))
      term.options.fontSize = TERM_MIN_FONT
    }
    term.fit()
  }
  term.onTitleChange(title => {
    const match = /^tether-pane (\d+)x\d+$/.exec(title)
    if (match && Number(match[1]) !== cols) { cols = Number(match[1]); fit() }
  })
  new ResizeObserver(() => fit()).observe(box)
}

type TerminalView = 'pane' | 'window'
const VIEW_KEY = 'tether.terminalView', ZOOM_KEY = 'tether.terminalZoom'
function savedView(): TerminalView {
  try { return localStorage.getItem(VIEW_KEY) === 'window' ? 'window' : 'pane' } catch { return 'pane' }
}

// The live terminal (ttyd through the server). "This pane" shows only the pane; "Whole window" is the classic tmux view.
// shell: Tether's own side shell (TerminalPanel). Its window holds only this pane, so the view zooms it to fit and shows no view switch.
// fixed: the window keeps its own size (btop needs a minimum), so the view never resizes it and scales the font instead.
export function LiveTerminal({ paneId, onChat, shell = false, fixed = false }: { paneId: string; onChat: () => void; shell?: boolean; fixed?: boolean }) {
  const [status, setStatus] = useState<'checking' | 'ok' | 'down'>('checking')
  const [attempt, setAttempt] = useState(0)
  const [width, setWidth] = useState<number | null>(null)
  const [view, setViewState] = useState<TerminalView>(savedView)
  const setView = (next: TerminalView) => { setWidth(null); setViewState(next); try { localStorage.setItem(VIEW_KEY, next) } catch { /* the choice lasts for this visit only */ } }
  // Zoom: the tmux pane fills this view while it is open (prefix+z), and goes back when the view closes.
  const [zoom, setZoomState] = useState(() => { try { return localStorage.getItem(ZOOM_KEY) === '1' } catch { return false } })
  const setZoom = (next: boolean) => { setWidth(null); setZoomState(next); try { localStorage.setItem(ZOOM_KEY, next ? '1' : '0') } catch { /* this visit only */ } }
  const arg = fixed ? paneId : shell ? 'z' + paneId : view === 'window' ? 'w' + paneId : zoom ? 'z' + paneId : paneId
  const box = useRef<HTMLDivElement>(null)
  const frame = useRef<HTMLIFrameElement>(null)
  const textField = useRef<HTMLTextAreaElement>(null)
  const [selection, setSelection] = useState<string | null>(null)
  const [hasSelection, setHasSelection] = useState(false)
  const [copyStatus, setCopyStatus] = useState('')
  useEffect(() => { setSelection(null); setCopyStatus(''); setHasSelection(false) }, [arg, attempt])
  const selectText = () => {
    if (selection !== null) { setSelection(null); setCopyStatus(''); return }
    const text = frame.current ? terminalText(frame.current) : null
    if (text === null) { setCopyStatus('The terminal is still connecting. Try again.'); return }
    setSelection(text); setHasSelection(false); setCopyStatus('')
  }
  const copySelection = async () => {
    const field = textField.current
    if (!field) return
    const text = field.selectionEnd > field.selectionStart ? field.value.slice(field.selectionStart, field.selectionEnd) : field.value
    try { await copyText(text); setCopyStatus('Copied to clipboard.') }
    catch { setCopyStatus('Could not copy. Use the selection menu to copy the text.') }
  }
  useEffect(() => {
    let live = true
    setStatus('checking')
    fetch('/terminal/live/', { credentials: 'same-origin' }).then(response => { if (live) setStatus(response.ok ? 'ok' : 'down') }, () => { if (live) setStatus('down') })
    return () => { live = false }
  }, [attempt])
  if (status === 'ok') return <div className="live-terminal-wrap">
    <div className="terminal-bar">{!shell && <><label>View <select value={view} onChange={event => setView(event.target.value as TerminalView)}>
      <option value="pane">This pane</option><option value="window">Whole window</option></select></label>
      {view === 'pane' && <button type="button" className={zoom ? 'terminal-zoom active' : 'terminal-zoom'} aria-pressed={zoom} onClick={() => setZoom(!zoom)}
        title="Zoom the tmux pane to fill this view while it is open"><Maximize2 size={15} />Zoom</button>}</>}
      <button type="button" className={`terminal-zoom${selection !== null ? ' active' : ''}`} aria-pressed={selection !== null} onClick={selectText}>{selection !== null ? 'Back to terminal' : 'Select text'}</button>
      {selection !== null && <button type="button" className="terminal-zoom" onClick={() => void copySelection()}>{hasSelection ? 'Copy selection' : 'Copy all'}</button>}</div>
    {(selection !== null || copyStatus) && <p className="terminal-selection-hint" role="status">{copyStatus || 'Drag or long-press to select text. This copy stays still until you return to the terminal.'}</p>}
    <div ref={box} className={`live-terminal-box${selection !== null ? ' selecting' : ''}`}><iframe ref={frame} key={`${attempt}:${arg}`} className="live-terminal" title="Live terminal" style={width ? { width } : undefined} inert={selection !== null}
      src={`/terminal/live/?arg=${encodeURIComponent(arg)}`} onLoad={event => { if (box.current && (shell || view === 'pane')) fitPane(event.currentTarget, box.current, setWidth) }} />
      {selection !== null && <textarea ref={textField} className="terminal-select-text" aria-label="Terminal text" readOnly value={selection} wrap="off" spellCheck={false}
        onSelect={event => setHasSelection(event.currentTarget.selectionStart !== event.currentTarget.selectionEnd)}
        onPointerUp={event => { if (event.pointerType === 'mouse' && event.currentTarget.selectionStart !== event.currentTarget.selectionEnd) void copySelection() }} />}</div></div>
  return <div className="live-terminal-empty">{status === 'checking' ? <p role="status">Connecting to the terminal…</p> : <>
    <SquareTerminal size={28} strokeWidth={1.4} /><p><strong>The live terminal is not running.</strong></p><p>On the server, run <code>python3 ~/apps/mobile_tmux_public/deploy/install.py</code>.</p>
    <div>{!shell && <button type="button" className="secondary" onClick={onChat}>Back to chat</button>}<button type="button" className="primary" onClick={() => setAttempt(attempt + 1)}>Try again</button></div></>}</div>
}

// Claude draws a text block and writes it to the transcript at almost the same moment, so most previews last under a second.
// Show one only after it stays for PREVIEW_HOLD_MS: a slow transcript write still shows, a flash does not.
