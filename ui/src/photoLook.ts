import type { BackgroundItem } from './backgrounds'
import { DEFAULT_LOOK, PHOTO_URL, type Look } from './layouts'
import type { Prefs } from './prefs'

type Update = (change: (current: Prefs) => Partial<Prefs>) => void

/** The photo's mean colour (weighted to its saturated parts) and whether it is light, from a 32×18 copy. */
async function photoPalette(url: string): Promise<{ tint: string; light: boolean }> {
  const image = new Image()
  image.src = url
  await image.decode()
  const canvas = document.createElement('canvas')
  canvas.width = 32; canvas.height = 18
  const context = canvas.getContext('2d', { willReadFrequently: true })
  if (!context) throw new Error('No canvas')
  context.drawImage(image, 0, 0, 32, 18)
  const data = context.getImageData(0, 0, 32, 18).data
  let r = 0, g = 0, b = 0, weight = 0, light = 0
  for (let at = 0; at < data.length; at += 4) {
    const red = data[at], green = data[at + 1], blue = data[at + 2]
    const w = 0.08 + (Math.max(red, green, blue) - Math.min(red, green, blue)) / 255
    r += red * w; g += green * w; b += blue * w; weight += w
    light += (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
  }
  const hex = (value: number) => Math.round(value / weight).toString(16).padStart(2, '0')
  return { tint: `#${hex(r)}${hex(g)}${hex(b)}`, light: light / (data.length / 4) > 0.55 }
}
const darkPage = () => getComputedStyle(document.documentElement).colorScheme.includes('dark') || matchMedia('(prefers-color-scheme: dark)').matches && document.documentElement.dataset.theme !== 'light'

/** A photo brings a matching tint and a fuller panel fill when its brightness differs from the page. */
export async function applyPhoto(item: BackgroundItem, update: Update): Promise<void> {
  if (!PHOTO_URL.test(item.url)) return
  const set = (patch: Partial<Look>) => update(state => ({ look: { ...state.look ?? DEFAULT_LOOK, ...patch } }))
  set({ backdrop: 'photo', photo: { id: item.id, url: item.url } })
  try {
    const { tint, light } = await photoPalette(item.url)
    set({ tint, fill: light === darkPage() ? 0.8 : 0.6 })
  } catch { /* The photo still shows; the tint stays as it was. */ }
}
