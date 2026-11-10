import patterns from './noticeActions.json'

interface NoticeAction { label: string; command: string; hint: string; confirm?: string }
const actions = patterns.map(({ pattern, flags, agents, ...action }) => ({ match: new RegExp(pattern, flags), agents, action }))

export function noticeAction(note: string, agent: 'claude' | 'codex' | string): NoticeAction | null {
  return actions.find(item => item.agents.includes(agent) && item.match.test(note))?.action ?? null
}
