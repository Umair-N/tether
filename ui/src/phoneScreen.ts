import type { Screen } from './Rail'

// The phone's screen switch (tab bar). Chats and Terminals are the open chat's two views.

export const toggleButton = () => document.querySelector<HTMLButtonElement>('.app-shell .chat-panel .terminal-toggle')

/** Show a screen. Chats and Terminals switch the open chat's terminal view to match. */
export function choosePhoneScreen(next: Screen, setScreen: (screen: Screen) => void) {
  setScreen(next)
  if (next === 'home' || next === 'files' || next === 'share') return
  setTimeout(() => {
    const toggle = toggleButton()
    if (toggle && (toggle.getAttribute('aria-pressed') === 'true') !== (next === 'terminals')) toggle.click()
  }, 0)
}
