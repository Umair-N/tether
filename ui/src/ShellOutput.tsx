import { useState } from 'react'
import { Copy, Check, RotateCcw } from 'lucide-react'
import { copyText } from './clipboard'
import { Linked } from './Messages'

/** Clear affects this view only. The terminal and its scrollback stay intact. */
export function ShellOutput({ text, folder }: { text: string; folder: string }) {
  const [cleared, setCleared] = useState('')
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')
  const visible = cleared && text.startsWith(cleared) ? text.slice(cleared.length).replace(/^\n/, '') : text
  async function copy() {
    try { await copyText(visible); setCopied(true); setError('') }
    catch { setError('Could not copy terminal output.') }
  }
  return <div className="shell-output">
    <div className="shell-output-bar">
      <span className="shell-output-folder" title={folder}>{folder || '~'}</span>
      <button type="button" disabled={!visible} onClick={() => void copy()} onBlur={() => setCopied(false)} aria-label="Copy terminal output">{copied ? <Check size={15} /> : <Copy size={15} />}{copied ? 'Copied' : 'Copy'}</button>
      <button type="button" disabled={!visible} title="Hide current output in Tether. Terminal history stays." onClick={() => { setCleared(text); setCopied(false) }}><RotateCcw size={15} />Clear view</button>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    <pre className="shell-output-text" tabIndex={0} aria-label="Terminal output"><code><Linked text={visible} /></code></pre>
    {!visible && <p className="shell-output-empty">{cleared ? 'View cleared. New output appears here.' : 'Your terminal is ready. Type a command below.'}</p>}
  </div>
}
