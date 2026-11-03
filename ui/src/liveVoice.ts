import { canSpeak, speakable } from './speech'
import type { MicVAD } from '@ricky0123/vad-web'

// Live mode audio: one open mic, pause detection, barge-in, and the natural voice (server Kokoro, /mobile/api/tts).
// If the voice server is not running, the browser voice reads the words instead.

export interface VoiceEvents {
  onPhrase: (audio: Blob) => void  // the person spoke, then paused
  onBargeIn: () => void            // the person talked over the voice
  onSpeech?: () => void            // speech began during an armed capture
}

const PAUSE_MS = 1100   // quiet after speech that ends a phrase
const MIN_SPEECH_MS = 400
const BARGE_AFTER = 600 // skips the echo of the first word
const BARGE_TICKS = 8   // 8 × 50 ms of steady sound
let warnedAboutVAD = false

interface RmsCapture {
  recorder: MediaRecorder
  accepted: boolean
  discard: boolean
  speechMs: number
}

/** Silero returns 16 kHz mono samples; Whisper accepts an ordinary PCM WAV. */
function wav(samples: Float32Array) {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)
  const text = (at: number, value: string) => {
    for (let i = 0; i < value.length; i++) view.setUint8(at + i, value.charCodeAt(i))
  }
  text(0, 'RIFF')
  view.setUint32(4, buffer.byteLength - 8, true)
  text(8, 'WAVE')
  text(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, 16000, true)
  view.setUint32(28, 32000, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  text(36, 'data')
  view.setUint32(40, samples.length * 2, true)
  samples.forEach((sample, i) => {
    const value = Math.max(-1, Math.min(1, sample))
    view.setInt16(44 + i * 2, Math.round(value * (value < 0 ? 32768 : 32767)), true)
  })
  return new Blob([buffer], { type: 'audio/wav' })
}

/** Sentences of about 180 characters or less, so the first one plays while the next one renders. */
export function sentences(markdown: string) {
  const text = speakable(markdown)
  return (text.match(/[^.!?\n]+[.!?]*\s*|\n+/g) ?? [text]).reduce<string[]>((all, piece) => {
    if (all.length && all[all.length - 1].length + piece.length < 180) all[all.length - 1] += piece
    else all.push(piece)
    return all
  }, []).map(part => part.trim()).filter(Boolean)
}

export class LiveVoice {
  readonly context: AudioContext
  listening = false
  speaking = false
  private isMuted = false
  private closed = false
  private opened = false
  private opening: Promise<void> | null = null
  private vad: MicVAD | null = null
  private vadWork: Promise<void> = Promise.resolve()
  private vadSuspended = false
  private fallback = false
  private speechInProgress = false
  private dropSegment = false
  private captureId = 0
  private finishing = false
  private stream: MediaStream | null = null
  private mic: AnalyserNode | null = null
  private out: AnalyserNode
  private samples = new Float32Array(1024)
  private floor = 0.01
  private timer = 0
  private capture: RmsCapture | null = null
  private heard = false
  private quietSince = 0
  private speakStart = 0
  private loud = 0
  private speakId = 0
  private source: AudioBufferSourceNode | null = null
  private ttsDown = false
  private events: VoiceEvents

  constructor(events: VoiceEvents) {
    this.events = events
    const Context = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    this.context = new Context()
    this.out = this.context.createAnalyser()
    this.out.fftSize = 1024
    this.out.connect(this.context.destination)
  }

  get muted() { return this.isMuted }
  set muted(value: boolean) {
    if (this.isMuted === value) return
    this.isMuted = value
    if (value) { this.vadSuspended = true; this.cancelListening(); this.speechInProgress = false }
    this.stream?.getAudioTracks().forEach(track => { track.enabled = !value })
    this.updateVad(async vad => {
      if (value) await vad.pause()
      else if (!this.muted && !this.closed) { await vad.start(); this.vadSuspended = false }
    })
  }

  /** Mic denial stays a DOMException; a model failure uses the RMS path. */
  open() {
    this.opening ??= this.openMic()
    return this.opening
  }

  private async openMic() {
    void this.context.resume()
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
    if (this.closed) { this.stream.getTracks().forEach(track => track.stop()); return }
    const stream = this.stream
    stream.getAudioTracks().forEach(track => { track.enabled = !this.muted })
    this.mic = this.context.createAnalyser()
    this.mic.fftSize = 1024
    this.context.createMediaStreamSource(this.stream).connect(this.mic)
    const started = performance.now()
    this.timer = window.setInterval(() => {
      const level = this.micLevel()
      if (performance.now() - started < 500) { this.floor = Math.max(this.floor, level); return }
      if (this.fallback) this.tick(level)
      else if (!this.speaking && !this.speechInProgress) this.followFloor(level)
    }, 50)
    try {
      const { MicVAD } = await import('@ricky0123/vad-web')
      const assetPath = `${import.meta.env.BASE_URL}vad/`
      this.vad = await MicVAD.new({
        model: 'v5',
        baseAssetPath: assetPath,
        onnxWASMBasePath: assetPath,
        redemptionMs: PAUSE_MS,
        minSpeechMs: MIN_SPEECH_MS,
        submitUserSpeechOnPause: true,
        startOnLoad: false,
        audioContext: this.context,
        getStream: async () => stream,
        // Pausing capture must not stop the shared mic or ask for permission again.
        pauseStream: async () => {},
        resumeStream: async () => stream,
        ortConfig: ort => { ort.env.wasm.numThreads = 1 },
        onSpeechStart: () => this.speechStart(),
        onSpeechEnd: audio => this.speechEnd(audio),
        onVADMisfire: () => {
          if (this.fallback) return
          this.speechInProgress = false
          this.heard = false
          this.dropSegment = false
        },
      })
      await this.vad.start()
      if (this.muted || this.closed) await this.vad.pause()
      else this.vadSuspended = false
    } catch (error) {
      this.useFallback(error)
    }
    this.opened = !this.closed
  }

  close() {
    if (this.closed) return
    this.closed = true
    this.opened = false
    window.clearInterval(this.timer)
    this.stopSpeaking()
    this.cancelListening()
    this.stream?.getTracks().forEach(track => track.stop())
    // Wait for model startup and queued pauses before releasing the audio context.
    void (async () => {
      await this.opening?.catch(() => {})
      await this.vadWork
      await this.vad?.destroy().catch(() => {})
      this.mic?.disconnect()
      await this.context.close()
    })()
  }

  private useFallback(error: unknown) {
    if (!warnedAboutVAD) { console.warn('Live VAD unavailable; using mic loudness detection.', error); warnedAboutVAD = true }
    this.fallback = true
    this.speechInProgress = false
    this.heard = false
    if (this.listening && !this.muted && !this.closed) this.startRmsCapture(true)
  }

  private updateVad(action: (vad: MicVAD) => Promise<void>) {
    this.vadWork = this.vadWork.then(async () => {
      if (this.vad && !this.fallback) await action(this.vad)
    }).catch(error => this.useFallback(error))
  }

  private speechStart() {
    if (this.fallback || this.closed || this.muted || this.vadSuspended) return
    this.speechInProgress = true
    this.dropSegment = !this.listening && !this.speaking
    if (this.listening) { this.heard = true; this.events.onSpeech?.() }
    else if (this.speaking && performance.now() - this.speakStart > BARGE_AFTER) this.events.onBargeIn()
  }

  private speechEnd(audio: Float32Array) {
    const accepted = this.listening && !this.dropSegment && !this.muted && !this.closed && !this.fallback && !this.vadSuspended
    this.speechInProgress = false
    this.heard = false
    this.dropSegment = false
    if (!accepted) return
    this.listening = false
    this.events.onPhrase(wav(audio))
  }

  private rms(analyser: AnalyserNode | null) {
    if (!analyser) return 0
    analyser.getFloatTimeDomainData(this.samples)
    return Math.sqrt(this.samples.reduce((sum, value) => sum + value * value, 0) / this.samples.length)
  }
  micLevel() { return this.muted ? 0 : this.rms(this.mic) }

  /** 0..1 for the orb: the voice while it speaks, else the mic. */
  level() {
    if (this.muted) return 0
    if (this.speaking) return Math.min(1, this.rms(this.out) * 5)
    const level = this.micLevel()
    return Math.min(1, Math.max(0, level - this.floor) * 8)
  }

  private tick(level: number) {
    if (this.muted || this.closed) return
    const now = performance.now()
    if (this.listening) {
      if (level > Math.max(0.02, this.floor * 2.5)) {
        if (!this.heard) { this.heard = true; this.speechInProgress = true; this.events.onSpeech?.() }
        if (this.capture) this.capture.speechMs += 50
        this.quietSince = now
      }
      if (this.heard && now - this.quietSince > PAUSE_MS) this.finishListening()
    } else if (this.speaking && now - this.speakStart > BARGE_AFTER) {
      if (level > Math.max(0.05, this.floor * 4)) {
        // Record from the first loud tick so the barge-in phrase keeps its start.
        this.startRmsCapture(false)
        this.speechInProgress = true
        if (this.capture) this.capture.speechMs += 50
        this.quietSince = now
        if (++this.loud >= BARGE_TICKS) { this.loud = 0; this.events.onBargeIn() }
      } else {
        this.loud = 0
        this.stopRmsCapture(true)
        this.speechInProgress = false
      }
    } else if (!this.speaking) {
      this.stopRmsCapture(true)
      this.speechInProgress = false
      this.followFloor(level)
    }
  }

  private followFloor(level: number) {
    if (level < this.floor * 1.5) this.floor = Math.max(0.004, this.floor * 0.98 + level * 0.02)
  }

  /** Start one phrase. Returns false when the mic is off or not open. */
  listen() {
    if (this.muted || !this.opened || this.closed || this.listening) return false
    this.captureId++
    this.listening = true
    this.heard = this.speechInProgress && !this.dropSegment
    this.finishing = false
    if (this.fallback) this.startRmsCapture(true)
    // A barge-in arms an existing Silero segment, rather than resetting it.
    if (this.heard) this.events.onSpeech?.()
    return true
  }

  private startRmsCapture(accepted: boolean) {
    if (this.capture) { this.capture.accepted ||= accepted; return }
    if (!this.stream) return
    const recorder = new MediaRecorder(this.stream)
    const capture: RmsCapture = { recorder, accepted, discard: false, speechMs: 0 }
    const chunks: Blob[] = []
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data) }
    recorder.onstop = () => {
      if (capture.discard || !capture.accepted || this.muted || this.closed) return
      if (capture.speechMs >= MIN_SPEECH_MS && chunks.length) this.events.onPhrase(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }))
    }
    this.capture = capture
    recorder.start()
  }

  private stopRmsCapture(discard: boolean) {
    const capture = this.capture
    this.capture = null
    if (!capture) return
    capture.discard = discard
    if (capture.recorder.state === 'recording') capture.recorder.stop()
  }

  /** Stop now and send what was heard. */
  finishListening() {
    if (!this.listening || !this.heard || this.finishing) return
    if (this.fallback) {
      const valid = (this.capture?.speechMs ?? 0) >= MIN_SPEECH_MS
      this.heard = this.speechInProgress = false
      if (valid) this.listening = false
      this.stopRmsCapture(!valid)
      if (!valid) this.startRmsCapture(true)
      return
    }
    this.finishing = true
    const id = this.captureId
    this.updateVad(async vad => {
      if (id !== this.captureId || !this.listening || !this.heard) return
      await vad.pause() // submitUserSpeechOnPause emits the captured segment.
      this.finishing = false
      if (!this.muted && !this.closed) await vad.start()
    })
  }

  /** Stop and drop what was heard. */
  cancelListening() {
    this.captureId++
    this.dropSegment = true
    this.listening = false
    this.heard = false
    this.finishing = false
    this.stopRmsCapture(true)
    if (this.fallback) this.speechInProgress = false
  }

  /** Read the parts in order. Resolves true at the end, false when stopped. */
  async speak(parts: string[]) {
    this.stopSpeaking()
    const id = ++this.speakId
    this.speaking = true
    this.speakStart = performance.now()
    void this.context.resume()
    let next = this.clip(parts[0])
    for (let at = 0; at < parts.length; at++) {
      const buffer = await next
      if (id !== this.speakId) return false
      if (at + 1 < parts.length) next = this.clip(parts[at + 1])
      const done = buffer ? await this.play(buffer, id) : await this.browserVoice(parts[at], id)
      if (!done) return false
    }
    if (id === this.speakId) this.speaking = false
    return true
  }

  stopSpeaking() {
    this.speakId++
    this.speaking = false
    this.loud = 0
    try { this.source?.stop() } catch { /* already ended */ }
    this.source = null
    if (canSpeak) speechSynthesis.cancel()
  }

  /** A short tone: 'listen' rises, 'sent' falls. */
  cue(kind: 'listen' | 'sent') {
    const now = this.context.currentTime
    const tone = this.context.createOscillator()
    const gain = this.context.createGain()
    tone.type = 'sine'
    tone.frequency.setValueAtTime(kind === 'listen' ? 520 : 780, now)
    tone.frequency.exponentialRampToValueAtTime(kind === 'listen' ? 780 : 460, now + 0.14)
    gain.gain.setValueAtTime(0.0001, now)
    gain.gain.exponentialRampToValueAtTime(0.08, now + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.18)
    tone.connect(gain).connect(this.context.destination)
    tone.start(now)
    tone.stop(now + 0.2)
  }

  private async clip(text: string): Promise<AudioBuffer | null> {
    if (this.ttsDown || !text) return null
    try {
      const response = await fetch('/mobile/api/tts', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' }, body: JSON.stringify({ text: text.slice(0, 600) }), signal: AbortSignal.timeout(20000) })
      if (!response.ok || !response.headers.get('content-type')?.includes('audio')) {
        if (response.status === 503 || response.status === 404) this.ttsDown = true
        return null
      }
      return await this.context.decodeAudioData(await response.arrayBuffer())
    } catch { return null }
  }

  private play(buffer: AudioBuffer, id: number) {
    return new Promise<boolean>(resolve => {
      const source = this.context.createBufferSource()
      source.buffer = buffer
      source.connect(this.out)
      source.onended = () => resolve(id === this.speakId)
      this.source = source
      source.start()
    })
  }

  private browserVoice(text: string, id: number) {
    return new Promise<boolean>(resolve => {
      if (!canSpeak) { resolve(id === this.speakId); return }
      const utterance = new SpeechSynthesisUtterance(text)
      utterance.voice = speechSynthesis.getVoices().find(voice => voice.lang === 'en-US') ?? null
      utterance.onend = () => resolve(id === this.speakId)
      utterance.onerror = () => resolve(false)
      speechSynthesis.speak(utterance)
    })
  }
}
