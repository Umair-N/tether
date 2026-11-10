import { useSyncExternalStore } from 'react'
import { request } from './api'

/** What the UI calls the machine until the server says its name, and if the lookup fails. */
export const GENERIC_HOST = 'the server'

// GET /mobile/api/host → {"name": "<short host name>", "office": bool}. Loaded once per page; every component shares the result.
let name: string | null = null
let office = false
let started = false
const listeners = new Set<() => void>()

function load() {
  if (started) return
  started = true
  void request<{ name?: string; office?: boolean }>('host').then(
    data => {
      if (typeof data.name === 'string' && data.name.trim()) name = data.name.trim()
      office = data.office === true
      listeners.forEach(notify => notify())
    },
    () => { started = false },
  )
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  load()
  return () => { listeners.delete(listener) }
}

/** The server's short host name, or "the server" while it loads or when it cannot be read. */
export function useHost() {
  return useSyncExternalStore(subscribe, () => name ?? GENERIC_HOST, () => GENERIC_HOST)
}

/** True when the server has the Office editor add-on (ONLYOFFICE) turned on. */
export function useOfficeEditing() {
  return useSyncExternalStore(subscribe, () => office, () => false)
}

/** Capitalise the first letter, for a host at the start of a sentence. */
export const sentence = (text: string) => text.charAt(0).toUpperCase() + text.slice(1)
