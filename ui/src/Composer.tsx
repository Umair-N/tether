import { history, loadHistory, remember } from './history'
import { chatTitle } from './Sidebar'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { ArrowUp, Blocks, Camera, CornerDownLeft, FolderOpen, Smartphone, Image, Mic, Paperclip, Plus, Square, X, AtSign, Slash } from 'lucide-react'
import { errorText } from './api'
import type { Api, Pane } from './api'
import { takeShared, doneShared, releaseShared } from './shared'
import { canPickFolder, forgetFolder, latestScreenshot } from './screenshot'
import { Suggestions } from './Suggest'
import type { SuggestHandle } from './Suggest'
import { ImageViewer } from './ImageViewer'
import { playChime, prepareChime } from './sound'
import { stopSpeaking } from './speech'
import { useHost } from './host'












async function prepareFile(file: File): Promise<{ blob: Blob; name: string }> {
  const kind = file.type
  if (!kind.startsWith('image/') || kind === 'image/gif' || kind === 'image/svg+xml') return { blob: file, name: file.name || 'file' }
  const isPng = kind === 'image/png'
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
    const scale = Math.min(1, 2400 / Math.max(bitmap.width, bitmap.height))
    if (isPng && scale === 1 && file.size <= 4 * 1024 * 1024) { bitmap.close(); return { blob: file, name: file.name || 'screenshot.png' } }
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale)
    canvas.getContext('2d')?.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
    bitmap.close()
    const type = isPng ? 'image/png' : 'image/jpeg'
    const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, type, 0.9))
    if (!blob) throw new Error('encode failed')
    return { blob, name: `${(file.name || 'photo').replace(/\.[^.]*$/, '') || 'photo'}${isPng ? '.png' : '.jpg'}` }
  } catch { return { blob: file, name: file.name || 'file' } }
}
async function uploadFile(file: File, session: string, thread: string | null): Promise<{ path: string; name: string }> {
  const { blob, name } = await prepareFile(file)
  const response = await fetch('/mobile/api/upload', { method: 'POST', body: blob, credentials: 'same-origin', headers: {
    'X-Codex-Mobile': '1', 'X-Session': session, 'X-Thread': thread ?? '', 'X-Filename': encodeURIComponent(name), 'Content-Type': blob.type || 'application/octet-stream' } })
  if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Sign in again before you attach files.')
  const data = await response.json() as { path?: string; name?: string; error?: string }
  if (!response.ok || !data.path) throw new Error(data.error || 'Could not attach that file.')
  return { path: data.path, name: name }
}

interface ComposerProps { api: Api; pane: Pane; thread: string | null; disabled: boolean; onSend: (text: string) => Promise<boolean>; error: string; errorAction?: React.ReactNode; draft: string; setDraft: (text: string) => void; working: boolean; onStop: () => void; micOk: boolean; hint?: string; chime: boolean; options?: React.ReactNode; onTools: () => void; handsFree: boolean; listenRequest: number; sent?: string[] }
export function Composer({ api, pane, thread, disabled, onSend, error, errorAction, draft, setDraft, working, onStop, micOk, chime, options, onTools, hint, handsFree, listenRequest, sent = [] }: ComposerProps) {
  const host = useHost()
  const [menu, setMenu] = useState(false)
  const hintId = useId()
  const camera = useRef<HTMLInputElement>(null)
  const photos = useRef<HTMLInputElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!menu) return
    const dismiss = (event: MouseEvent) => { if (!menuRef.current?.contains(event.target as Node)) setMenu(false) }
    document.addEventListener('click', dismiss)
    return () => document.removeEventListener('click', dismiss)
  }, [menu])
  // Start a "/" or "@" suggestion from the menu: "/" only works at the start, "@" goes after the text.
  function insertTrigger(mark: '@' | '/') {
    setMenu(false)
    setDraft(mark === '/' ? '/' + draft.replace(/^\//, '') : draft + (draft && !/\s$/.test(draft) ? ' ' : '') + '@')
    requestAnimationFrame(() => { const box = textarea.current; if (box) { box.focus(); box.setSelectionRange(mark === '/' ? 1 : box.value.length, mark === '/' ? 1 : box.value.length) } })
  }
  // Gboard's image history cannot reach a web text box, so read the newest file in the Screenshots folder instead.
  const [hasFolder, setHasFolder] = useState(() => { try { return localStorage.getItem('tether-screenshot-folder') === '1' } catch { return false } })
  async function attachLatest() {
    setMenu(false)
    if (!canPickFolder()) { photos.current?.click(); return }
    try {
      const file = await latestScreenshot()
      if (!file) return
      try { localStorage.setItem('tether-screenshot-folder', '1') } catch { /* private mode */ }
      setHasFolder(true)
      await attach([file])
    } catch (failure) { setSpeechError(errorText(failure)) }
  }
  async function changeFolder() {
    await forgetFolder()
    try { localStorage.removeItem('tether-screenshot-folder') } catch { /* private mode */ }
    setHasFolder(false)
    await attachLatest()
  }
  const [files, setFiles] = useState<{ name: string; path: string; url?: string }[]>([])
  const [viewing, setViewing] = useState<{ name: string; url: string } | null>(null)
  const [uploading, setUploading] = useState('')
  const picker = useRef<HTMLInputElement>(null)
  const suggest = useRef<SuggestHandle>(null)
  // A line that starts with "!" runs as a shell command in the agent's terminal on the server. The box shows it as a terminal prompt.
  const shellMode = pane.agent !== 'shell' && draft.startsWith('!')
  const canAttach = pane.agent !== 'shell' && pane.writable && !shellMode
  useEffect(() => { setFiles([]) }, [pane.id, thread])
  // Attach files shared from another app (Gallery → Share → Tether) once a session that takes files is open.
  // Wait until the chat has loaded (disabled is false), so the upload carries the current thread.
  useEffect(() => { if (canAttach && !disabled) void takeShared().then(async shared => { if (!shared.length) return; const saved = await attach(shared.map(item => item.file), index => void doneShared(shared[index])); releaseShared(shared.slice(saved)) }) }, [canAttach, disabled, pane.id, thread]) // eslint-disable-line react-hooks/exhaustive-deps
  // Images from a side panel (the browser panel's "Send to chat") arrive as a 'tether-attach' event for this pane.
  // Cancel the event so the panel knows a composer took the files.
  useEffect(() => {
    const take = (event: Event) => { const detail = (event as CustomEvent<{ pane: string; files: File[] }>).detail; if (detail?.pane !== pane.id || !canAttach || disabled) return; event.preventDefault(); void attach(detail.files) }
    window.addEventListener('tether-attach', take)
    return () => window.removeEventListener('tether-attach', take)
  }, [canAttach, disabled, pane.id, thread]) // eslint-disable-line react-hooks/exhaustive-deps
  // Upload files in order and return how many worked. onSaved runs after each good upload.
  async function attach(list: FileList | File[], onSaved?: (index: number) => void): Promise<number> {
    const chosen = Array.from(list)
    if (!chosen.length) return 0
    setSpeechError('')
    let count = 0
    try {
      for (const [index, file] of chosen.entries()) {
        setUploading(`Uploading ${chosen.length > 1 ? `${index + 1}/${chosen.length} ` : ''}${file.name || 'file'}…`)
        const saved = await uploadFile(file, pane.id, thread)
        setFiles(current => [...current, { ...saved, url: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined }])
        onSaved?.(index); count++
      }
    } catch (failure) { setSpeechError(errorText(failure)) } finally { setUploading('') }
    textarea.current?.focus()
    return count
  }
  // Voice: record here, turn it into text on the server (/mobile/api/transcribe). Chrome's own speech service
  // ends with no text on some Android phones, so Tether does not use it.
  const [listening, setListening] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [seconds, setSeconds] = useState(0)
  const [speechError, setSpeechError] = useState('')
  const recorder = useRef<MediaRecorder | null>(null)
  // The draft after the final (accurate) text lands. Send waits for it, so it never sends the rough live text.
  const finalDraft = useRef<Promise<string> | null>(null)
  // Stop button while the final text is on its way: cancel it and keep the live text.
  const converting = useRef<AbortController | null>(null)
  const draftNow = useRef(draft)
  draftNow.current = draft
  const textarea = useRef<HTMLTextAreaElement>(null)
  const [sending, setSending] = useState(false)
  useEffect(() => () => { const active = recorder.current; if (active) { active.onstop = null; if (active.state !== 'inactive') active.stop(); active.stream.getTracks().forEach(track => track.stop()) } }, [])
  useLayoutEffect(() => {
    const input = textarea.current
    if (input) { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 180)}px` }
  }, [draft])
  // Hands-free: a read-aloud reply asks for the mic (listenRequest). A pause after speech stops it and sends.
  const form = useRef<HTMLFormElement>(null)
  const toggleRef = useRef(toggleMic)
  toggleRef.current = toggleMic
  useEffect(() => { if (listenRequest && !recorder.current && micOk) void toggleRef.current() }, [listenRequest, micOk])
  async function toggleMic() {
    if (recorder.current) { if (recorder.current.state !== 'inactive') recorder.current.stop(); return }
    stopSpeaking()
    setSpeechError('')
    if (chime) { prepareChime(); await playChime() }
    let stream: MediaStream
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }) } catch (failure) {
      setSpeechError(failure instanceof DOMException && failure.name === 'NotAllowedError' ? 'Allow microphone access for this site, then try again.' : 'Could not start the microphone. Close other apps that use it, then try again.')
      return
    }
    const type = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'].find(kind => MediaRecorder.isTypeSupported(kind))
    const active = new MediaRecorder(stream, type ? { mimeType: type } : undefined)
    const chunks: Blob[] = []
    const started = Date.now()
    // Live text: about every 1.5 s, send the audio so far to the fast model and show its rough text in the box.
    // The accurate model replaces it after stop.
    const base = draftNow.current.trimEnd()
    // One id per recording: the server converts finished sentences while the user speaks, so stop only waits for the rest.
    const recording = crypto.randomUUID?.() ?? `${started}-${Math.random().toString(36).slice(2)}`
    let finish: (text: string) => void = () => undefined
    const final = new Promise<string>(resolve => { finish = resolve })
    finalDraft.current = final
    void final.then(() => { if (finalDraft.current === final) finalDraft.current = null })
    const show = (text: string) => setDraft(`${base}${base && text ? ' ' : ''}${text}`)
    let asking = false, askedAt = 0, done = false
    const askLive = async () => {
      // Wait for 2 s of audio, then ask at most every 1.5 s. Cancel a slow request so the next one can go.
      if (asking || done || Date.now() - started < 2000 || Date.now() - askedAt < 1500) return
      asking = true; askedAt = Date.now()
      const slow = new AbortController(), cancel = window.setTimeout(() => slow.abort(), 8000)
      try {
        const audio = new Blob(chunks, { type: active.mimeType || type || 'audio/webm' })
        const response = await fetch('/mobile/api/transcribe', { method: 'POST', body: audio, credentials: 'same-origin', signal: slow.signal, headers: { 'X-Codex-Mobile': '1', 'X-Live': '1', 'X-Recording': recording, 'Content-Type': audio.type } })
        const data = response.ok ? await response.json() as { text?: string } : {}
        if (!done && data.text) show(data.text.trim())
      } catch { /* live text is a preview; the final pass still runs */ } finally { window.clearTimeout(cancel); asking = false }
    }
    const timer = window.setInterval(() => { const elapsed = Math.floor((Date.now() - started) / 1000); setSeconds(elapsed); if (elapsed >= 180 && active.state !== 'inactive') active.stop() }, 250)
    active.ondataavailable = event => { if (event.data.size) { chunks.push(event.data); if (active.state === 'recording') void askLive() } }
    active.onstop = () => {
      done = true
      window.clearInterval(timer); stream.getTracks().forEach(track => track.stop()); recorder.current = null; setListening(false)
      const heard = pause?.stop() ?? true
      const audio = new Blob(chunks, { type: active.mimeType || type || 'audio/webm' })
      if (!heard) { setDraft(base); finish(base); return }
      if (Date.now() - started < 600 || !audio.size) { setDraft(base); setSpeechError('Tap the mic, speak, then tap stop.'); finish(base); return }
      void transcribeAudio(audio, base, recording).then(text => {
        finish(text)
        // Send at once; submit still waits for this final text.
        if (pause && text.trim() && text !== base) form.current?.requestSubmit()
      })
    }
    const pause = handsFree ? watchForPause(stream, () => { if (active.state !== 'inactive') active.stop() }) : null
    recorder.current = active
    active.start(1000); setSeconds(0); setListening(true)
  }
  // Returns the new draft.
  async function transcribeAudio(audio: Blob, base: string, recording: string): Promise<string> {
    setTranscribing(true)
    const cancel = new AbortController()
    converting.current = cancel
    try {
      const response = await fetch('/mobile/api/transcribe', { method: 'POST', body: audio, credentials: 'same-origin', signal: cancel.signal, headers: { 'X-Codex-Mobile': '1', 'X-Recording': recording, 'Content-Type': audio.type } })
      if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Sign in again, then try the mic.')
      const data = await response.json() as { text?: string; error?: string }
      if (!response.ok) throw new Error(data.error || 'Voice-to-text failed.')
      const text = (data.text ?? '').trim()
      if (!text) { setDraft(base); setSpeechError('No speech heard. Tap the mic and speak.'); return base }
      const next = `${base}${base ? ' ' : ''}${text}`
      setDraft(next)
      return next
    } catch (failure) {
      if (!cancel.signal.aborted) setSpeechError(errorText(failure))
      return draftNow.current
    } finally { converting.current = null; setTranscribing(false); textarea.current?.focus() }
  }
  const canRecord = typeof MediaRecorder !== 'undefined' && !!navigator.mediaDevices?.getUserMedia
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    const waiting = finalDraft.current
    if (disabled || sending || uploading || (!draft.trim() && !files.length && !waiting) || (shellMode && !draft.slice(1).trim())) return
    if (recorder.current?.state === 'recording') recorder.current.stop()
    const typing = document.activeElement === textarea.current
    setSending(true)
    try {
      const body = waiting ? (await waiting).trim() : draft.trim()
      const text = [...files.map(file => file.path), body].filter(Boolean).join(' ')
      if (text && await onSend(text)) { if (body) remember(historyKind, body); browse.current = null; setDraft(''); setFiles([]); if (coarse?.matches) navigator.vibrate?.(10) }
    } finally { setSending(false); if (typing) requestAnimationFrame(() => textarea.current?.focus()) }
  }
  // Up on the first line goes to the previous sent prompt; Down on the last line goes forward, then back to the draft.
  // The list: this session's own prompts from the chat (any device, older ones too), then recent sends from this browser
  // that the chat does not show (slash commands, shell lines).
  const historyKind = pane.id
  const browse = useRef<{ at: number; list: string[]; draft: string } | null>(null)
  function walk(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.nativeEvent.isComposing) return false
    const box = event.currentTarget, start = box.selectionStart, end = box.selectionEnd
    if (start !== end || box.readOnly) return false
    if (event.key === 'ArrowUp' && !draft.slice(0, start).includes('\n')) {
      const state = browse.current ?? { at: -1, list: history(sent, loadHistory(historyKind)), draft }
      const at = state.at === -1 ? state.list.length - 1 : state.at - 1
      if (at < 0 || !state.list.length) return false
      browse.current = { ...state, at }
      setDraft(state.list[at])
      requestAnimationFrame(() => textarea.current?.setSelectionRange(0, 0))
      return true
    }
    if (event.key === 'ArrowDown' && browse.current && !draft.slice(end).includes('\n')) {
      const state = browse.current, at = state.at + 1
      const text = at < state.list.length ? state.list[at] : state.draft
      browse.current = at < state.list.length ? { ...state, at } : null
      setDraft(text)
      requestAnimationFrame(() => textarea.current?.setSelectionRange(text.length, text.length))
      return true
    }
    return false
  }
  // A tap on a button in the composer keeps the focus in the text box. Without this, on a phone the tap first closes
  // the keyboard, the layout jumps, and the tap misses the button. On a desktop the box keeps the caret.
  function keepFocus(event: React.MouseEvent) {
    if (document.activeElement === textarea.current && event.target !== textarea.current && (event.target as HTMLElement).closest('button')) event.preventDefault()
  }
  return <div className="composer-area"><Suggestions ref={suggest} api={api} pane={pane} draft={draft} setDraft={setDraft} focus={() => textarea.current?.focus()} /><form className={shellMode ? 'composer shell-mode' : 'composer'} ref={form} onSubmit={submit} onMouseDown={keepFocus}>
    {files.length > 0 && <ul className="attachments" aria-label="Attached files">{files.map((file, index) => <li key={file.path} className="chip">{file.url ? <button type="button" className="chip-view" aria-label={`View ${file.name}`} onClick={() => setViewing({ name: file.name, url: file.url! })}><img src={file.url} alt="" /><span>{file.name}</span></button> : <><Paperclip size={13} /><span>{file.name}</span></>}<button type="button" aria-label={`Remove ${file.name}`} onClick={() => setFiles(current => current.filter((_, at) => at !== index))}><X size={14} /></button></li>)}</ul>}
    <ImageViewer image={viewing} close={() => setViewing(null)} />
    <div className="composer-input">{shellMode && <span className="shell-prompt" title={pane.dir ? `Runs in ${pane.dir}` : undefined}><span className="shell-folder">{shortFolder(pane.dir)}</span><span aria-hidden="true">$</span></span>}
    <textarea className={shellMode ? 'shell-input' : undefined} onKeyDown={event => {
      if (browse.current && walk(event)) event.preventDefault()
      else if (suggest.current?.key(event)) event.preventDefault()
      else if (walk(event)) event.preventDefault()
      else if (shellMode && event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setDraft(draft.slice(1)) }
      else if (shellMode && event.key === 'Backspace' && !draft.slice(1).trim() && event.currentTarget.selectionEnd <= 1) { event.preventDefault(); setDraft(draft.slice(1)) }
      else if (sendsOnEnter(event)) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} onPaste={event => { if (canAttach && event.clipboardData.files.length) { event.preventDefault(); void attach(event.clipboardData.files) } }} ref={textarea} aria-label={pane.agent === 'shell' ? 'Run a command' : shellMode ? 'Shell command' : `Message ${chatTitle(pane)}`} aria-describedby={shellMode ? hintId : undefined} spellCheck={!shellMode} autoCapitalize={shellMode ? 'off' : undefined} autoCorrect={shellMode ? 'off' : undefined} placeholder={hint ?? (pane.agent === 'shell' ? 'Run a command' : `Message ${chatTitle(pane)}`)} rows={1} value={draft} readOnly={listening || transcribing || sending} disabled={disabled} onChange={event => setDraft(event.target.value)} /></div>
    <div className="composer-controls"><div>{canAttach && <div className="attach-container" ref={menuRef}>
      <input ref={picker} type="file" multiple hidden onChange={event => { const chosen = Array.from(event.target.files ?? []); event.target.value = ''; void attach(chosen) }} />
      <input ref={photos} type="file" accept="image/*" hidden onChange={event => { const chosen = Array.from(event.target.files ?? []); event.target.value = ''; void attach(chosen) }} />
      <input ref={camera} type="file" accept="image/*" capture="environment" hidden onChange={event => { const chosen = Array.from(event.target.files ?? []); event.target.value = ''; void attach(chosen) }} />
      <button type="button" className="icon-button attach" aria-label="Add photos and files" aria-expanded={menu} disabled={disabled || sending || !!uploading} onClick={() => setMenu(!menu)}><Plus size={21} /></button>
      {menu && <div className="attach-menu" role="menu">
        <button role="menuitem" type="button" onClick={() => { setMenu(false); camera.current?.click() }}><Camera size={18} />Camera</button>
        <button role="menuitem" type="button" onClick={() => { setMenu(false); picker.current?.click() }}><Image size={18} />Photos & files</button>
        <button role="menuitem" type="button" onClick={() => void attachLatest()}><Smartphone size={18} />Latest screenshot</button>
        {hasFolder && <button role="menuitem" type="button" onClick={() => void changeFolder()}><FolderOpen size={18} />Change screenshot folder</button>}
        {pane.agent !== 'shell' && <>
          <button role="menuitem" type="button" onClick={() => insertTrigger('@')}><AtSign size={18} />Mention a file</button>
          <button role="menuitem" type="button" onClick={() => insertTrigger('/')}><Slash size={18} />Commands & skills</button>
          <button role="menuitem" type="button" onClick={() => { setMenu(false); onTools() }}><Blocks size={18} />Plugins & MCP</button>
        </>}
      </div>}
    </div>}{!uploading && !listening && !transcribing && options}<span className="composer-status" aria-live="polite">{uploading || (listening ? `Recording ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}` : transcribing ? 'Converting…' : pane.agent === 'shell' ? 'Terminal input' : shellMode ? 'Shell mode' : '')}</span>{!draft && !files.length && pane.agent !== 'shell' && !listening && !transcribing && !uploading && <span className="composer-keys" aria-hidden="true"><span><kbd>↵</kbd> send</span><span><kbd>⇧↵</kbd> new line</span><span><kbd>/</kbd> commands</span><span><kbd>!</kbd> shell</span></span>}</div><div>
      {micOk && canRecord && (transcribing
        ? <button type="button" className="icon-button mic active" aria-label="Stop converting and keep the live text" onClick={() => converting.current?.abort()}><X size={19} /></button>
        : <button type="button" className={`icon-button mic ${listening ? 'active' : ''}`} aria-label={listening ? 'Stop recording' : 'Start voice input'} aria-pressed={listening} disabled={disabled || sending} onClick={() => void toggleMic()}>{listening ? <Square size={18} /> : <Mic size={20} />}</button>)}
      {working && !draft.trim() && !files.length
        ? <button type="button" className="primary send-button stop-button" aria-label={pane.agent === 'shell' ? 'Stop command (Ctrl+C)' : 'Stop response (Esc)'} disabled={disabled} onClick={onStop}><Square size={15} fill="currentColor" /></button>
        : shellMode
          ? <button className="primary send-button run-button" aria-label="Run command" disabled={disabled || sending || !!uploading || !draft.slice(1).trim()}><CornerDownLeft size={16} />Run</button>
          : <button className="primary send-button" aria-label="Send message" disabled={disabled || sending || !!uploading || (!draft.trim() && !files.length)}><ArrowUp size={21} /></button>}
    </div></div>
  </form>{shellMode && <p className="shell-hint" id={hintId}>Runs on {host} · Esc or Backspace on empty leaves shell mode</p>}{(error || speechError) && <p className="composer-error error" role="alert">{error || speechError}{error && errorAction}</p>}</div>
}

// "~/apps/tether/src/panels" → "…/src/panels": the end of the path names where you are.
function shortFolder(dir: string) {
  const parts = (dir || '~').split('/')
  return parts.length > 3 ? `…/${parts.slice(-2).join('/')}` : dir || '~'
}

// Desktop (a mouse or trackpad): Enter sends, Shift+Enter adds a line. Phone soft keyboard: Enter adds a line, as before.
// Ctrl/Cmd+Enter sends everywhere. An open IME composition (Chinese, Japanese input) keeps its Enter.
const desktop = typeof matchMedia === 'function' ? matchMedia('(hover: hover) and (pointer: fine)') : null
const coarse = typeof matchMedia === 'function' ? matchMedia('(pointer: coarse)') : null  // a phone: a light tap on send
function sendsOnEnter(event: React.KeyboardEvent) {
  if (event.key !== 'Enter' || event.nativeEvent.isComposing || event.shiftKey || event.altKey) return false
  return event.ctrlKey || event.metaKey || !!desktop?.matches
}

// Listen to the mic level. After speech, 1.8 s of quiet calls done. No speech in the first 8 s also calls done.
// stop() returns whether any speech was heard.
function watchForPause(stream: MediaStream, done: () => void) {
  const Context = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!Context) return null
  const context = new Context()
  const analyser = context.createAnalyser()
  analyser.fftSize = 1024
  context.createMediaStreamSource(stream).connect(analyser)
  const samples = new Float32Array(analyser.fftSize)
  const started = Date.now()
  let heard = false, quietSince = Date.now(), floor = 0.01
  const timer = window.setInterval(() => {
    analyser.getFloatTimeDomainData(samples)
    const level = Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length)
    // The room noise level, learned in the first half second.
    if (Date.now() - started < 500) { floor = Math.max(floor, level); return }
    const speech = level > Math.max(0.02, floor * 2.5)
    if (speech) { heard = true; quietSince = Date.now() }
    if (heard ? Date.now() - quietSince > 1800 : Date.now() - started > 8000) done()
  }, 100)
  return { stop: () => { window.clearInterval(timer); void context.close(); return heard } }
}
