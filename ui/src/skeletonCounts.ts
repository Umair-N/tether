// Placeholder counts kept between visits, so a skeleton shows as many rows as the real list had last time.

/** How many placeholders to show: the last real count, kept between visits, within 0..max. */
export function rememberedCount(key: string, fallback: number, max = 4): number {
  try {
    const value = Number(localStorage.getItem(`tether.skeleton.${key}`))
    if (Number.isFinite(value) && localStorage.getItem(`tether.skeleton.${key}`) !== null) return Math.min(max, Math.max(0, Math.round(value)))
  } catch { /* Storage can be blocked. */ }
  return fallback
}

export function rememberCount(key: string, count: number) {
  try { localStorage.setItem(`tether.skeleton.${key}`, String(count)) } catch { /* Storage can be blocked. */ }
}
