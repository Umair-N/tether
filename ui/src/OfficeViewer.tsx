import { useEffect, useMemo, useRef, useState } from 'react'
import { Download, FolderOpen, X } from 'lucide-react'
import type { WorkBook } from 'xlsx'
import { useModal } from './hooks'
import { useHost } from './host'

export type OfficeFile = { path: string; url: string }
/** Office files Tether shows in its own viewer: spreadsheets, Word documents and slide decks. */
export const OFFICE_SUFFIXES = ['xlsx', 'xlsm', 'xls', 'ods', 'docx', 'pptx']
const suffixOf = (path: string) => path.split('.').pop()?.toLowerCase() ?? ''
export const isOffice = (path: string) => OFFICE_SUFFIXES.includes(suffixOf(path))

// The file is read in the browser: the libraries load only when a file of their type opens, and nothing leaves Tether.
// External images and links in a file stay off, so opening a file never calls another site.
const MAX_BYTES = 50 * 1024 * 1024
const MAX_ROWS = 2000
const MAX_COLUMNS = 200
const UNREADABLE = 'Tether cannot read this file. Download it to open it in another app.'

type Loaded = { url: string; error?: string; unreachable?: boolean; sheet?: WorkBook; data?: ArrayBuffer }

/** Read-only view of a spreadsheet, a Word document or a slide deck. onShow: a "Show in Files" button. */
export function OfficeViewer({ file, close, onShow }: { file: OfficeFile | null; close: () => void; onShow?: () => void }) {
  const host = useHost()
  const modal = useModal(!!file, close)
  const [state, setState] = useState<Loaded | null>(null)
  useEffect(() => {
    if (!file) return
    const abort = new AbortController()
    const kind = suffixOf(file.path)
    const load = async () => {
      const response = await fetch(file.url, { signal: abort.signal })
      if (!response.ok) throw new Error(((await response.json().catch(() => null)) as { error?: string } | null)?.error ?? 'Tether cannot open this file.')
      if (Number(response.headers.get('Content-Length')) > MAX_BYTES) throw new Error('This file is too large to show (50 MB at most). Download it instead.')
      const data = await response.arrayBuffer()
      if (data.byteLength > MAX_BYTES) throw new Error('This file is too large to show (50 MB at most). Download it instead.')
      if (kind !== 'docx' && kind !== 'pptx') {
        const { read } = await import('xlsx')
        try { return { sheet: read(data, { type: 'array', dense: true, sheetRows: MAX_ROWS + 1 }) } } catch { throw new Error(UNREADABLE) }
      }
      return { data }
    }
    load().then(result => setState({ url: file.url, ...result }),
      failure => { if (!abort.signal.aborted) setState({ url: file.url, ...(failure instanceof TypeError ? { unreachable: true } : { error: String(failure.message) }) }) })
    return () => abort.abort()
  }, [file])
  const current = file && state?.url === file.url ? state : null
  const name = file?.path.split('/').pop() || file?.path
  const kind = file ? suffixOf(file.path) : ''
  return <dialog {...modal} className="text-viewer office-viewer" aria-labelledby="office-viewer-title">{file && <>
    <div className="text-viewer-head">
      <div className="text-viewer-title"><h2 id="office-viewer-title">{name}</h2><p title={file.path}><bdi>{file.path}</bdi></p></div>
      {onShow && <button type="button" className="viewer-files" onClick={onShow}><FolderOpen size={15} aria-hidden="true" />Show in Files</button>}
      <a className="icon-button" href={file.url} download={name} aria-label={`Download ${name}`} title="Download"><Download size={19} /></a>
      <button type="button" className="icon-button" aria-label="Close" onClick={close}><X /></button>
    </div>
    {current?.error || current?.unreachable ? <div className="text-viewer-body"><p className="text-viewer-note" role="alert">{current.unreachable ? `Cannot reach ${host}.` : current.error}</p></div>
      : !current ? <div className="text-viewer-body"><p className="text-viewer-note">Loading…</p></div>
      : current.sheet ? <Sheets key={current.url} book={current.sheet} name={name ?? ''} />
      : current.data && kind === 'docx' ? <WordDocument key={current.url} data={current.data} name={name ?? ''} />
      : current.data ? <Slides key={current.url} data={current.data} name={name ?? ''} /> : null}
  </>}</dialog>
}

function Sheets({ book, name }: { book: WorkBook; name: string }) {
  const [chosen, setChosen] = useState(0)
  const [table, setTable] = useState<{ index: number; rows: string[][]; firstRow: number; columns: string[]; more: boolean } | null>(null)
  const index = Math.min(chosen, book.SheetNames.length - 1)
  useEffect(() => {
    let live = true
    import('xlsx').then(({ utils }) => {
      const sheet = book.Sheets[book.SheetNames[index]]
      const ref = sheet?.['!ref']
      if (!live) return
      if (!sheet || !ref) { setTable({ index, rows: [], firstRow: 1, columns: [], more: false }); return }
      const range = utils.decode_range(ref)
      const last = Math.min(range.e.c, range.s.c + MAX_COLUMNS - 1)
      const rows = (utils.sheet_to_json<unknown[]>(sheet, { header: 1, raw: false, defval: '', blankrows: true, range: { s: range.s, e: { r: range.e.r, c: last } } }))
        .slice(0, MAX_ROWS).map(row => row.map(cell => cell == null ? '' : String(cell)))
      const columns = Array.from({ length: last - range.s.c + 1 }, (_, at) => utils.encode_col(range.s.c + at))
      setTable({ index, rows, firstRow: range.s.r + 1, columns, more: range.e.r - range.s.r + 1 > MAX_ROWS || range.e.c > last })
    }, () => live && setTable({ index, rows: [], firstRow: 1, columns: [], more: false }))
    return () => { live = false }
  }, [book, index])
  const shown = table?.index === index ? table : null
  return <>
    {book.SheetNames.length > 1 && <div className="office-tabs" role="tablist" aria-label="Sheets">
      {book.SheetNames.map((sheet, at) => <button key={at} type="button" role="tab" aria-selected={at === index} onClick={() => setChosen(at)}>{sheet}</button>)}
    </div>}
    <div className="text-viewer-body office-sheet" tabIndex={0} role={book.SheetNames.length > 1 ? 'tabpanel' : undefined} aria-label={`${book.SheetNames[index]} in ${name}`}>
      {!shown ? <p className="text-viewer-note">Loading…</p>
        : !shown.rows.length ? <p className="text-viewer-note">This sheet is empty.</p>
        : <>
          <table>
            <thead><tr><th aria-hidden="true" />{shown.columns.map(column => <th key={column} scope="col">{column}</th>)}</tr></thead>
            <tbody>{shown.rows.map((row, at) => <tr key={at}><th scope="row">{shown.firstRow + at}</th>
              {shown.columns.map((_, column) => <td key={column}>{row[column] ?? ''}</td>)}</tr>)}</tbody>
          </table>
          {shown.more && <p className="text-viewer-note">Tether shows the first {MAX_ROWS} rows and {MAX_COLUMNS} columns. Download the file to see the rest.</p>}
        </>}
    </div>
  </>
}

// Links in a document open in a new tab when they go to a web page or an email address. Other links show as text.
function safeLinks(root: HTMLElement) {
  for (const link of root.querySelectorAll('a')) {
    const href = link.getAttribute('href') ?? ''
    if (/^(https?|mailto):/i.test(href)) { link.target = '_blank'; link.rel = 'noopener noreferrer' }
    else if (!href.startsWith('#')) link.removeAttribute('href')
  }
}

function WordDocument({ data, name }: { data: ArrayBuffer; name: string }) {
  const body = useRef<HTMLDivElement>(null)
  const [error, setError] = useState('')
  // A phone is narrower than the page: let the text wrap to the screen instead of scrolling sideways.
  const narrow = useMemo(() => typeof matchMedia === 'function' && matchMedia('(max-width: 820px)').matches, [])
  useEffect(() => {
    const target = body.current
    if (!target) return
    let live = true
    import('docx-preview')
      .then(({ renderAsync }) => renderAsync(data, target, target, { className: 'docx', inWrapper: true, ignoreWidth: narrow, ignoreHeight: narrow, breakPages: !narrow, renderComments: false, renderChanges: false, experimental: false }))
      .then(() => { if (live) safeLinks(target) }, () => { if (live) setError(UNREADABLE) })
    return () => { live = false; target.replaceChildren() }
  }, [data, narrow])
  return <div className={narrow ? 'text-viewer-body office-doc narrow' : 'text-viewer-body office-doc'} tabIndex={0} aria-label={`Contents of ${name}`}>
    {error && <p className="text-viewer-note" role="alert">{error}</p>}
    <div ref={body} />
  </div>
}

function Slides({ data, name }: { data: ArrayBuffer; name: string }) {
  const scroller = useRef<HTMLDivElement>(null)
  const stage = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<{ count: number; error?: string } | null>(null)
  useEffect(() => {
    const target = stage.current
    if (!target || !scroller.current) return
    const abort = new AbortController()
    let viewer: { destroy(): void } | null = null
    import('@aiden0z/pptx-renderer')
      .then(({ PptxViewer, RECOMMENDED_ZIP_LIMITS }) => PptxViewer.open(data, target, {
        signal: abort.signal, zipLimits: RECOMMENDED_ZIP_LIMITS, externalMedia: 'block', pdfjs: false, fitMode: 'contain',
        scrollContainer: scroller.current ?? undefined, lazyMedia: true, lazySlides: true,
        renderMode: 'list', listOptions: { windowed: true, showSlideLabels: true },
      }))
      .then(opened => {
        if (abort.signal.aborted) { opened.destroy(); return }
        viewer = opened
        setState({ count: opened.slideCount })
      }, () => { if (!abort.signal.aborted) setState({ count: 0, error: UNREADABLE }) })
    return () => { abort.abort(); viewer?.destroy(); target.replaceChildren() }
  }, [data])
  return <div ref={scroller} className="text-viewer-body office-slides" tabIndex={0} aria-label={state && !state.error ? `${state.count} slides in ${name}` : `Slides in ${name}`}>
    {!state ? <p className="text-viewer-note">Loading…</p> : state.error ? <p className="text-viewer-note" role="alert">{state.error}</p>
      : !state.count ? <p className="text-viewer-note">This deck has no slides.</p> : null}
    <div ref={stage} className="office-slides-stage" />
  </div>
}
