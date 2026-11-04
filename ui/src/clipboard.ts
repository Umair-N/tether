/** Copy from a user action, including plain HTTP where Clipboard API is absent. */
export async function copyText(text: string): Promise<void> {
  try {
    if (navigator.clipboard) { await navigator.clipboard.writeText(text); return }
  } catch { /* Try the browser's native copy command. */ }
  const previous = document.activeElement
  const field = document.createElement('textarea')
  field.value = text
  field.readOnly = true
  field.style.cssText = 'position:fixed;left:-9999px;top:0'
  document.body.append(field)
  try {
    field.select()
    if (!document.execCommand('copy')) throw new Error('Copy failed')
  } finally {
    field.remove()
    if (previous instanceof HTMLElement) previous.focus({ preventScroll: true })
  }
}
