export function joinRows(rows: { text: string; wrapped: boolean }[], cols: number): string {
  let text = ''
  for (let i = 0; i < rows.length; i++) {
    // tmux redraws with cursor moves, so full-width rows can continue without isWrapped.
    if (i > 0 && !rows[i].wrapped && rows[i - 1].text.length < cols) text += '\n'
    text += rows[i].text
  }
  return text.replace(/\n+$/, '')
}
