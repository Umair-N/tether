import { useEffect, useState } from 'react'
import { Copy, Laptop } from 'lucide-react'
import { errorText } from './api'
import type { Api, BrowserExtensionDevice, BrowserExtensionDevices, BrowserExtensionPair } from './api'

function ago(seconds: number, now: number) {
  const minutes = Math.round((now / 1000 - seconds) / 60)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`
}

const chromeVersion = (agent: string) => /Chrome\/(\d+)/.exec(agent)?.[1] ?? ''

/** Mac Chrome with no open tab: pair the Tether extension (server/mac_browser.py) and manage paired Macs.
 * Tabs appear here only after Mak taps Allow on an agent's request. */
export function MacPairing({ api, connected }: { api: Api; connected: boolean }) {
  const [devices, setDevices] = useState<BrowserExtensionDevice[] | null>(null)
  const [code, setCode] = useState<BrowserExtensionPair | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState('')

  useEffect(() => {
    let stop = false
    const load = async () => {
      try { const rows = await api<BrowserExtensionDevice[]>('browser/extension/devices'); if (!stop) setDevices(rows) } catch (failure) { if (!stop) setError(errorText(failure)) }
    }
    void load()
    const timer = window.setInterval(() => { setNow(Date.now()); void load() }, 5000)
    return () => { stop = true; clearInterval(timer) }
  }, [api])

  const left = code ? Math.max(0, Math.ceil((code.expires * 1000 - now) / 60000)) : 0
  const live = code && code.expires * 1000 > now ? code : null
  const origin = window.location.origin

  async function pair() {
    setBusy(true); setError('')
    try { setCode(await api<BrowserExtensionPair>('browser/extension/pair', {})); setNow(Date.now()) } catch (failure) { setError(errorText(failure)) } finally { setBusy(false) }
  }
  async function revoke(id: string) {
    setError('')
    try { setDevices((await api<BrowserExtensionDevices>('browser/extension/revoke', { id })).devices) } catch (failure) { setError(errorText(failure)) }
  }
  async function copy(text: string) {
    try { await navigator.clipboard.writeText(text); setCopied(text); window.setTimeout(() => setCopied(''), 1500) } catch { /* no clipboard access: the text stays selectable */ }
  }

  return <div className="mac-pairing">
    <Laptop size={22} aria-hidden="true" className="mac-pairing-icon" />
    <h3>{connected ? 'Your Mac is connected' : 'Connect Chrome on your Mac'}</h3>
    <p className="mac-pairing-lead">{connected
      ? 'No Mac tab is open. When an agent asks for your browser, tap Allow. Its tab shows here.'
      : 'Agents can then use a tab in your own Chrome, after you tap Allow.'}</p>

    {!connected && <ol className="mac-pairing-steps">
      <li>On the Mac, open <code>chrome://extensions</code>, turn on Developer mode, and load the Tether extension folder.</li>
      <li>Open the extension’s options. Enter this address and a pairing code.</li>
      <li>Click Allow when Chrome asks for access.</li>
    </ol>}

    <div className="mac-pairing-fields">
      <div className="mac-pairing-field"><span>Address</span><code>{origin}</code>
        <button type="button" className="browser-icon" aria-label="Copy the address" onClick={() => void copy(origin)}><Copy size={15} /></button></div>
      {live ? <div className="mac-pairing-field"><span>Code</span><code className="mac-pairing-code">{live.code}</code>
        <button type="button" className="browser-icon" aria-label="Copy the code" onClick={() => void copy(live.code)}><Copy size={15} /></button></div> : null}
    </div>
    <p className="mac-pairing-meta" role="status">{copied ? 'Copied' : live ? `The code works once. It expires in ${left} min.` : ''}</p>
    <button type="button" className={connected ? 'secondary' : 'primary'} disabled={busy} onClick={() => void pair()}>{live ? 'New code' : connected ? 'Pair another Mac' : 'Get a pairing code'}</button>

    {devices && devices.length > 0 && <ul className="mac-pairing-devices" aria-label="Paired Macs">
      {devices.map(device => <li key={device.id}>
        <span className={device.connected ? 'mac-dot on' : 'mac-dot'} aria-hidden="true" />
        <span className="mac-pairing-name"><strong>{device.name}</strong><small>{device.connected ? 'Connected' : `Last seen ${ago(device.lastSeen, now)}`}{chromeVersion(device.chrome) ? ` · Chrome ${chromeVersion(device.chrome)}` : ''}</small></span>
        <button type="button" className="browser-text-button" onClick={() => void revoke(device.id)}>Remove</button>
      </li>)}
    </ul>}
    {error && <p className="error" role="alert">{error}</p>}
  </div>
}
