import { useEffect, useLayoutEffect, useRef } from 'react'
import type { Screen } from './Rail'

export type Place = { pane?: string; screen: Screen }
type NavState = { tether: 1; layer: number; place?: Place }
type Overlay = { id: number; close: () => void; layer: number; noEntry: boolean }

const overlays: Overlay[] = []
let pendingBacks = 0
const queue: (() => void)[] = []
let placeHandler: ((place: Place) => void) | null = null
let currentPlace: Place | null = null
let nextId = 0
let listening = false

const state = () => history.state as Partial<NavState> | null
const equal = (a: Place | null | undefined, b: Place | null) => a?.pane === b?.pane && a?.screen === b?.screen
const activated = () => navigator.userActivation?.isActive !== false

function run(fn: () => void) {
  if (pendingBacks > 0) queue.push(fn)
  else fn()
}

function back() {
  run(() => { pendingBacks++; history.back() })
}

function listen() {
  if (listening) return
  listening = true
  window.addEventListener('popstate', event => {
    if (pendingBacks > 0) {
      pendingBacks--
      while (pendingBacks === 0 && queue.length) queue.shift()!()
      return
    }
    const next = event.state as Partial<NavState> | null
    const layer = next?.layer ?? 0
    // Remove first, so the close callback cannot go back a second time.
    while (overlays.length > layer) overlays.pop()!.close()
    if (layer > overlays.length) { back(); return }
    if (layer === 0 && next?.place && !equal(next.place, currentPlace)) {
      currentPlace = next.place
      placeHandler?.(next.place)
    }
  })
}

export function openOverlay(close: () => void): number {
  listen()
  const overlay: Overlay = { id: ++nextId, close, layer: overlays.length + 1, noEntry: true }
  const count = overlays.length
  overlays.push(overlay)
  if (activated()) run(() => {
    if (!overlays.includes(overlay)) return
    const next: NavState = { ...state(), tether: 1, layer: overlay.layer, place: currentPlace ?? undefined }
    if ((state()?.layer ?? 0) > count) history.replaceState(next, '')
    else history.pushState(next, '')
    overlay.noEntry = false
  })
  return overlay.id
}

export function closeOverlay(id: number) {
  const at = overlays.findIndex(overlay => overlay.id === id)
  if (at < 0) return
  const [overlay] = overlays.splice(at, 1)
  run(() => {
    if (!overlay.noEntry && state()?.layer === overlay.layer) back()
  })
}

export function setPlace(place: Place, { push }: { push: boolean }) {
  listen()
  if (equal(place, currentPlace)) return
  const first = currentPlace === null
  const canPush = push && overlays.length === 0 && activated()
  currentPlace = place
  run(() => {
    const previous = state()
    const next: NavState = { ...previous, tether: 1, layer: 0, place }
    if (first) history.replaceState(next, '')
    else if (canPush) {
      if ((previous?.layer ?? 0) > overlays.length) history.replaceState(next, '')
      else history.pushState(next, '')
    } else if ((previous?.layer ?? 0) === 0) history.replaceState(next, '')
  })
}

export function setPlaceHandler(fn: ((place: Place) => void) | null) {
  listen()
  placeHandler = fn
}

export function useBackStep(open: boolean, close: () => void) {
  const latest = useRef(close)
  useLayoutEffect(() => { latest.current = close }, [close])
  useEffect(() => {
    if (!open) return
    const id = openOverlay(() => latest.current())
    return () => closeOverlay(id)
  }, [open])
}
