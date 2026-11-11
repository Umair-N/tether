// The composer's + sheet opens in the browser's top layer (popover="manual"), so no chat overlay
// (pins, task chips, agent bubbles) can draw over it, whatever stacking context the composer is in.
// On a wide screen it sits above the + button; on a phone the CSS turns it into a bottom sheet.
type Layer = { showPopover?: () => void; style: { setProperty: (name: string, value: string) => void } }
type Anchor = { getBoundingClientRect: () => { left: number; top: number } }

export function placeAttachLayer(layer: Layer, anchor: Anchor, viewportHeight: number) {
  layer.showPopover?.()
  const at = anchor.getBoundingClientRect()
  layer.style.setProperty('--attach-left', `${Math.max(8, Math.round(at.left))}px`)
  layer.style.setProperty('--attach-bottom', `${Math.max(8, Math.round(viewportHeight - at.top + 8))}px`)
}
