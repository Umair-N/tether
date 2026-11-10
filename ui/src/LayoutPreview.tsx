import type { CSSProperties, ReactNode } from 'react'
import type { GridColumn, Layout } from './layouts'

// A drawing of a layout on a desktop, a tablet and a phone, to the right scale, in the layout's colours. Settings shows
// it before a generated or imported layout applies. It draws the real rules: the rail always shows on a wide screen,
// a hidden sessions list gives its room to the chat, and the phone keeps its own layout (or ignores this one: keep).

export type Device = 'desktop' | 'tablet' | 'phone'
const SIZE: Record<Device, { width: number; height: number; frame: number; label: string }> = {
  desktop: { width: 1440, height: 900, frame: 264, label: 'Desktop' },
  tablet: { width: 1024, height: 768, frame: 188, label: 'Tablet' },
  phone: { width: 390, height: 844, frame: 92, label: 'Phone' },
}
const COLOURS = ['bg', 'sidebar', 'surface', 'bubble', 'text', 'muted', 'line', 'accent', 'attention', 'done'] as const

function colours(layout: Layout, own: boolean) {
  const theme = own ? layout.theme : null
  // No theme: the user's own colours, read live from the page.
  const pick = (key: typeof COLOURS[number], token: string) => (theme as Record<string, string | undefined> | null)?.[key] ?? `var(${token})`
  return Object.fromEntries(COLOURS.map(key => [`--p-${key}`, pick(key, key === 'sidebar' ? '--sidebar' : key === 'bubble' ? '--bubble' : `--${key}`)])) as CSSProperties
}

const Lines = ({ count, short }: { count: number; short?: boolean }) => <>{Array.from({ length: count }, (_, at) => <i key={at} className="lp-line" style={{ width: `${short ? 40 + (at * 23) % 35 : 55 + (at * 31) % 40}%` }} />)}</>

function Sessions({ variant }: { variant?: string }) {
  const rows = variant === 'compact' ? 9 : variant === 'avatars' ? 5 : 6
  return <div className={`lp-sessions lp-${variant ?? 'list'}`}>{Array.from({ length: rows }, (_, at) => <div key={at} className={`lp-row${at === 1 ? ' on' : ''}`}>
    {variant === 'avatars' && <b className="lp-avatar" style={{ background: `hsl(${(at * 67) % 360} 55% 55%)` }} />}
    <span><i className="lp-line" style={{ width: `${60 + (at * 17) % 30}%` }} />{variant === 'avatars' && <i className="lp-line lp-faint" style={{ width: `${45 + (at * 29) % 40}%` }} />}</span>
  </div>)}</div>
}

function Chat({ layout, phone }: { layout: Layout; phone?: boolean }) {
  const bubbles = layout.variants?.chat === 'bubbles'
  const tabs = !phone && layout.panels.includes('agents')
  const centre = !phone && layout.grid.chatWidth ? { maxWidth: `${Math.round(layout.grid.chatWidth / 14.4)}%` } : undefined
  return <div className="lp-chat">
    <div className="lp-head"><i className="lp-line" style={{ width: '34%' }} /></div>
    {tabs && <div className={`lp-tabs lp-tabs-${layout.variants?.tabs ?? 'pills'}`}>{[0, 1, 2].map(at => <b key={at} className={at === 0 ? 'on' : ''} />)}</div>}
    <div className="lp-messages" style={centre}>
      {bubbles ? <>
        <div className="lp-bubble lp-them"><Lines count={2} /></div>
        <div className="lp-bubble lp-me"><Lines count={1} short /></div>
        <div className="lp-bubble lp-them"><Lines count={3} /></div>
      </> : <>
        <div className="lp-plain"><Lines count={3} /></div>
        <div className="lp-bubble lp-me"><Lines count={1} short /></div>
        <div className="lp-plain"><Lines count={2} /></div>
      </>}
    </div>
    <div className="lp-composer" style={centre ? { ...centre, marginInline: 'auto', width: '100%' } : undefined} />
  </div>
}

function Panel({ name, layout }: { name: string; layout: Layout }) {
  let body: ReactNode = null
  if (name === 'sessions') body = <Sessions variant={layout.variants?.sessions} />
  else if (name === 'chat') body = <Chat layout={layout} />
  else if (name === 'activity') body = <div className="lp-activity">{[0, 1, 2, 3].map(at => <b key={at} className={at === 0 ? 'on' : ''} />)}</div>
  else if (name === 'waiting') body = <div className="lp-waiting">{[0, 1].map(at => <b key={at} />)}</div>
  else if (name === 'status') body = <div className="lp-status">{[0, 1, 2, 3, 4].map(at => <span key={at}><b className={at < 2 ? 'need' : ''} /><i className="lp-line" /></span>)}</div>
  return <div className={`lp-panel lp-${name}-panel`}>{body}</div>
}

function Wide({ layout, device }: { layout: Layout; device: Exclude<Device, 'phone'> }) {
  const { width, frame } = SIZE[device]
  const scale = frame / width
  let columns: GridColumn[] = layout.grid.columns.filter(column => !(column.main === 'sessions' && layout.grid.sessionsClosed))
  if (columns[0]?.main !== 'activity') columns = [{ main: 'activity' }, ...columns]
  const track = (column: GridColumn) => column.main === 'chat' ? 'minmax(0, 1fr)' : `${Math.round((column.main === 'activity' ? 52 : column.width ?? 240) * scale)}px`
  const gap = layout.style.gap * scale
  return <div className="lp-grid" style={{ gridTemplateColumns: columns.map(track).join(' '), gap, padding: gap, '--p-radius': `${Math.max(1, layout.style.radius * scale)}px` } as CSSProperties}>
    {columns.map((column, at) => <div key={at} className="lp-column">
      {column.top && <Panel name={column.top} layout={layout} />}
      <Panel name={column.main} layout={layout} />
      {column.bottom && <Panel name={column.bottom} layout={layout} />}
    </div>)}
  </div>
}

export function LayoutPreview({ layout, device }: { layout: Layout; device: Device }) {
  const size = SIZE[device]
  const phoneKeeps = device === 'phone' && layout.phone === 'keep'
  const glass = layout.style.surface === 'glass' && !phoneKeeps
  const attrs = phoneKeeps ? {} : { 'data-v-density': layout.variants?.density, 'data-v-type': layout.variants?.type }
  return <figure className="lp" data-device={device}>
    <div className={`lp-screen${glass ? ' lp-glass' : ''}`} {...attrs} style={{ ...colours(layout, !phoneKeeps), width: size.frame, height: Math.round(size.frame * size.height / size.width) }} aria-hidden="true">
      {device === 'phone' ? <Chat layout={phoneKeeps ? { ...layout, variants: undefined } : layout} phone /> : <Wide layout={layout} device={device} />}
    </div>
    <figcaption>{size.label}{phoneKeeps ? ': keeps its own look' : device === 'phone' ? ': colours and style' : ''}</figcaption>
  </figure>
}

export function LayoutPreviews({ layout }: { layout: Layout }) {
  return <div className="lp-row-devices" role="img" aria-label={`Preview of ${layout.name} on a desktop, a tablet and a phone`}>
    {(['desktop', 'tablet', 'phone'] as const).map(device => <LayoutPreview key={device} layout={layout} device={device} />)}
  </div>
}
