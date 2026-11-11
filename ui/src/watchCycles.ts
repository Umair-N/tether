import type { ChatMessage } from './api'

const isQuiet = (message: ChatMessage) => message.kind === 'notice' && message.quiet === 'watch'
const isRearm = (message: ChatMessage) => message.kind === 'actions'
  && !!message.tools?.length
  && message.tools.every(tool => tool.name === 'Monitor' && tool.status === 'complete' && !tool.is_error)
const isReply = (message: ChatMessage) => message.role === 'assistant'
  && !message.kind
  && message.text.trim().length <= 400
  && !/needs you:\s*yes/i.test(message.text)
  && !message.text.includes('?')

export function foldWatchCycles(messages: ChatMessage[]): ChatMessage[] {
  const firstQuiet = messages.findIndex(isQuiet)
  if (firstQuiet === -1) return messages

  const result = messages.slice(0, firstQuiet)
  let index = firstQuiet
  while (index < messages.length) {
    const first = messages[index]
    if (!isQuiet(first)) {
      result.push(first)
      index++
      continue
    }

    const members: ChatMessage[] = []
    const watches = new Set<string>()
    let cycles = 0
    let rearmed = false
    let afterReply = false
    while (index < messages.length) {
      const message = messages[index]
      if (isQuiet(message)) {
        cycles++
        if (message.watch !== undefined) watches.add(message.watch)
        afterReply = false
      } else if (!afterReply && isRearm(message)) {
        rearmed = true
      } else if (!afterReply && isReply(message)) {
        afterReply = true
      } else {
        break
      }
      members.push(message)
      index++
    }

    result.push({
      id: 'watch:' + first.id,
      role: 'assistant',
      kind: 'watch',
      text: '',
      time: members[members.length - 1].time,
      members,
      watches: [...watches],
      cycles,
      rearmed,
    })
  }
  return result
}
