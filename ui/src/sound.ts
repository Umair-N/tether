// The short rising two-note chime that voice input apps play when the mic is ready.
// Browsers start audio only from a tap, so call prepareChime() at the start of the tap handler, before any await.
let context: AudioContext | null = null

export function prepareChime() {
  try {
    context ??= new AudioContext()
    if (context.state === 'suspended') void context.resume()
  } catch { context = null }
}

// Plays the chime and resolves when it ends. Play it before the mic opens: on Android, an open mic switches
// the phone to call audio, which can silence or reroute sounds played after it.
export async function playChime(): Promise<void> {
  const audio = context
  if (!audio) return
  // Without a recent tap, resume() can wait forever. Do not hold up the mic for the chime.
  if (audio.state !== 'running') await Promise.race([audio.resume().catch(() => undefined), new Promise(done => setTimeout(done, 400))])
  if (audio.state !== 'running') return
  const start = audio.currentTime + 0.02
  const notes: [number, number][] = [[660, 0], [990, 0.1]]
  for (const [frequency, offset] of notes) {
    const tone = audio.createOscillator(), level = audio.createGain()
    tone.type = 'sine'
    tone.frequency.value = frequency
    level.gain.setValueAtTime(0, start + offset)
    level.gain.linearRampToValueAtTime(0.22, start + offset + 0.015)
    level.gain.exponentialRampToValueAtTime(0.001, start + offset + 0.12)
    tone.connect(level).connect(audio.destination)
    tone.start(start + offset)
    tone.stop(start + offset + 0.13)
  }
  await new Promise(resolve => window.setTimeout(resolve, 260))
}
