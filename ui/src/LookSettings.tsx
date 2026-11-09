import { useState } from 'react'
import type { Api } from './api'
import type { Prefs } from './prefs'
import { useModal } from './hooks'
import { BUILT_INS, CUSTOM_PREFIX, DEFAULT_LOOK, lookKey, lookLayout, themeLabel, useCustomLayouts } from './layouts'
import type { Backdrop, Layout, Look, Surface } from './layouts'
import { Choice } from './SettingsControls'

// Settings › Appearance: Layout and Style (layouts.ts). A layout with its own colours replaces the user's colours, so
// Tether first offers to save the current look as "My look", which then comes back in one click.

type Update = (change: (current: Prefs) => Partial<Prefs>) => void
const SWATCHES = ['--bg', '--surface', '--text', '--accent', '--attention']

function SaveLook({ next, onSave, onSkip, onCancel }: { next: Layout; onSave: () => void; onSkip: () => void; onCancel: () => void }) {
  const modal = useModal(true, onCancel)
  const [colours] = useState(() => { const style = getComputedStyle(document.documentElement); return SWATCHES.map(name => style.getPropertyValue(name).trim()) })
  return <dialog {...modal} onCancel={event => { event.stopPropagation(); modal.onCancel(event) }} className="sheet look-save" aria-labelledby="look-save-title">
    <h2 id="look-save-title">Save your current look first?</h2>
    <p>“{next.name}” brings its own colours ({themeLabel(next)}). They replace your current colours. Save your current layout and colours as “My look” to come back to them in one click.</p>
    <div className="look-swatches" aria-hidden="true">{colours.map((colour, at) => <span key={SWATCHES[at]} style={{ background: colour }} />)}</div>
    <div className="sheet-buttons look-save-buttons">
      <button type="button" className="secondary" onClick={onCancel}>Cancel</button>
      <button type="button" className="secondary" onClick={onSkip}>Don’t save</button>
      <button type="button" className="primary" onClick={onSave}>Save as “My look”, then apply</button>
    </div>
  </dialog>
}

export function LayoutPicker({ api, prefs, update }: { api: Api; prefs: Prefs; update: Update }) {
  const { custom, error } = useCustomLayouts(api)
  const [asking, setAsking] = useState<Layout | null>(null)
  const look = prefs.look ?? DEFAULT_LOOK
  const shown = lookLayout(look)
  const current = lookKey(shown)
  const layouts = [...BUILT_INS, ...custom?.layouts ?? []]
  // The saved copy of a custom layout stays in the list while its file is gone or refused.
  if (current.startsWith(CUSTOM_PREFIX) && !layouts.some(layout => lookKey(layout) === current)) layouts.push(shown)

  const lookFor = (layout: Layout): Look => ({ layout: lookKey(layout), surface: layout.style.surface, backdrop: look.backdrop, ...lookKey(layout).startsWith(CUSTOM_PREFIX) ? { custom: layout } : {} })
  const apply = (layout: Layout, save: boolean) => {
    setAsking(null)
    update(state => ({ look: lookFor(layout), ...save ? { myLook: { ...state.look ?? DEFAULT_LOOK, scheme: state.scheme } } : {} }))
  }
  const choose = (layout: Layout) => {
    if (lookKey(layout) === current) return
    // Ask only while the user's own colours show and no look is saved yet.
    if (layout.theme && !shown.theme && !prefs.myLook) setAsking(layout)
    else apply(layout, false)
  }
  const restore = () => {
    const saved = prefs.myLook
    if (!saved) return
    const { scheme, ...rest } = saved
    update(() => ({ look: rest, scheme }))
  }

  return <fieldset className="choice look-layouts">
    <legend>Layout</legend>
    <div className="look-options" role="radiogroup" aria-label="Layout">
      {layouts.map(layout => {
        const key = lookKey(layout)
        return <label key={key} className="look-option">
          <input type="radio" name="look-layout" value={key} checked={key === current} onChange={() => choose(layout)} />
          <span className="look-name">{layout.name}{layout.theme && <span className="look-theme" style={{ background: layout.theme.bg, borderColor: layout.theme.accent }} aria-hidden="true" />}</span>
          {layout.description && <small>{layout.description}</small>}
          {layout.stale && <small className="look-stale">The file is refused, so the last good copy shows: {layout.stale}</small>}
        </label>
      })}
    </div>
    {prefs.myLook && <button type="button" className="secondary look-mine" onClick={restore}>My look</button>}
    <p className="setting-note">The layout shows on wide screens. Phones keep their layout and take the colours and the style.</p>
    {custom && <p className="setting-note">Custom layouts: ask an agent to write a JSON layout file to <code>{custom.folder}</code>, then open Settings again.</p>}
    {error && <p className="setting-note look-error" role="alert">Tether cannot read the custom layouts: {error}</p>}
    {!!custom?.errors.length && <ul className="look-errors" role="alert">{custom.errors.map(item => <li key={item.file + item.reason}><code>{item.file || custom.folder}</code>: {item.reason}</li>)}</ul>}
    {asking && <SaveLook next={asking} onSave={() => apply(asking, true)} onSkip={() => apply(asking, false)} onCancel={() => setAsking(null)} />}
  </fieldset>
}

export function StylePicker({ prefs, update }: { prefs: Prefs; update: Update }) {
  const look = prefs.look ?? DEFAULT_LOOK
  const set = (patch: Partial<Look>) => update(state => ({ look: { ...state.look ?? DEFAULT_LOOK, ...patch } }))
  return <>
    <Choice<Surface> legend="Style" name="look-surface" value={look.surface === 'glass' ? 'glass' : 'flat'} options={[['flat', 'Flat'], ['glass', 'Glass']]} onChange={surface => set({ surface })} />
    {look.surface === 'glass' && <Choice<Backdrop> legend="Backdrop" name="look-backdrop" value={look.backdrop} options={[['ember', 'Ember'], ['graphite', 'Graphite'], ['dusk', 'Dusk']]} onChange={backdrop => set({ backdrop })} />}
  </>
}
