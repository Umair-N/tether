import { useEffect } from 'react'
import { Folder, MessageCircle, SquareTerminal } from 'lucide-react'
import type { Screen } from './Rail'
import { toggleButton } from './phoneScreen'
import './ExplorerPhone.css'

// The phone's bottom tab bar: Chats | Files | Terminals (ExplorerPhone.dc.html). The desktop shows the left rail instead.
// Chats and Terminals are the chat panel and its terminal view; Files is the phone explorer.

const TABS: { id: Screen; label: string; icon: typeof Folder }[] = [
  { id: 'chats', label: 'Chats', icon: MessageCircle },
  { id: 'files', label: 'Files', icon: Folder },
  { id: 'terminals', label: 'Terminals', icon: SquareTerminal },
]

export function PhoneTabs({ screen, onScreen, setScreen, terminals }: { screen: Screen; onScreen: (screen: Screen) => void; setScreen: (screen: Screen) => void; terminals: boolean }) {
  // The chat header has its own terminal button, and each pane keeps its own view. Keep the tab in step with what shows.
  useEffect(() => {
    if (screen === 'files') return
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
  </nav>
}
