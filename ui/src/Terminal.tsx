import { useEffect, useRef, useState } from 'react'
import { Maximize2, SquareTerminal } from 'lucide-react'



































type TtydTerm = { cols: number; options: { fontSize: number }; element?: HTMLElement; fit: () => void; onTitleChange: (listener: (title: string) => void) => unknown }
const TERM_FONT = 13, TERM_MIN_FONT = 9

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
  useEffect(() => {
    let live = true
    setStatus('checking')
    fetch('/terminal/live/', { credentials: 'same-origin' }).then(response => { if (live) setStatus(response.ok ? 'ok' : 'down') }, () => { if (live) setStatus('down') })
    return () => { live = false }
  }, [attempt])
  if (status === 'ok') return <div className="live-terminal-wrap">
    {!shell && <div className="terminal-bar"><label>View <select value={view} onChange={event => setView(event.target.value as TerminalView)}>
      <option value="pane">This pane</option><option value="window">Whole window</option></select></label>
      {view === 'pane' && <button type="button" className={zoom ? 'terminal-zoom active' : 'terminal-zoom'} aria-pressed={zoom} onClick={() => setZoom(!zoom)}
        title="Zoom the tmux pane to fill this view while it is open"><Maximize2 size={15} />Zoom</button>}</div>}
    <div ref={box} className="live-terminal-box"><iframe key={`${attempt}:${arg}`} className="live-terminal" title="Live terminal" style={width ? { width } : undefined}
      src={`/terminal/live/?arg=${encodeURIComponent(arg)}`} onLoad={event => { if (box.current && (shell || view === 'pane')) fitPane(event.currentTarget, box.current, setWidth) }} /></div></div>
  return <div className="live-terminal-empty">{status === 'checking' ? <p role="status">Connecting to the terminal…</p> : <>
    <SquareTerminal size={28} strokeWidth={1.4} /><p><strong>The live terminal is not running.</strong></p><p>On the server, run <code>python3 ~/apps/mobile_tmux_public/deploy/install.py</code>.</p>
    <div>{!shell && <button type="button" className="secondary" onClick={onChat}>Back to chat</button>}<button type="button" className="primary" onClick={() => setAttempt(attempt + 1)}>Try again</button></div></>}</div>
}

// Claude draws a text block and writes it to the transcript at almost the same moment, so most previews last under a second.
// Show one only after it stays for PREVIEW_HOLD_MS: a slow transcript write still shows, a flash does not.
