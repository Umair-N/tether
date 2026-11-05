import type { PipecatClient } from '@pipecat-ai/client-js'

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
  private closed = false
  private micLevel = 0
  private botLevel = 0
  private bot = ''
  private events: AgentEvents

  constructor(events: AgentEvents) {
    this.events = events
    this.audio.autoplay = true
  }

  get speaking() { return this.turn === 'speaking' }
  get muted() { return this.isMuted }
  set muted(value: boolean) {
    this.isMuted = value
    this.client?.enableMic(!value)
  }

  /** 0..1 for the orb: the voice while it speaks, else the mic. */
  level() {
    if (this.isMuted && this.turn !== 'speaking') return 0
    return Math.min(1, (this.turn === 'speaking' ? this.botLevel : this.micLevel) * 3)
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
        onBotStartedSpeaking: () => { this.bot = ''; this.setTurn('speaking') },
        onBotStoppedSpeaking: () => this.setTurn('listening'),
        onUserTranscript: data => this.events.onUserText(data.text, data.final),
        onBotOutput: data => { this.bot = `${this.bot} ${data.text}`.trim(); this.events.onBotText(this.bot) },
        onServerMessage: (message: { t?: string; d?: unknown }) => {
          if (message?.t === 'metrics') this.events.onMetrics?.(message.d as AgentMetrics)
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
    await client.initDevices()
    await client.connect()
  }

  close() {
    if (this.closed) return
    this.closed = true
    this.audio.srcObject = null
    void this.client?.disconnect().catch(() => {})
    this.client = null
  }

  send(t: 'state' | 'event' | 'say' | 'note', d: unknown) {
    try { this.client?.sendClientMessage(t, d) } catch { /* not connected yet */ }
  }
  state(chats: AgentChats) { this.send('state', chats) }
  event(event: AgentEvent) { this.send('event', event) }
  say(text: string) { this.send('say', { text }) }
  note(text: string) { this.send('note', { text }) }

  private setTurn(turn: AgentTurn) {
    if (this.turn === turn) return
    this.turn = turn
    this.events.onTurn(turn)
  }
}
