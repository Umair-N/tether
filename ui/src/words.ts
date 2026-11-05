import { createContext, useContext } from 'react'

/** The words the UI uses for a tmux pane. Tether calls it a tab; the "tmux words" setting brings back pane, split and Ctrl-b keys. */
export interface Words {
  tmux: boolean
  /** "tab" or "pane", lower case, for the middle of a sentence. */
  pane: string
  panes: string
  /** "Tab" or "Pane", for the start of a label. */
  Pane: string
  newRight: string
  newBelow: string
  /** tmux key hints such as "Ctrl-b %". Empty in Tether words. */
  key: (tmuxKey: string) => string
}

export function wordsFor(tmux: boolean): Words {
  return tmux
    ? { tmux, pane: 'pane', panes: 'panes', Pane: 'Pane', newRight: 'New pane right', newBelow: 'New pane below', key: tmuxKey => tmuxKey }
    : { tmux, pane: 'tab', panes: 'tabs', Pane: 'Tab', newRight: 'New tab', newBelow: 'New tab below', key: () => '' }
}

export const WordsContext = createContext<Words>(wordsFor(false))
export const useWords = () => useContext(WordsContext)
/** "1 tab", "3 panes". */
export const count = (words: Words, n: number) => `${n} ${n === 1 ? words.pane : words.panes}`
