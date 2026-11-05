import { useEffect } from 'react'
import { AudioLines, Folder, Globe, MessageCircle, Share2, SquareTerminal } from 'lucide-react'
import { ASSISTANT } from './Live'
import type { Screen } from './Rail'
import { toggleButton } from './phoneScreen'
import './ExplorerPhone.css'

// The phone's bottom tab bar: Chats | Files | Terminals (ExplorerPhone.dc.html). The desktop shows the left rail instead.
// Chats and Terminals are the chat panel and its terminal view; Files is the phone explorer.

const TABS: { id: Screen; label: string; icon: typeof Folder }[] = [
  { id: 'chats', label: 'Chats', icon: MessageCircle },
  { id: 'files', label: 'Files', icon: Folder },
  { id: 'terminals', label: 'Terminals', icon: SquareTerminal },
  { id: 'share', label: 'Share', icon: Share2 },
]

/** browser: opens the shared browser full screen. busy = an agent has tabs open, asking = an agent needs the person there. */
export interface PhoneBrowser { onOpen: () => void; busy: boolean; asking: boolean }

export function PhoneTabs({ screen, onScreen, setScreen, terminals, onLive, browser }: { screen: Screen; onScreen: (screen: Screen) => void; setScreen: (screen: Screen) => void; terminals: boolean; onLive?: () => void; browser?: PhoneBrowser }) {
  // The chat header has its own terminal button, and each pane keeps its own view. Keep the tab in step with what shows.
  useEffect(() => {
    if (screen === 'files' || screen === 'share') return
    const shell = document.querySelector('.app-shell')
    if (!shell) return
    const sync = () => {
      const toggle = toggleButton()
      const shown: Screen = toggle?.getAttribute('aria-pressed') === 'true' ? 'terminals' : 'chats'
      if (toggle && shown !== screen) setScreen(shown)
    }
    const observer = new MutationObserver(sync)
    observer.observe(shell, { subtree: true, childList: true, attributes: true, attributeFilter: ['aria-pressed'] })
    return () => observer.disconnect()
  }, [screen, setScreen])
  return <nav className="phone-tabbar" aria-label="Tether">
    {TABS.map(({ id, label, icon: Icon }) => <button key={id} type="button" aria-current={screen === id ? 'page' : undefined}
      disabled={id === 'terminals' && !terminals} onClick={() => onScreen(id)}><Icon size={22} aria-hidden="true" /><span>{label}</span></button>)}
    {browser && <button type="button" className="phone-tab-browser" onClick={browser.onOpen}
      aria-label={browser.asking ? 'Browser, an agent needs you' : browser.busy ? 'Browser, agents are using it' : 'Browser'}>
      <span className="phone-tab-icon"><Globe size={22} aria-hidden="true" />{(browser.busy || browser.asking) && <span className={`phone-tab-dot${browser.asking ? ' asking' : ''}`} aria-hidden="true" />}</span>
      <span>Browser</span>
    </button>}
    {onLive && <button type="button" onClick={onLive}><AudioLines size={22} aria-hidden="true" /><span>{ASSISTANT}</span></button>}
  </nav>
}
