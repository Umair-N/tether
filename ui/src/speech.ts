import { useSyncExternalStore } from 'react'

// Read a reply aloud with the browser's own voice (speechSynthesis). Nothing leaves the device.
export const canSpeak = typeof window !== 'undefined' && 'speechSynthesis' in window

// Markdown to words: code blocks are named, not read; links read their text; marks and table pipes go.
export function speakable(markdown: string) {
  return markdown
    .replace(/```[\w-]*\n[\s\S]*?(```|$)/g, ' (code block) ')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+/gm, '')
    .replace(/^\s*\|?[\s:|-]+\|[\s:|-]*$/gm, '')
    .replace(/\|/g, ', ')
    .replace(/(\*\*|__|\*|_|~~)(\S[^*_~]*?)\1/g, '$2')
    .replace(/https?:\/\/\S+/g, 'link')
    .replace(/[ \t]+/g, ' ')
    .trim()
}

let speaking: string | null = null
const listeners = new Set<() => void>()
const set = (id: string | null) => { speaking = id; listeners.forEach(listener => listener()) }

// One reply at a time. onEnd runs only when the reply was read to the end (not after stop).
export function speak(id: string, markdown: string, onEnd?: () => void) {
  if (!canSpeak) return
  speechSynthesis.cancel()
  const text = speakable(markdown)
  if (!text) return
  // Some engines stop on long text, so read it in sentence groups of up to about 200 characters.
  const parts = text.match(/[^.!?\n]+[.!?]*\s*|\n+/g)?.reduce<string[]>((all, piece) => {
    if (all.length && all[all.length - 1].length + piece.length < 200) all[all.length - 1] += piece
    else all.push(piece)
    return all
  }, []).filter(part => part.trim()) ?? [text]
  set(id)
  parts.forEach((part, at) => {
    const utterance = new SpeechSynthesisUtterance(part)
    if (at === parts.length - 1) utterance.onend = () => { if (speaking === id) { set(null); onEnd?.() } }
    utterance.onerror = () => { if (speaking === id) set(null) }
    speechSynthesis.speak(utterance)
  })
}

export function stopSpeaking() { if (canSpeak && speaking) { set(null); speechSynthesis.cancel() } }

export function useSpeaking() {
  return useSyncExternalStore(listener => { listeners.add(listener); return () => { listeners.delete(listener) } }, () => speaking)
}
