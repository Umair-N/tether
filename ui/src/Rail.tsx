import { AudioLines, Folder, MessageCircle, SquareTerminal } from 'lucide-react'
import { ASSISTANT } from './Live'
import { keyLabel } from './keys'

export type Screen = 'chats' | 'files' | 'terminals'
const SCREENS: { id: Screen; label: string; key: string; icon: typeof Folder }[] = [
  { id: 'chats', label: 'Chats', key: '⌘1', icon: MessageCircle },
  { id: 'files', label: 'Files', key: '⌘2', icon: Folder },
  { id: 'terminals', label: 'Terminals', key: '⌘3', icon: SquareTerminal },
]

/** The desktop left rail: Chats, Files and Terminals. The phone does not show it. */
export function Rail({ screen, onScreen, onLive }: { screen: Screen; onScreen: (screen: Screen) => void; onLive?: () => void }) {
  return <nav className="app-rail" aria-label="Tether">
    <span className="app-rail-mark" aria-hidden="true"><SquareTerminal size={16} /></span>
    {SCREENS.map(({ id, label, key, icon: Icon }) => <button key={id} type="button" className="app-rail-button" aria-label={label} title={`${label}  ${keyLabel(key)}`}
      aria-current={screen === id ? 'page' : undefined} onClick={() => onScreen(id)}><Icon size={20} aria-hidden="true" /></button>)}
    {onLive && <button type="button" className="app-rail-button app-rail-live" aria-label={`${ASSISTANT} Live mode`} title={`${ASSISTANT} Live mode: talk hands-free`} onClick={onLive}><AudioLines size={20} aria-hidden="true" /></button>}
  </nav>
}
