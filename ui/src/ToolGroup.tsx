import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { Bot, ChevronRight, CircleAlert, Code2, Eye, Globe, ListTodo, Search, SquarePen, Terminal, Wrench } from 'lucide-react'
import { errorText } from './api'
import type { Api, ToolSummary, ToolDetail } from './api'
import { ImageViewer } from './ImageViewer'

// Tool icons and the live-edge detail scroller follow T3 Code's TimelineToolActivityIcon and WorkLogDetails
// (github.com/pingdotgg/t3code, MIT License, Copyright (c) 2026 T3 Tools Inc.).
function ToolIcon({ name, failed }: { name: string; failed: boolean }) {
  const key = name.toLowerCase()
  const Icon = failed ? CircleAlert
    : /bash|shell|exec|command|run/.test(key) ? Terminal
    : /edit|write|patch|notebook/.test(key) ? SquarePen
    : /^read|view|open/.test(key) ? Eye
    : /grep|glob|search|find|ls\b/.test(key) && !/web/.test(key) ? Search
    : /web|fetch|browser|url/.test(key) ? Globe
    : /task|agent/.test(key) && !/todo/.test(key) ? Bot
    : /todo|plan/.test(key) ? ListTodo
    : Wrench
  return <Icon className={failed ? 'tool-icon failed' : 'tool-icon'} size={15} aria-hidden="true" />
}

// Long output keeps the newest line in view while you stay at the bottom; scroll up to read and it stays put.
function LiveDetail({ children, label }: { children: React.ReactNode; label: string }) {
  const ref = useRef<HTMLPreElement>(null)
  const atEdge = useRef(true)
  useLayoutEffect(() => { const box = ref.current; if (box && atEdge.current) box.scrollTop = box.scrollHeight })
  return <pre ref={ref} tabIndex={0} aria-label={label} onScroll={event => { const box = event.currentTarget; atEdge.current = box.scrollHeight - box.scrollTop - box.clientHeight <= 4 }}>{children}</pre>
}

function ToolRow({ tool, api, session }: { tool: ToolSummary; api: Api; session: string }) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<ToolDetail | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const id = useId()
  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    setError('')
    void api<ToolDetail>(`tool?session=${encodeURIComponent(session)}&id=${encodeURIComponent(tool.id)}`, undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) setDetail(value) }).catch(reason => { if (!controller.signal.aborted) setError(errorText(reason)) })
    return () => controller.abort()
  }, [open, api, session, tool.id, tool.is_error, tool.status, attempt])
  return <li className="tool-item">
    <button type="button" className="tool-toggle" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
      <ToolIcon name={tool.name} failed={!!(tool.is_error || detail?.is_error)} /><span><strong>{tool.name}</strong>{tool.title && <span className="tool-title">{tool.title}</span>}</span>
      {(tool.is_error || detail?.is_error) && <span className="tool-error">Error</span>}<ChevronRight className="chevron" size={15} aria-hidden="true" />
    </button>
    {open && <div id={id}>
      {error ? <p role="alert">{error} <button type="button" onClick={() => setAttempt(attempt + 1)}>Retry</button></p> : !detail ? <p role="status">Loading tool detail…</p> : <>
        <LiveDetail label={`${tool.name} detail`}><code>{detail.detail.split('\n').map((line, index) => <span key={index} className={detail.format === 'diff' ? line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-remove' : '' : ''}>{line}{'\n'}</span>)}</code></LiveDetail>
        {detail.status === 'pending' && <p role="status">Result pending or unavailable.</p>}
        {detail.truncated && <p className="tool-cut">Detail truncated.</p>}
      </>}
    </div>}
  </li>
}

// Images the agent looked at stay visible under the folded tool group. They come from the transcript, not from a file path.
function ToolImages({ tools, session }: { tools: ToolSummary[]; session: string }) {
  const [viewing, setViewing] = useState<{ name: string; url: string } | null>(null)
  const shots = tools.flatMap(tool => Array.from({ length: tool.images ?? 0 }, (_, n) => ({
    name: tool.title.split('/').pop() || 'Image', url: `/mobile/api/tool-image?session=${encodeURIComponent(session)}&id=${encodeURIComponent(tool.id)}&n=${n}` })))
  if (!shots.length) return null
  return <>
    <ul className="tool-images" aria-label="Images">{shots.map(shot => <li key={shot.url}>
      <button type="button" aria-label={`View ${shot.name}`} onClick={() => setViewing(shot)}><img src={shot.url} alt="" loading="lazy" /><span>{shot.name}</span></button>
    </li>)}</ul>
    <ImageViewer image={viewing} close={() => setViewing(null)} />
  </>
}

export function ToolGroup({ id, text, count, tools, api, session }: { id: string; text: string; count?: unknown; tools?: ToolSummary[]; api: Api; session: string }) {
  const [open, setOpen] = useState(false)
  const panel = useId()
  const total = tools?.length || Number(count) || text.split('\n').filter(Boolean).length || 1
  return <div className="tool-row tool-group" data-msg-id={id}>
    <button type="button" className="tool-toggle" aria-expanded={open} aria-controls={panel} onClick={() => setOpen(!open)}><Code2 size={15} aria-hidden="true" />Used {total} {total === 1 ? 'tool' : 'tools'}<ChevronRight className="chevron" size={15} aria-hidden="true" /></button>
    {open && <div id={panel}>{tools?.length ? <ul className="tool-list">{tools.map((tool, index) => <ToolRow key={`${tool.id}:${index}`} tool={tool} api={api} session={session} />)}</ul> : <pre tabIndex={0}>{text}</pre>}</div>}
    {tools && <ToolImages tools={tools} session={session} />}
  </div>
}
