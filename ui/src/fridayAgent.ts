import type { PipecatClient } from '@pipecat-ai/client-js'
import { DeviceVoice, type VoiceKind } from './deviceVoice'

// Friday agent: one WebRTC call to the local voice worker (server/friday_agent.py, through /mobile/api/live/offer).
// The worker listens, thinks and speaks; this side plays its voice, runs the tools it asks for, and sends it chat events.
// Client messages are {t, d}: state (chats), event (chat news to speak), say (exact words), note (silent context).

export type AgentTurn = 'listening' | 'thinking' | 'speaking'
export type ToolResult = Record<string, unknown>
export type ToolHandler = (args: Record<string, unknown>) => Promise<ToolResult>
export interface AgentMetrics { turn: number; stop_to_first_audio: number; stt: number; llm_ttft: number; tts_ttfb: number }
export interface AgentEvent { id: string; chat: string; kind: 'approval' | 'question' | 'plan' | 'done' | 'error'; text: string; priority: 'now' | 'idle' }
// Kept small: the state goes into the LLM prompt on most turns (about 7 ms per token on this CPU).
export interface AgentChats { target: string; busy: string[]; needs: { chat: string; needs: string }[]; idle: number }
export interface AgentChatList { target: string; chats: string[] }

export interface AgentEvents {
  onTurn: (turn: AgentTurn) => void
  onUserText: (text: string, final: boolean) => void
  onBotText: (text: string) => void
  onMetrics?: (metrics: AgentMetrics) => void
  onVoice?: (kind: VoiceKind | null) => void
  onClosed: (reason: string) => void
  tools: Record<string, ToolHandler>
}

/** True when the worker answers its health check. */
export async function agentReady() {
  try {
    const response = await fetch('/mobile/api/live/agent', { credentials: 'same-origin', signal: AbortSignal.timeout(2500) })
    const data = await response.json() as { ok?: boolean; llm?: boolean }
    return response.ok && data.ok === true
  } catch { return false }
}

export class FridayAgent {
  turn: AgentTurn = 'listening'
  private client: PipecatClient | null = null
  private audio = new Audio()
  private isMuted = false
  private gated = false
  private closed = false
  private micLevel = 0
  private botLevel = 0
  private bot = ''
  private events: AgentEvents
  private voice: DeviceVoice

  constructor(events: AgentEvents) {
    this.events = events
    this.audio.autoplay = true
    // The device speaks when it can; the server voice (Pocket) is the fallback.
    this.voice = new DeviceVoice({
      onStart: turn => { this.send('spoken', { turn, state: 'start' }); this.setTurn('speaking'); this.gate(this.voice.kind === 'system') },
      onEnd: turn => { this.gate(false); this.send('spoken', { turn, state: 'end' }); this.setTurn('listening') },
      onFail: reason => { console.warn('Friday device voice failed', reason); this.gate(false); this.voice.close(); this.mode(null) },
      onKind: kind => this.mode(kind),
    })
  }

  /** Tell the server who speaks: this device (client) or Pocket (server). */
  private mode(kind: VoiceKind | null) {
    this.send('voice', { mode: kind ? 'client' : 'server' })
    this.events.onVoice?.(kind)
  }

  /** The built-in voice gives the echo canceller no reference, so the mic is off while it speaks (no barge-in). */
  private gate(on: boolean) {
    if (this.gated === on) return
    this.gated = on
    this.client?.enableMic(!this.isMuted && !on)
  }

  get speaking() { return this.turn === 'speaking' }
  get muted() { return this.isMuted }
  set muted(value: boolean) {
    this.isMuted = value
    this.client?.enableMic(!value && !this.gated)
  }

  /** 0..1 for the orb: the voice while it speaks, else the mic. */
  level() {
    if (this.isMuted && this.turn !== 'speaking') return 0
    const bot = this.voice.kind ? this.voice.level() : this.botLevel
    return Math.min(1, (this.turn === 'speaking' ? bot : this.micLevel) * 3)
  }

  /** Connect the call. Mic denial stays a DOMException. */
  async open() {
    const [{ PipecatClient }, { SmallWebRTCTransport }] = await Promise.all([import('@pipecat-ai/client-js'), import('@pipecat-ai/small-webrtc-transport')])
    if (this.closed) return
    const headers = new Headers({ 'X-Codex-Mobile': '1' })
    const client = new PipecatClient({
      transport: new SmallWebRTCTransport({ webrtcRequestParams: { endpoint: '/mobile/api/live/offer', headers, timeout: 15000 } }),
      enableMic: !this.isMuted,
      enableCam: false,
      callbacks: {
        onTrackStarted: (track, participant) => {
          if (track.kind !== 'audio' || participant?.local) return
          this.audio.srcObject = new MediaStream([track])
          void this.audio.play().catch(() => {})
        },
        onLocalAudioLevel: level => { this.micLevel = level },
        onRemoteAudioLevel: level => { this.botLevel = level },
        onUserStartedSpeaking: () => this.setTurn('listening'),
        onUserStoppedSpeaking: () => this.setTurn('thinking'),
        // Messages sent before the data channel opens are dropped, so tell the server the voice mode again here.
        onBotReady: () => this.mode(this.voice.kind),
        onBotStartedSpeaking: () => { if (!this.voice.kind) this.bot = ''; this.setTurn('speaking') },
        onBotStoppedSpeaking: () => this.setTurn('listening'),
        onUserTranscript: data => this.events.onUserText(data.text, data.final),
        onBotOutput: data => {
          if (this.voice.kind) return  // The device shows each sentence as it gets it (say).
          this.bot = `${this.bot} ${data.text}`.trim(); this.events.onBotText(this.bot)
        },
        onServerMessage: (message: { t?: string; d?: unknown }) => {
          if (message?.t === 'metrics') this.events.onMetrics?.(message.d as AgentMetrics)
          else if (message?.t === 'ready') { if (this.voice.kind) this.mode(this.voice.kind) }
          else if (message?.t === 'say') { const d = message.d as { turn: number; seq: number; text: string }; this.bot = d.seq ? `${this.bot} ${d.text}` : d.text; this.events.onBotText(this.bot); this.voice.say(d.turn, d.seq, d.text) }
          else if (message?.t === 'say_end') { const d = message.d as { turn: number; count: number }; this.voice.end(d.turn, d.count) }
          else if (message?.t === 'stop') this.voice.stop()
          // The server heard no words (noise, echo, or a short overlap): no reply comes, so stop showing thinking.
          else if (message?.t === 'idle') { if (this.turn === 'thinking') this.setTurn('listening') }
        },
        onDisconnected: () => { if (!this.closed) this.events.onClosed('Friday hung up.') },
        onError: message => { if (!this.closed) this.events.onClosed(String((message.data as { message?: string })?.message ?? 'Friday stopped.')) },
      },
    })
    this.client = client
    for (const [name, handler] of Object.entries(this.events.tools)) {
      client.registerFunctionCallHandler(name, async call => {
        try { return await handler(call.arguments ?? {}) } catch (failure) { return { error: failure instanceof Error ? failure.message : String(failure) } }
      })
    }
    void this.voice.open().catch(failure => console.warn('Friday device voice unavailable', failure))
    await client.initDevices()
    await client.connect()
  }

  close() {
    if (this.closed) return
    this.closed = true
    this.audio.srcObject = null
    this.voice.close()
    void this.client?.disconnect().catch(() => {})
    this.client = null
  }

  send(t: 'state' | 'event' | 'say' | 'note' | 'voice' | 'spoken' | 'interrupt', d: unknown) {
    try { this.client?.sendClientMessage(t, d) } catch { /* not connected yet */ }
  }
  state(chats: AgentChats) { this.send('state', chats) }
  event(event: AgentEvent) { this.send('event', event) }
  say(text: string) { this.send('say', { text }) }
  note(text: string) { this.send('note', { text }) }
  /** Stop Friday now. Phones on speaker often hide the user's voice from the server, so a tap must work too. */
  interrupt() { this.send('interrupt', {}); this.voice.stop(); this.setTurn('listening') }

  private setTurn(turn: AgentTurn) {
    if (this.turn === turn) return
    this.turn = turn
    this.events.onTurn(turn)
  }
}
