import type { Pane } from './api'

// A Codex pane started with `codex -p foundry` shows "Foundry", so Azure use is easy to see. The plan shows nothing.
export const profileName = (profile: string) => profile === 'foundry' ? 'Foundry' : profile
export const profileSuffix = (pane: Pane) => pane.agent === 'codex' && pane.codex_profile ? ` · ${profileName(pane.codex_profile)}` : ''
