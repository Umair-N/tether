// PDF pages in the Share preview pane. Each page draws when it scrolls near view, at the width of the pane.
import { useEffect, useRef, useState } from 'react'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import { drawPage, openPdf } from './sharePdf'

function Page({ doc, n, width, ratio }: { doc: PDFDocumentProxy; n: number; width: number; ratio: number }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const [near, setNear] = useState(false)
  useEffect(() => {
    const target = canvas.current
    if (!target) return
    const observer = new IntersectionObserver(entries => { if (entries[0].isIntersecting) setNear(true) }, { rootMargin: '600px 0px' })
    observer.observe(target)
    return () => observer.disconnect()
  }, [])
  useEffect(() => {
    if (!near || !width || !canvas.current) return
    let draw: ReturnType<typeof drawPage> | null = null, live = true
    void doc.getPage(n).then(page => { if (live && canvas.current) { draw = drawPage(page, canvas.current, width); draw.done.catch(() => {}) } })
    return () => { live = false; draw?.cancel() }
  }, [doc, n, width, near])
  return <canvas ref={canvas} className="sh-pdf-page" style={{ aspectRatio: `auto ${ratio}` }} aria-label={`Page ${n}`} role="img" />
}

export function PdfView({ url, name }: { url: string; name: string }) {
  const box = useRef<HTMLDivElement>(null)
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null)
  const [ratio, setRatio] = useState(0.7727)
  const [width, setWidth] = useState(0)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let live = true, opened: PDFDocumentProxy | null = null
    openPdf(url).then(async pdf => {
      opened = pdf
      const first = (await pdf.getPage(1)).getViewport({ scale: 1 })
      if (!live) return
      setRatio(first.width / first.height); setDoc(pdf)
    }).catch(() => { if (live) setFailed(true) })
    return () => { live = false; void opened?.loadingTask.destroy() }
  }, [url])
  useEffect(() => {
    const target = box.current
    if (!target) return
    let t = 0
    const observer = new ResizeObserver(([entry]) => {
      clearTimeout(t)
      t = window.setTimeout(() => setWidth(Math.floor(entry.contentRect.width)), width ? 150 : 0)
    })
    observer.observe(target)
    return () => { observer.disconnect(); clearTimeout(t) }
  }, [width])
  if (failed) return <div className="sh-view-none"><span>This PDF could not be shown here. Use Download, or open it in a new tab.</span></div>
  return <div ref={box} className="sh-pdf" tabIndex={0} aria-label={`Pages of ${name}`} aria-busy={!doc}>
    {doc ? Array.from({ length: doc.numPages }, (_, i) => <Page key={i} doc={doc} n={i + 1} width={width} ratio={ratio} />)
      : <div className="sh-view-none">Loading…</div>}
    {doc && <p className="sh-pdf-count">{doc.numPages === 1 ? '1 page' : `${doc.numPages} pages`}</p>}
  </div>
}
