// PROTOTYPE — throwaway (branch share-ui-proto). Four layouts for the Share screen, switchable with ?variant=A|B|C|D
// and the floating bar at the bottom. Files live in memory only: "Add" uses object URLs, a reload forgets them.
import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, Bot, Check, ChevronLeft, ChevronRight, ChevronsRight, Download, File, FileText, Film, Image as ImageIcon, Laptop, MessageSquarePlus, Music, Pencil, Play, Plus, Search, Smartphone, Trash2, X } from 'lucide-react'
import { ImageViewer, type Media } from './ImageViewer'
import './SharePrototype.css'

type Kind = 'image' | 'video' | 'audio' | 'pdf' | 'text' | 'other'
type Source = { type: 'phone' | 'desktop' | 'agent'; label: string }
type Item = { id: string; name: string; kind: Kind; size: number; created: number; source: Source; url?: string; thumb?: string; page?: string; note?: string; duration?: number; drop: string }
type Upload = { id: string; name: string; size: number; done: number }

const HOUR = 3600e3, DAY = 24 * HOUR
const now = Date.now()

function swatch(hue: number, seed: number, w = 480, h = 360) {
  const canvas = document.createElement('canvas'); canvas.width = w; canvas.height = h
  const c = canvas.getContext('2d')!
  const g = c.createLinearGradient(0, 0, w, h)
  g.addColorStop(0, `hsl(${hue} 42% 62%)`); g.addColorStop(1, `hsl(${(hue + 40) % 360} 38% 34%)`)
  c.fillStyle = g; c.fillRect(0, 0, w, h)
  for (let i = 0; i < 5; i++) {
    c.fillStyle = `hsla(${(hue + i * 25) % 360} 50% ${70 - i * 8}% / .35)`
    c.beginPath(); c.arc(((seed * 97 + i * 131) % w), ((seed * 53 + i * 89) % h), 40 + ((seed + i) * 37) % 120, 0, 7); c.fill()
  }
  return canvas.toDataURL('image/jpeg', .8)
}

/** A drawn first page for documents. The real build renders page 1 of a PDF with pdf.js; text files show their real text. */
function page(title: string, kind: Kind, text?: string) {
  const w = 360, h = 480, canvas = document.createElement('canvas'); canvas.width = w; canvas.height = h
  const c = canvas.getContext('2d')!
  c.fillStyle = '#fbfaf6'; c.fillRect(0, 0, w, h)
  if (text != null) {
    c.fillStyle = '#2b2a26'; c.font = '600 15px ui-monospace, monospace'
    text.split('\n').slice(0, 34).forEach((line, i) => { c.font = `${/^#/.test(line) ? 700 : 400} ${/^#/.test(line) ? 15 : 12}px ui-monospace, monospace`; c.fillText(line.slice(0, 44), 24, 36 + i * 13.4) })
    return canvas.toDataURL('image/jpeg', .85)
  }
  const seed = [...title].reduce((sum, ch) => sum + ch.charCodeAt(0), 0)
  c.fillStyle = kind === 'pdf' ? '#3d4a2a' : '#2b2a26'; c.fillRect(24, 28, 34, 34)
  c.fillStyle = '#2b2a26'; c.font = '700 20px system-ui, sans-serif'; c.fillText(title.replace(/\.[^.]+$/, '').replace(/[-_]/g, ' ').slice(0, 22), 70, 52)
  for (let i = 0; i < 22; i++) {
    const y = 96 + i * 16 + Math.floor(i / 6) * 14
    c.fillStyle = i % 6 === 0 ? '#8a877d' : '#cfccc2'
    c.fillRect(24, y, (i % 6 === 0 ? 120 : 200 + ((seed * (i + 3)) % 112)), i % 6 === 0 ? 8 : 6)
  }
  c.fillStyle = '#e7e4da'; c.fillRect(24, 430, w - 48, 1); c.fillStyle = '#8a877d'; c.fillRect(w - 110, 444, 86, 9)
  return canvas.toDataURL('image/jpeg', .85)
}

function samples(): Item[] {
  const phone: Source = { type: 'phone', label: 'Phone' }, desk: Source = { type: 'desktop', label: 'Desktop' }
  const claude: Source = { type: 'agent', label: 'Claude · tether' }, codex: Source = { type: 'agent', label: 'Codex · project' }
  const img = (id: string, name: string, hue: number, ago: number, source: Source, drop: string, extra: Partial<Item> = {}): Item => {
    const thumb = swatch(hue, Number(id.replace(/\D/g, '')) || 3)
    return { id, name, kind: 'image', size: 1.2e6 + hue * 9000, created: now - ago, source, url: thumb, thumb, drop, ...extra }
  }
  return [
    img('i1', 'whiteboard-plan.jpg', 30, 12 * 60e3, phone, 'd1', { note: 'Share feature sketch' }),
    img('i2', 'whiteboard-plan-2.jpg', 45, 12 * 60e3, phone, 'd1'),
    img('i3', 'whiteboard-plan-3.jpg', 60, 12 * 60e3, phone, 'd1'),
    { id: 'v1', name: 'friday-demo.mp4', kind: 'video', size: 48e6, created: now - 2 * HOUR, source: claude, thumb: swatch(200, 7), duration: 74, drop: 'd2', note: 'Screen recording of Live mode' },
    img('i4', 'render-hero-v3.png', 260, 3 * HOUR, claude, 'd3', { note: 'Blender render, 640x360' }),
    { id: 'p1', name: 'lease-agreement.pdf', kind: 'pdf', size: 820e3, created: now - 5 * HOUR, source: desk, drop: 'd4', page: page('lease-agreement.pdf', 'pdf') },
    { id: 't1', name: 'release-notes.md', kind: 'text', size: 6.4e3, created: now - DAY - 2 * HOUR, source: codex, drop: 'd5', page: page('release-notes.md', 'text', '# Release notes\n\n## 0.9.0\n- Share: a private file store\n- Upload from phone or desktop\n- Send a file to chat\n- Agents add files with the CLI\n\n## 0.8.4\n- Usage follows an account switch\n- + on a session row\n- Live mode: faster final pass\n\n## 0.8.3\n- Queue: !cmd leaves the queue\n- Notices for blocked tools\n- Codex level hook\n\n## 0.8.2\n- Pinned side tab\n- Narrow pane fixes') },
    img('i5', 'receipt-hardware.jpg', 15, DAY + 4 * HOUR, phone, 'd6'),
    { id: 'a1', name: 'voice-memo-idea.m4a', kind: 'audio', size: 2.1e6, created: now - DAY - 6 * HOUR, source: phone, duration: 95, drop: 'd7' },
    img('i6', 'dashboard-shot.png', 180, 3 * DAY, desk, 'd8'),
    img('i7', 'dashboard-shot-dark.png', 220, 3 * DAY, desk, 'd8'),
    { id: 'o1', name: 'invoices-2026.zip', kind: 'other', size: 312e6, created: now - 4 * DAY, source: desk, drop: 'd9' },
    { id: 'v2', name: 'garden-timelapse.mov', kind: 'video', size: 640e6, created: now - 6 * DAY, source: phone, thumb: swatch(110, 11), duration: 212, drop: 'd10' },
    img('i8', 'passport-scan.jpg', 330, 9 * DAY, phone, 'd11'),
  ]
}

const kindOf = (file: globalThis.File): Kind => file.type.startsWith('image/') ? 'image' : file.type.startsWith('video/') ? 'video'
  : file.type.startsWith('audio/') ? 'audio' : file.type === 'application/pdf' ? 'pdf' : file.type.startsWith('text/') ? 'text' : 'other'
const size = (n: number) => n < 1e3 ? `${n} B` : n < 1e6 ? `${(n / 1e3).toFixed(0)} KB` : n < 1e9 ? `${(n / 1e6).toFixed(1)} MB` : `${(n / 1e9).toFixed(2)} GB`
const clock = (s?: number) => s == null ? '' : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`
const ago = (t: number) => { const m = Math.round((now - t) / 60e3); return m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : new Date(t).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' }) }
const dayLabel = (t: number) => { const d = Math.floor((now - t) / DAY); return d < 1 ? 'Today' : d < 2 ? 'Yesterday' : d < 7 ? 'This week' : 'Earlier' }
const ext = (name: string) => (name.split('.').pop() ?? '').toUpperCase().slice(0, 4)
const KIND_ICON: Record<Kind, typeof File> = { image: ImageIcon, video: Film, audio: Music, pdf: FileText, text: FileText, other: File }
const SOURCE_ICON = { phone: Smartphone, desktop: Laptop, agent: Bot }
type Filter = 'all' | 'media' | 'docs' | 'other'
const FILTERS: { id: Filter; label: string }[] = [{ id: 'all', label: 'All' }, { id: 'media', label: 'Photos & videos' }, { id: 'docs', label: 'Docs' }, { id: 'other', label: 'Other' }]
const matches = (item: Item, filter: Filter, q: string) => (filter === 'all' || (filter === 'media' ? ['image', 'video'].includes(item.kind)
  : filter === 'docs' ? ['pdf', 'text'].includes(item.kind) : ['audio', 'other'].includes(item.kind))) && item.name.toLowerCase().includes(q.toLowerCase())

/** Shared state for all variants: the items, the add/delete/open actions, and a toast. */
function useShare(onAddToChat?: (name: string) => void, simulate = false) {
  const [items, setItems] = useState<Item[]>(() => samples())
  const [uploads, setUploads] = useState<Upload[]>([])
  const busy = uploads.some(u => u.done < u.size)
  // Fake upload: at most two files move at once, as the backend allows.
  useEffect(() => {
    if (!busy) { if (!uploads.length) return; const t = setTimeout(() => setUploads([]), 2600); return () => clearTimeout(t) }
    const t = setInterval(() => setUploads(list => { let active = 0; return list.map(u => {
      if (u.done >= u.size || active >= 2) return u
      active++; return { ...u, done: Math.min(u.size, u.done + Math.max(u.size / 24, .5e6)) }
    }) }), 100)
    return () => clearInterval(t)
  }, [busy, uploads.length])
  const [viewing, setViewing] = useState<Media | null>(null)
  const [toast, setToast] = useState('')
  const [dragging, setDragging] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(''), 2600); return () => clearTimeout(t) }, [toast])
  const add = (files: FileList | globalThis.File[] | null, note?: string) => {
    if (!files?.length) return
    const drop = `n${Date.now()}`
    const added = [...files].map((file, i): Item => {
      const kind = kindOf(file), url = URL.createObjectURL(file)
      const id = `${drop}-${i}`
      if (kind === 'text') file.slice(0, 4000).text().then(text => setItems(list => list.map(item => item.id === id ? { ...item, page: page(file.name, kind, text) } : item)))
      return { id, name: file.name, kind, size: file.size, created: Date.now(), source: { type: matchMedia('(pointer: coarse)').matches ? 'phone' : 'desktop', label: matchMedia('(pointer: coarse)').matches ? 'Phone' : 'Desktop' }, url, thumb: kind === 'image' ? url : undefined, page: kind === 'pdf' ? page(file.name, kind) : undefined, drop, note }
    })
    setItems(list => [...added, ...list])
    if (simulate) setUploads(list => [...list, ...added.map(item => ({ id: item.id, name: item.name, size: item.size, done: 0 }))])
    else setToast(added.length === 1 ? `Added ${added[0].name}` : `Added ${added.length} files`)
  }
  const remove = (ids: string[]) => { setItems(list => list.filter(item => !ids.includes(item.id))); setToast(ids.length === 1 ? 'Moved to Trash. Undo for 30 days in Trash.' : `Moved ${ids.length} files to Trash.`) }
  const open = (item: Item) => {
    if (item.kind === 'image' || item.kind === 'audio' || (item.kind === 'video' && item.url)) setViewing({ name: item.name, url: item.url ?? item.thumb ?? '', kind: item.kind as Media['kind'] })
    else if (item.kind === 'video') setViewing({ name: item.name, url: item.thumb ?? '', kind: 'image' })
    else setToast(`${item.name} opens in a viewer or downloads (not in the prototype).`)
  }
  const toChat = (item: Item) => { onAddToChat?.(item.name); setToast(`Added ${item.name} to the message.`) }
  const used = items.reduce((sum, item) => sum + item.size, 0)
  const drop = {
    onDragOver: (e: React.DragEvent) => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDragging(true) } },
    onDragLeave: (e: React.DragEvent) => { if (e.currentTarget === e.target) setDragging(false) },
    onDrop: (e: React.DragEvent) => { e.preventDefault(); setDragging(false); add(e.dataTransfer.files) },
    onPaste: (e: React.ClipboardEvent) => { if (e.clipboardData.files.length) add(e.clipboardData.files) },
  }
  const picker = <input ref={input} type="file" multiple hidden onChange={e => { add(e.target.files); e.target.value = '' }} />
  return { items, uploads, add, remove, open, toChat, used, viewing, setViewing, toast, setToast, dragging, drop, picker, pick: () => input.current?.click() }
}
type Share = ReturnType<typeof useShare>

function Thumb({ item, big }: { item: Item; big?: boolean }) {
  const Icon = KIND_ICON[item.kind]
  if (item.thumb) return <span className="sp-thumb"><img src={item.thumb} alt="" loading="lazy" />{item.kind === 'video' && <span className="sp-badge">{clock(item.duration)}</span>}</span>
  return <span className={`sp-thumb sp-doc k-${item.kind}`}><Icon size={big ? 40 : 22} strokeWidth={1.5} aria-hidden="true" /><b>{ext(item.name)}</b></span>
}

function SourceTag({ source }: { source: Source }) {
  const Icon = SOURCE_ICON[source.type]
  return <span className="sp-source"><Icon size={13} aria-hidden="true" />{source.label}</span>
}

function Usage({ used }: { used: number }) {
  return <span className="sp-usage" title="Share storage">{size(used)} of 50 GB</span>
}

// ── A: Library. Photo-library grid grouped by day, filter chips, select mode with a bottom action bar. ──
function Library({ s }: { s: Share }) {
  const [filter, setFilter] = useState<Filter>('all')
  const [q, setQ] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const shown = s.items.filter(item => matches(item, filter, q))
  const groups = useMemo(() => { const map = new Map<string, Item[]>(); for (const item of shown) map.set(dayLabel(item.created), [...(map.get(dayLabel(item.created)) ?? []), item]); return [...map] }, [shown])
  const toggle = (id: string) => setPicked(list => list.includes(id) ? list.filter(x => x !== id) : [...list, id])
  const [selectMode, setSelectMode] = useState(false)
  const selecting = selectMode || picked.length > 0
  return <div className={`sp-a${selecting ? ' selecting' : ''}`}>
    <header className="sp-head">
      <h1>Share</h1><Usage used={s.used} />
      <label className="sp-search"><Search size={16} aria-hidden="true" /><input type="search" placeholder="Search Share" value={q} onChange={e => setQ(e.target.value)} /></label>
      <button type="button" className="sp-text" aria-pressed={selectMode} onClick={() => { setSelectMode(!selectMode); setPicked([]) }}>{selectMode ? 'Done' : 'Select'}</button>
      <button type="button" className="sp-primary" onClick={s.pick}><Plus size={17} aria-hidden="true" />Add</button>
    </header>
    <nav className="sp-chips" aria-label="Filter">{FILTERS.map(f => <button key={f.id} type="button" className="sp-chip" aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}>{f.label}</button>)}</nav>
    <div className="sp-scroll">
      {groups.map(([label, list]) => <section key={label}>
        <h2 className="sp-day">{label}</h2>
        <ul className="sp-grid">{list.map(item => <li key={item.id} className={picked.includes(item.id) ? 'picked' : undefined}>
          <button type="button" className="sp-tile" onClick={() => selecting ? toggle(item.id) : s.open(item)} aria-label={`${item.name}, ${size(item.size)}`}>
            <Thumb item={item} />{!item.thumb && <span className="sp-tile-name">{item.name}</span>}
          </button>
          <button type="button" className="sp-check" aria-label={`Select ${item.name}`} aria-pressed={picked.includes(item.id)} onClick={() => toggle(item.id)}><span /></button>
        </li>)}</ul>
      </section>)}
      {!shown.length && <p className="sp-empty">Nothing here. Drop files anywhere on this screen, or press Add.</p>}
    </div>
    {picked.length > 0 && <div className="sp-actionbar" role="toolbar" aria-label="Selected files">
      <button type="button" onClick={() => { setPicked([]); setSelectMode(false) }} aria-label="Clear selection"><X size={18} /></button>
      <span>{picked.length} selected</span>
      <button type="button" onClick={() => { const item = s.items.find(i => i.id === picked[0]); if (item) s.toChat(item); setPicked([]) }}><MessageSquarePlus size={18} aria-hidden="true" />Send to chat</button>
      <button type="button" onClick={() => s.setToast('Download starts (not in the prototype).')}><Download size={18} aria-hidden="true" />Download</button>
      <button type="button" className="sp-danger" onClick={() => { s.remove(picked); setPicked([]) }}><Trash2 size={18} aria-hidden="true" />Delete</button>
    </div>}
  </div>
}

// ── B: Shelf. Dense list with a details pane (desktop) or a details page (phone). Built for finding and acting. ──
function Shelf({ s, wide }: { s: Share; wide: boolean }) {
  const [filter, setFilter] = useState<Filter>('all')
  const [q, setQ] = useState('')
  const [selected, setSelected] = useState<string | null>(wide ? s.items[0]?.id ?? null : null)
  const shown = s.items.filter(item => matches(item, filter, q))
  const current = s.items.find(item => item.id === selected) ?? null
  const details = current && <aside className="sp-details" aria-label="Details">
    {!wide && <button type="button" className="sp-back" onClick={() => setSelected(null)}><ArrowLeft size={18} aria-hidden="true" />Share</button>}
    <button type="button" className="sp-preview" onClick={() => s.open(current)} aria-label={`Open ${current.name}`}><Thumb item={current} big /></button>
    <h2>{current.name}</h2>
    {current.note && <p className="sp-note">{current.note}</p>}
    <dl>
      <dt>Size</dt><dd>{size(current.size)}{current.duration ? ` · ${clock(current.duration)}` : ''}</dd>
      <dt>Added</dt><dd>{ago(current.created)}</dd>
      <dt>From</dt><dd><SourceTag source={current.source} /></dd>
    </dl>
    <div className="sp-detail-actions">
      <button type="button" className="sp-primary" onClick={() => s.toChat(current)}><MessageSquarePlus size={17} aria-hidden="true" />Send to chat</button>
      <button type="button" className="sp-secondary" onClick={() => s.setToast('Download starts (not in the prototype).')}><Download size={17} aria-hidden="true" />Download</button>
      <button type="button" className="sp-secondary sp-danger" onClick={() => { s.remove([current.id]); setSelected(null) }}><Trash2 size={17} aria-hidden="true" />Delete</button>
    </div>
  </aside>
  if (!wide && current) return <div className="sp-b">{details}</div>
  return <div className="sp-b">
    <div className="sp-b-list">
      <header className="sp-head">
        <h1>Share</h1><Usage used={s.used} />
        <button type="button" className="sp-primary" onClick={s.pick}><Plus size={17} aria-hidden="true" />Add</button>
      </header>
      <div className="sp-b-tools">
        <label className="sp-search"><Search size={16} aria-hidden="true" /><input type="search" placeholder="Search Share" value={q} onChange={e => setQ(e.target.value)} /></label>
        <select value={filter} onChange={e => setFilter(e.target.value as Filter)} aria-label="Filter">{FILTERS.map(f => <option key={f.id} value={f.id}>{f.label}</option>)}</select>
      </div>
      <ul className="sp-rows">{shown.map(item => <li key={item.id}>
        <button type="button" aria-current={item.id === selected || undefined} onClick={() => setSelected(item.id)}>
          <Thumb item={item} />
          <span className="sp-row-main"><span className="sp-row-name">{item.name}</span><span className="sp-row-meta"><SourceTag source={item.source} /> · {ago(item.created)}</span></span>
          <span className="sp-row-size">{size(item.size)}</span>
        </button>
      </li>)}</ul>
    </div>
    {wide && (details || <aside className="sp-details sp-details-empty">Pick a file to see it here.</aside>)}
  </div>
}

// ── C: Drops. A feed to yourself, like a chat with one person. Each drop keeps its files together, with an optional note. ──
function Drops({ s }: { s: Share }) {
  const [note, setNote] = useState('')
  const end = useRef<HTMLDivElement>(null)
  const drops = useMemo(() => { const map = new Map<string, Item[]>(); for (const item of [...s.items].reverse()) map.set(item.drop, [...(map.get(item.drop) ?? []), item]); return [...map.values()] }, [s.items])
  useEffect(() => { end.current?.scrollIntoView({ block: 'end' }) }, [drops.length])
  return <div className="sp-c">
    <header className="sp-head"><h1>Share</h1><Usage used={s.used} /></header>
    <div className="sp-feed">
      {drops.map(list => { const first = list[0]; const media = list.filter(i => i.thumb); const rest = list.filter(i => !i.thumb)
        return <article key={first.drop} className="sp-drop">
          <header><SourceTag source={first.source} /><time>{ago(first.created)}</time></header>
          {first.note && <p className="sp-note">{first.note}</p>}
          {media.length > 0 && <div className={`sp-mosaic n${Math.min(media.length, 4)}`}>{media.slice(0, 4).map(item => <button key={item.id} type="button" onClick={() => s.open(item)} aria-label={item.name}><Thumb item={item} /></button>)}</div>}
          {rest.map(item => <button key={item.id} type="button" className="sp-filecard" onClick={() => s.open(item)}><Thumb item={item} /><span><b>{item.name}</b><small>{size(item.size)}{item.duration ? ` · ${clock(item.duration)}` : ''}</small></span></button>)}
          <footer>
            <button type="button" onClick={() => s.toChat(first)}><MessageSquarePlus size={15} aria-hidden="true" />Send to chat</button>
            <button type="button" onClick={() => s.setToast('Download starts (not in the prototype).')}><Download size={15} aria-hidden="true" />Download</button>
            <button type="button" className="sp-danger" onClick={() => s.remove(list.map(i => i.id))}><Trash2 size={15} aria-hidden="true" />Delete</button>
          </footer>
        </article> })}
      <div ref={end} />
    </div>
    <form className="sp-dropbar" onSubmit={e => { e.preventDefault(); s.pick() }}>
      <button type="button" className="sp-round" aria-label="Add files" onClick={s.pick}><Plus size={20} /></button>
      <input type="text" placeholder="Note for the next drop (optional)" value={note} onChange={e => setNote(e.target.value)} />
      <button type="submit" className="sp-primary">Pick files</button>
    </form>
  </div>
}

// ── D: Library+. A's grid, plus what the Dribbble review found: real page previews on document tiles, a preview pane
// beside the grid (full screen on phone), an upload queue with progress, and a "From agents" strip. ──
const KIND_LABEL: Record<Kind, string> = { image: 'Photo', video: 'Video', audio: 'Audio', pdf: 'PDF', text: 'Text', other: 'File' }

function Ring({ value, size: px = 22 }: { value: number; size?: number }) {
  const r = px / 2 - 2.5, c = 2 * Math.PI * r
  return <svg className="sp-ring" width={px} height={px} viewBox={`0 0 ${px} ${px}`} aria-hidden="true">
    <circle cx={px / 2} cy={px / 2} r={r} /><circle cx={px / 2} cy={px / 2} r={r} strokeDasharray={c} strokeDashoffset={c * (1 - value)} />
  </svg>
}

function DThumb({ item, big }: { item: Item; big?: boolean }) {
  if (!item.page) return <Thumb item={item} big={big} />
  return <span className="sp-thumb sp-page"><img src={item.page} alt="" loading="lazy" /><b className={`sp-ext k-${item.kind}`}>{ext(item.name)}</b></span>
}

function Uploads({ list }: { list: Upload[] }) {
  const left = list.filter(u => u.done < u.size)
  const total = list.reduce((sum, u) => sum + u.size, 0), done = list.reduce((sum, u) => sum + u.done, 0)
  return <aside className="sp-uploads" aria-label="Uploads" aria-live="polite">
    <header>{left.length ? <Ring value={done / total} /> : <Check size={18} className="sp-ok" aria-hidden="true" />}
      <b>{left.length ? `Adding ${list.length} ${list.length === 1 ? 'file' : 'files'}` : `Added ${list.length} ${list.length === 1 ? 'file' : 'files'}`}</b>
      {left.length > 0 && <span>{size(done)} of {size(total)}</span>}</header>
    {left.length > 0 && <ul>{list.slice(0, 3).map(u => <li key={u.id}>
      <span className="sp-up-name">{u.name}</span>
      <span className="sp-up-meta">{u.done >= u.size ? 'Done' : u.done ? `${size(u.done)} of ${size(u.size)}` : 'Waiting'}</span>
      {u.done >= u.size ? <Check size={16} className="sp-ok" aria-hidden="true" /> : <Ring value={u.done / u.size} size={18} />}
    </li>)}{list.length > 3 && <li className="sp-up-more">and {list.length - 3} more</li>}</ul>}
  </aside>
}

function Pane({ item, s, wide, close }: { item: Item; s: Share; wide: boolean; close: () => void }) {
  const big = item.page ? <img className="sp-pane-page" src={item.page} alt={`First page of ${item.name}`} />
    : item.kind === 'image' ? <img className="sp-pane-img" src={item.url ?? item.thumb} alt={item.name} />
    : item.kind === 'video' ? <button type="button" className="sp-pane-video" onClick={() => s.open(item)} aria-label={`Play ${item.name}`}><img src={item.thumb} alt="" /><span><Play size={26} fill="currentColor" /></span></button>
    : item.kind === 'audio' ? <div className="sp-pane-none"><Music size={44} strokeWidth={1.4} />{item.url ? <audio controls src={item.url} /> : <span>Audio · {clock(item.duration)}</span>}</div>
    : <div className="sp-pane-none"><File size={44} strokeWidth={1.4} /><b>{ext(item.name)}</b><span>No preview. Download it to open.</span></div>
  return <aside className={`sp-pane${wide ? '' : ' sp-sheet'}`} aria-label={`Preview of ${item.name}`} onKeyDown={e => { if (e.key === 'Escape') close() }}>
    <header>
      <button type="button" className="sp-icon" onClick={close} aria-label="Close preview" autoFocus>{wide ? <ChevronsRight size={18} /> : <ArrowLeft size={20} />}</button>
      <div><h2>{item.name}</h2><p>{KIND_LABEL[item.kind]} · {size(item.size)}{item.duration ? ` · ${clock(item.duration)}` : ''}</p></div>
    </header>
    <div className="sp-pane-actions">
      <button type="button" className="sp-primary" onClick={() => s.toChat(item)}><MessageSquarePlus size={17} aria-hidden="true" />Send to chat</button>
      <button type="button" className="sp-secondary" onClick={() => s.setToast('Download starts (not in the prototype).')}><Download size={17} aria-hidden="true" />Download</button>
      <button type="button" className="sp-secondary sp-icon-only" onClick={() => s.setToast('Rename opens an inline field (not in the prototype).')} aria-label="Rename" title="Rename"><Pencil size={17} /></button>
      <button type="button" className="sp-secondary sp-icon-only sp-danger" onClick={() => { s.remove([item.id]); close() }} aria-label="Delete" title="Delete"><Trash2 size={17} /></button>
    </div>
    <div className="sp-pane-body">
      <div className="sp-pane-view">{big}</div>
      {item.note && <p className="sp-note">{item.note}</p>}
      <p className="sp-pane-meta"><SourceTag source={item.source} /> · {ago(item.created)}</p>
    </div>
  </aside>
}

function LibraryPlus({ s, wide }: { s: Share; wide: boolean }) {
  const [filter, setFilter] = useState<Filter>('all')
  const [q, setQ] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [selectMode, setSelectMode] = useState(false)
  const [openId, setOpenId] = useState<string | null>(null)
  const [hideAgents, setHideAgents] = useState(false)
  const selecting = selectMode || picked.length > 0
  const current = s.items.find(item => item.id === openId) ?? null
  const progress = new Map(s.uploads.filter(u => u.done < u.size).map(u => [u.id, u.done / u.size]))
  const shown = s.items.filter(item => matches(item, filter, q))
  const groups = useMemo(() => { const map = new Map<string, Item[]>(); for (const item of shown) map.set(dayLabel(item.created), [...(map.get(dayLabel(item.created)) ?? []), item]); return [...map] }, [shown])
  const agentDrops = useMemo(() => { const map = new Map<string, Item[]>(); for (const item of s.items) if (item.source.type === 'agent' && now - item.created < 3 * DAY) map.set(item.drop, [...(map.get(item.drop) ?? []), item]); return [...map.values()].slice(0, 2) }, [s.items])
  const toggle = (id: string) => setPicked(list => list.includes(id) ? list.filter(x => x !== id) : [...list, id])
  return <div className={`sp-d${selecting ? ' selecting' : ''}${current && wide ? ' with-pane' : ''}`}>
    <div className="sp-d-main">
      <header className="sp-head">
        <h1>Share</h1><Usage used={s.used} />
        <label className="sp-search"><Search size={16} aria-hidden="true" /><input type="search" placeholder="Search Share" value={q} onChange={e => setQ(e.target.value)} /></label>
        <button type="button" className="sp-text" aria-pressed={selectMode} onClick={() => { setSelectMode(!selectMode); setPicked([]) }}>{selectMode ? 'Done' : 'Select'}</button>
        <button type="button" className="sp-primary" onClick={s.pick}><Plus size={17} aria-hidden="true" />Add</button>
      </header>
      <nav className="sp-chips" aria-label="Filter">{FILTERS.map(f => <button key={f.id} type="button" className="sp-chip" aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}>{f.label}</button>)}</nav>
      <div className="sp-scroll">
        {!hideAgents && filter === 'all' && !q && agentDrops.length > 0 && <section className="sp-agents" aria-label="From agents">
          <header><h2 className="sp-day">From agents</h2><button type="button" className="sp-icon" onClick={() => setHideAgents(true)} aria-label="Hide From agents"><X size={15} /></button></header>
          {agentDrops.map(list => <button key={list[0].drop} type="button" className="sp-agent-row" onClick={() => setOpenId(list[0].id)}>
            <span className="sp-agent-icon"><Bot size={16} aria-hidden="true" /></span>
            <span className="sp-agent-text"><b>{list[0].source.label.split(' · ')[0]}</b> sent {list[0].name}{list.length > 1 ? ` and ${list.length - 1} more` : ''}
              <small>{list[0].source.label.split(' · ')[1]} · {ago(list[0].created)}</small></span>
            <span className="sp-agent-thumbs">{list.slice(0, 3).map(item => <DThumb key={item.id} item={item} />)}</span>
          </button>)}
        </section>}
        {groups.map(([label, list]) => <section key={label}>
          <h2 className="sp-day">{label}</h2>
          <ul className="sp-grid">{list.map(item => <li key={item.id} className={[picked.includes(item.id) && 'picked', item.id === openId && 'open'].filter(Boolean).join(' ') || undefined}>
            <button type="button" className="sp-tile" onClick={() => selecting ? toggle(item.id) : setOpenId(item.id)} aria-label={`${item.name}, ${size(item.size)}`}>
              <DThumb item={item} />{!item.thumb && <span className="sp-tile-name">{item.name}</span>}
              {progress.has(item.id) && <span className="sp-tile-up"><Ring value={progress.get(item.id)!} size={30} /></span>}
            </button>
            <button type="button" className="sp-check" aria-label={`Select ${item.name}`} aria-pressed={picked.includes(item.id)} onClick={() => toggle(item.id)}><span /></button>
          </li>)}</ul>
        </section>)}
        {!shown.length && <p className="sp-empty">Nothing here. Drop files anywhere on this screen, or press Add.</p>}
      </div>
      {s.uploads.length > 0 && !picked.length && <Uploads list={s.uploads} />}
      {picked.length > 0 && <div className="sp-actionbar" role="toolbar" aria-label="Selected files">
        <button type="button" onClick={() => { setPicked([]); setSelectMode(false) }} aria-label="Clear selection"><X size={18} /></button>
        <span>{picked.length} selected</span>
        <button type="button" onClick={() => { const item = s.items.find(i => i.id === picked[0]); if (item) s.toChat(item); setPicked([]) }}><MessageSquarePlus size={18} aria-hidden="true" />Send to chat</button>
        <button type="button" onClick={() => s.setToast('Download starts (not in the prototype).')}><Download size={18} aria-hidden="true" />Download</button>
        <button type="button" className="sp-danger" onClick={() => { s.remove(picked); setPicked([]) }}><Trash2 size={18} aria-hidden="true" />Delete</button>
      </div>}
    </div>
    {current && <Pane key={current.id} item={current} s={s} wide={wide} close={() => setOpenId(null)} />}
  </div>
}

const VARIANTS = [{ key: 'A', name: 'Library' }, { key: 'B', name: 'Shelf' }, { key: 'C', name: 'Drops' }, { key: 'D', name: 'Library+' }]

export function SharePrototype({ wide, onAddToChat }: { wide: boolean; onAddToChat?: (name: string) => void }) {
  const [variant, setVariant] = useState(() => new URLSearchParams(location.search).get('variant')?.toUpperCase() ?? 'A')
  const index = Math.max(0, VARIANTS.findIndex(v => v.key === variant))
  const s = useShare(onAddToChat, VARIANTS[index].key === 'D')
  const go = (step: number) => {
    const next = VARIANTS[(index + step + VARIANTS.length) % VARIANTS.length].key
    const url = new URL(location.href); url.searchParams.set('variant', next); history.replaceState(history.state, '', url); setVariant(next)
  }
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if ((e.target as Element).closest('input, textarea, select, [contenteditable], .sp-pane') || document.querySelector('dialog[open]')) return; if (e.key === 'ArrowLeft') go(-1); if (e.key === 'ArrowRight') go(1) }
    addEventListener('keydown', key); return () => removeEventListener('keydown', key)
  })
  return <section className={`share-proto${wide ? ' wide' : ''}${s.dragging ? ' dragging' : ''}`} aria-label="Share" {...s.drop}>
    {s.picker}
    {{ A: <Library s={s} />, B: <Shelf s={s} wide={wide} />, C: <Drops s={s} />, D: <LibraryPlus s={s} wide={wide} /> }[VARIANTS[index].key]}
    {s.dragging && <div className="sp-dropzone" aria-hidden="true"><Plus size={28} />Drop to add to Share</div>}
    {s.toast && <p className="sp-toast" role="status">{s.toast}</p>}
    <ImageViewer image={s.viewing} close={() => s.setViewing(null)} />
    <div className="proto-switcher" role="toolbar" aria-label="Prototype variant">
      <button type="button" onClick={() => go(-1)} aria-label="Previous variant"><ChevronLeft size={16} /></button>
      <span>{VARIANTS[index].key} · {VARIANTS[index].name}</span>
      <button type="button" onClick={() => go(1)} aria-label="Next variant"><ChevronRight size={16} /></button>
    </div>
  </section>
}
