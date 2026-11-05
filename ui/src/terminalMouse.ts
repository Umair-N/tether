type DragTerm = {
  element?: HTMLElement
  cols: number
  options: { macOptionClickForcesSelection?: boolean }
  focus(): void
  hasSelection(): boolean
  getSelection(): string
  getSelectionPosition(): { start: { x: number; y: number }; end: { x: number; y: number } } | undefined
  clearSelection(): void
  buffer: { active: { getLine(y: number): { isWrapped: boolean; translateToString(trim: boolean): string } | undefined } }
}

// A full pane row continues into the next part, even when tmux did not mark it wrapped.
export function joinSelection(parts: string[], prevFull: boolean[]): string {
  return parts.map((part, k) => (k > 0 && !prevFull[k] ? '\n' : '') + part).join('')
}

export function dragSelect(win: Window, term: DragTerm, paneCols: () => number, onCopied: () => void): void {
  const marked = win as Window & { __tetherDragSelect?: boolean }
  if (marked.__tetherDragSelect) return
  marked.__tetherDragSelect = true
  const mac = ['Macintosh', 'MacIntel', 'MacPPC', 'Mac68K'].includes(win.navigator.platform)
  if (mac) term.options.macOptionClickForcesSelection = true
  const forced = mac ? { altKey: true, shiftKey: false } : { shiftKey: true, altKey: false }
  const plain = { altKey: false, shiftKey: false }
  let state: { kind: 'idle' } | { kind: 'pending'; down: MouseEvent } | { kind: 'dragging' } = { kind: 'idle' }
  const clone = (type: string, event: MouseEvent, modifiers: typeof plain, detail = event.detail) => new MouseEvent(type, {
    bubbles: true, cancelable: true, composed: true, view: win, detail,
    screenX: event.screenX, screenY: event.screenY, clientX: event.clientX, clientY: event.clientY,
    button: 0, buttons: event.buttons, ctrlKey: false, metaKey: false, ...modifiers,
  })
  win.addEventListener('mousedown', event => {
    if (event.isTrusted === false || event.button !== 0 || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return
    if (!term.element?.querySelector('.xterm-screen')?.contains(event.target as Node | null)) return
    event.stopImmediatePropagation(); event.preventDefault(); term.focus()
    if (event.detail >= 2) {
      term.clearSelection()
      event.target?.dispatchEvent(clone('mousedown', event, forced))
      state = { kind: 'dragging' }
    } else state = { kind: 'pending', down: event }
  }, { capture: true })
  win.addEventListener('mousemove', event => {
    if (event.isTrusted === false || state.kind !== 'pending') return
    const down = state.down
    if (Math.hypot(event.clientX - down.clientX, event.clientY - down.clientY) < 4) return
    term.clearSelection()
    down.target?.dispatchEvent(clone('mousedown', down, forced, 1))
    state = { kind: 'dragging' }
  }, { capture: true })
  win.addEventListener('mouseup', event => {
    if (event.isTrusted === false) return
    if (event.button === 0) {
      if (state.kind === 'pending') {
        const down = state.down
        down.target?.dispatchEvent(clone('mousedown', down, plain, 1))
      } else if (state.kind === 'dragging') {
        event.stopImmediatePropagation(); event.preventDefault()
        // End xterm's selection on the document, so tmux receives no release report.
        win.document.dispatchEvent(clone('mouseup', event, plain))
        if (term.hasSelection()) {
          let copied = false
          try { copied = win.document.execCommand('copy') } catch { /* clipboard access may be unavailable */ }
          if (copied) onCopied()
        }
      }
    }
    state = { kind: 'idle' }
  }, { capture: true })
  win.addEventListener('blur', () => { state = { kind: 'idle' } })
  win.document.addEventListener('copy', event => {
    if (!term.hasSelection()) return
    let text = term.getSelection()
    const pos = term.getSelectionPosition()
    if (pos) {
      const pane = paneCols(), cols = pane > 0 && pane < term.cols ? pane : term.cols
      const buffer = term.buffer.active, lastRows: number[] = []
      // Each part from xterm covers one group of wrapped buffer rows.
      for (let y = pos.start.y; y <= pos.end.y; y++) {
        if (y === pos.start.y || !buffer.getLine(y)?.isWrapped) lastRows.push(y)
        else lastRows[lastRows.length - 1] = y
      }
      const parts = text.split('\n')
      if (parts.length === lastRows.length) {
        const prevFull = lastRows.map((_, k) => k > 0 && (buffer.getLine(lastRows[k - 1])?.translateToString(true).length ?? 0) >= cols)
        text = joinSelection(parts, prevFull)
      }
    }
    event.clipboardData?.setData('text/plain', text)
    event.preventDefault(); event.stopImmediatePropagation()
  }, { capture: true })
}
