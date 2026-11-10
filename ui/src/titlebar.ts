import { useEffect } from 'react'

// Installed desktop app with the title bar hidden: the window controls (close, minimise, the app menu) float over
// the top strip of the page. Which panel sits under them depends on the layout, and layouts can be made by the user
// or an agent. So Tether does not guess from class names: it measures each panel of the app shell against the
// controls and gives it how far the controls reach into it. TitleBar.css pads the panel headers by that much.

export type Box = { left: number; right: number; top: number; bottom: number }
export type Area = { x: number; width: number; height: number }  // navigator.windowControlsOverlay.getTitlebarAreaRect()
export type Insets = { left: number; right: number; height: number }

// How far the controls reach into a panel: from its left edge (left controls, macOS), from its right edge (right
// controls, Windows and Linux, and the app menu) and down from its top. Zero when the panel is below the strip.
export function controlInsets(box: Box, area: Area, viewport: number): Insets {
  const none = { left: 0, right: 0, height: 0 }
  if (area.height <= 0 || box.bottom <= box.top || box.top >= area.height) return none
  const leftEnd = area.x, rightStart = area.width > 0 ? area.x + area.width : viewport
  const left = box.left < leftEnd ? Math.min(leftEnd, box.right) - box.left : 0
  const right = box.right > rightStart ? box.right - Math.max(rightStart, box.left) : 0
  if (!left && !right) return none
  return { left: Math.round(left), right: Math.round(right), height: Math.round(area.height - box.top) }
}

type Overlay = EventTarget & { visible: boolean; getTitlebarAreaRect: () => DOMRect }

export function useWindowControls(shown: boolean) {
  useEffect(() => {
    const overlay = (navigator as Navigator & { windowControlsOverlay?: Overlay }).windowControlsOverlay
    const root = document.querySelector('.app-shell')
    if (!overlay || !shown || !root) return
    let frame = 0
    const panels = () => Array.from(document.querySelectorAll<HTMLElement>('.app-shell > *'))
    const measure = () => {
      frame = 0
      const visible = overlay.visible
      const area = overlay.getTitlebarAreaRect()
      for (const panel of panels()) {
        const inset = visible ? controlInsets(panel.getBoundingClientRect(), { x: area.x, width: area.width, height: area.height }, window.innerWidth) : null
        if (!inset || (!inset.left && !inset.right)) {
          if (panel.hasAttribute('data-wco')) { panel.removeAttribute('data-wco'); for (const name of ['--wco-l', '--wco-r', '--wco-h']) panel.style.removeProperty(name) }
          continue
        }
        panel.setAttribute('data-wco', '')
        panel.style.setProperty('--wco-l', `${inset.left}px`)
        panel.style.setProperty('--wco-r', `${inset.right}px`)
        panel.style.setProperty('--wco-h', `${inset.height}px`)
      }
    }
    const later = () => { if (!frame) frame = requestAnimationFrame(measure) }
    const sizes = new ResizeObserver(later)
    const watch = () => { sizes.disconnect(); for (const panel of panels()) sizes.observe(panel); later() }
    // Panels come and go (inbox, layout panels), and a layout change moves them without a resize of the window.
    const changes = new MutationObserver(watch)
    changes.observe(root, { childList: true, attributes: true, attributeFilter: ['style', 'class', 'data-grid', 'data-layout', 'data-gap'] })
    overlay.addEventListener('geometrychange', later)
    window.addEventListener('resize', later)
    watch()
    return () => { cancelAnimationFrame(frame); sizes.disconnect(); changes.disconnect(); overlay.removeEventListener('geometrychange', later); window.removeEventListener('resize', later) }
  }, [shown])
}
