// One WebSocket per browser panel stream (server/browser_live.py): pictures come in, input and acks go out.
// Each server message is binary: u32 big-endian header length, a JSON header, then the payload bytes.
// With codec=h264 the server sends H.264 (config + video) and one sharp JPEG ("still") when the page stops
// changing. Otherwise it sends JPEG frames. Pictures reach the panel as ImageBitmaps, newest first: a picture
// that finishes decoding after a newer one was shown is dropped.

export interface LiveMeta { type: string; seq?: number; width: number; height: number; scrollOffsetX?: number; scrollOffsetY?: number; key?: boolean; dup?: boolean }
export interface LiveHandlers {
  onPicture: (bitmap: ImageBitmap, meta: LiveMeta) => void
  onTabs: (state: unknown) => void
  onClosed: () => void
  onOpen: () => void
  onError: (text: string) => void
  // The socket ended without close(). opened: it connected first (false: the server has no live route, or the
  // network failed before the upgrade).
  onEnd: (opened: boolean) => void
}
export interface LiveStream { ready: () => boolean; input: (events: unknown[]) => void; close: () => void }

// Set when this browser cannot decode the server's H.264. Later streams in this page ask for JPEG.
let h264Failed = false
export function canH264() { return !h264Failed && typeof VideoDecoder === 'function' && typeof EncodedVideoChunk === 'function' }

export function openLive(url: string, handlers: LiveHandlers): LiveStream {
  const socket = new WebSocket(`${url}&codec=${canH264() ? 'h264' : 'jpeg'}`)
  socket.binaryType = 'arraybuffer'
  let ended = false, opened = false
  let order = 0, shown = 0
  let decoder: VideoDecoder | null = null
  let stamp = 0
  const metas = new Map<number, { meta: LiveMeta; at: number }>()
  const text = new TextDecoder()

  const send = (data: object) => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(data)) }
  const ack = (meta: LiveMeta) => { if (typeof meta.seq === 'number') send({ type: 'ack', seq: meta.seq }) }
  function show(bitmap: ImageBitmap, meta: LiveMeta, at: number) {
    if (ended || at < shown) { bitmap.close(); return }
    shown = at
    handlers.onPicture(bitmap, meta)
  }
  function jpeg(payload: Uint8Array, meta: LiveMeta) {
    const at = ++order
    createImageBitmap(new Blob([payload as BlobPart], { type: 'image/jpeg' })).then(bitmap => show(bitmap, meta, at), () => undefined)
  }
  function stopDecoder() {
    if (decoder && decoder.state !== 'closed') decoder.close()
    decoder = null
    metas.clear()
  }
  // The decoder failed: drop the socket; the panel connects again and asks for JPEG.
  function failVideo() {
    if (ended) return
    h264Failed = true
    stopDecoder()
    socket.close()
  }
  function output(frame: VideoFrame) {
    const entry = metas.get(frame.timestamp)
    metas.delete(frame.timestamp)
    if (!entry || ended) { frame.close(); return }
    createImageBitmap(frame).then(bitmap => { frame.close(); show(bitmap, entry.meta, entry.at) }, () => frame.close())
  }
  function configure(header: LiveMeta & { codec?: string }, payload: Uint8Array) {
    try {
      if (!decoder || decoder.state === 'closed') decoder = new VideoDecoder({ output, error: failVideo })
      metas.clear()
      decoder.configure({ codec: String(header.codec), description: payload, codedWidth: header.width, codedHeight: header.height, optimizeForLatency: true })
    } catch { failVideo() }
  }
  function video(header: LiveMeta, payload: Uint8Array) {
    if (!header.dup) ack(header)
    if (!decoder || decoder.state !== 'configured') return
    const timestamp = ++stamp
    metas.set(timestamp, { meta: header, at: ++order })
    if (metas.size > 64) metas.delete(metas.keys().next().value as number)
    try { decoder.decode(new EncodedVideoChunk({ type: header.key ? 'key' : 'delta', timestamp, data: payload })) } catch { failVideo() }
  }

  socket.onopen = () => { opened = true; handlers.onOpen() }
  socket.onmessage = event => {
    if (ended || !(event.data instanceof ArrayBuffer)) return
    const data = event.data
    let header: LiveMeta & Record<string, unknown>, payload: Uint8Array
    try {
      const length = new DataView(data).getUint32(0)
      header = JSON.parse(text.decode(new Uint8Array(data, 4, length)))
      payload = new Uint8Array(data, 4 + length)
    } catch { return }
    switch (header.type) {
      case 'frame': case 'still': ack(header); jpeg(payload, header); break
      case 'config': configure(header, payload); break
      case 'video': video(header, payload); break
      case 'tabs': handlers.onTabs(header.data ?? header); break
      case 'closed': handlers.onClosed(); break
      case 'error': handlers.onError(String(header.error ?? 'Browser error')); break
      case 'hello': if (header.codec !== 'h264') stopDecoder(); break
    }
  }
  socket.onclose = () => {
    if (ended) return
    ended = true
    stopDecoder()
    handlers.onEnd(opened)
  }
  return {
    ready: () => !ended && socket.readyState === WebSocket.OPEN,
    input: events => send({ type: 'input', events }),
    close: () => { ended = true; stopDecoder(); socket.close() },
  }
}
