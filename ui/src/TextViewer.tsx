import { useEffect, useRef, useState } from 'react'
import { FolderOpen, X } from 'lucide-react'
import { useModal } from './hooks'
import { useHost } from './host'

export type TextFile = { path: string; url: string; line?: number }
const MAX_LINES = 20000

// Read-only view of a source or log file an agent named. It opens at the named line and marks it.
/** onShow: a "Show in Files" button (a chat on desktop). */
export function TextViewer({ file, close, onShow }: { file: TextFile | null; close: () => void; onShow?: () => void }) {
  const host = useHost()
  const modal = useModal(!!file, close)
  const [state, setState] = useState<{ url: string; lines?: string[]; error?: string; unreachable?: boolean } | null>(null)
  const body = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!file) return
    const abort = new AbortController()
    fetch(file.url, { signal: abort.signal })
      .then(async response => {
        if (!response.ok) throw new Error(((await response.json().catch(() => null)) as { error?: string } | null)?.error ?? 'Tether cannot open this file.')
        return response.text()
      })
      .then(text => setState({ url: file.url, lines: text.replace(/\n$/, '').split('\n') }),
        failure => { if (!abort.signal.aborted) setState({ url: file.url, ...(failure instanceof TypeError ? { unreachable: true } : { error: String(failure.message) }) }) })
    return () => abort.abort()
  }, [file])
  const current = file && state?.url === file.url ? state : null
  const lines = current?.lines ?? []
  const target = file?.line && file.line <= lines.length ? file.line : undefined
  // A huge file shows a window of lines around the target.
  const first = lines.length > MAX_LINES ? Math.max(0, Math.min((target ?? 1) - 1 - MAX_LINES / 2, lines.length - MAX_LINES)) : 0
  const shown = lines.slice(first, first + MAX_LINES)
  useEffect(() => {
    if (!current?.lines) return
    const row = target ? body.current?.querySelector(`[data-line="${target}"]`) : null
    if (row) row.scrollIntoView({ block: 'center' })
    else body.current?.scrollTo(0, 0)
  }, [current, target])
  const name = file?.path.split('/').pop() || file?.path
  return <dialog {...modal} className="text-viewer" aria-labelledby="text-viewer-title">{file && <>
    <div className="text-viewer-head">
      <div className="text-viewer-title"><h2 id="text-viewer-title">{name}{target ? <span>:{target}</span> : null}</h2><p title={file.path}>{file.path}</p></div>
      {onShow && <button type="button" className="viewer-files" onClick={onShow}><FolderOpen size={15} aria-hidden="true" />Show in Files</button>}
      <button type="button" className="icon-button" aria-label="Close" onClick={close}><X /></button>
    </div>
    <div className="text-viewer-body" ref={body} tabIndex={0} aria-label={`Contents of ${name}`}>
      {current?.error || current?.unreachable ? <p className="text-viewer-note" role="alert">{current.unreachable ? `Cannot reach ${host}.` : current.error}</p>
        : !current ? <p className="text-viewer-note">Loading…</p>
        : <pre style={{ '--gutter': `${String(first + shown.length).length + 1}ch` } as React.CSSProperties}>{first > 0 && <span className="text-viewer-skip">{first} lines above not shown</span>}
          {shown.map((text, at) => <span key={at} data-line={first + at + 1} className={first + at + 1 === target ? 'marked' : undefined}>{text || ' '}</span>)}
          {first + shown.length < lines.length && <span className="text-viewer-skip">{lines.length - first - shown.length} lines below not shown</span>}</pre>}
    </div>
  </>}</dialog>
}
