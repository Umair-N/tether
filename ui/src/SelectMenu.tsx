import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode, UIEvent } from 'react'
import { Check, ChevronDown, Folder, Search } from 'lucide-react'
import './SelectMenu.css'

export interface MenuOption { value: string; label: string; detail?: string; count?: number; group?: string; search?: string }

const SEARCH_FROM = 9  // A shorter list is quick to read.
const GAP = 4, EDGE = 8, MAX_HEIGHT = 440

/**
 * The app's dropdown, in place of a native select. A long list gets a search field.
 * A sheet's swipe, click and key handlers must not see what happens inside the panel.
 */
export function SelectMenu({ id, value, options, onChange, label, icon, className = 'sm-field', disabled, minWidth = 220, searchLabel = 'Search', emptyText = 'Nothing matches.' }: {
  id?: string; value: string; options: MenuOption[]; onChange: (value: string) => void; label?: string; icon?: ReactNode
  className?: string; disabled?: boolean; minWidth?: number; searchLabel?: string; emptyText?: string
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [activeValue, setActiveValue] = useState(value)
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const field = useRef<HTMLInputElement>(null)
  const listId = useId()
  const selected = options.find(option => option.value === value)
  const searchable = options.length >= SEARCH_FROM
  const shown = useMemo(() => {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean)
    if (!terms.length) return options
    return options.filter(option => { const text = `${option.label} ${option.detail ?? ''} ${option.search ?? ''}`.toLowerCase(); return terms.every(term => text.includes(term)) })
  }, [options, query])
  const active = shown.find(option => option.value === activeValue) ?? shown[0]
  const optionId = (index: number) => `${listId}-${index}`
  const activeId = active ? optionId(shown.indexOf(active)) : undefined

  const show = () => { setQuery(''); setActiveValue(value); setOpen(true) }
  const close = (focus: boolean) => { setOpen(false); if (focus) trigger.current?.focus() }
  const pick = (next: string) => { close(true); if (next !== value) onChange(next) }

  useLayoutEffect(() => {
    const box = panel.current, button = trigger.current
    if (!open || !box || !button) return
    box.showPopover?.()
    const place = () => {
      const anchor = button.getBoundingClientRect(), view = window.visualViewport
      const top = view?.offsetTop ?? 0, height = view?.height ?? innerHeight
      const below = top + height - anchor.bottom - GAP - EDGE, above = anchor.top - top - GAP - EDGE
      const up = below < 240 && above > below
      const width = Math.min(Math.max(anchor.width, minWidth), innerWidth - 2 * EDGE)
      box.style.width = `${width}px`
      box.style.left = `${Math.max(EDGE, Math.min(anchor.left, innerWidth - width - EDGE))}px`
      box.style.maxHeight = `${Math.min(up ? above : below, MAX_HEIGHT)}px`
      box.style.top = up ? 'auto' : `${anchor.bottom + GAP}px`
      box.style.bottom = up ? `${innerHeight - anchor.top + GAP}px` : 'auto'
    }
    place()
    const scrolled = (event: Event) => { if (!(event.target instanceof Node && box.contains(event.target))) place() }
    window.addEventListener('resize', place)
    window.addEventListener('scroll', scrolled, true)
    window.visualViewport?.addEventListener('resize', place)
    window.visualViewport?.addEventListener('scroll', place)
    // A phone keeps its keyboard closed until the person taps the search field.
    if (searchable && matchMedia('(pointer: fine)').matches) field.current?.focus()
    else list.current?.focus()
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', scrolled, true)
      window.visualViewport?.removeEventListener('resize', place)
      window.visualViewport?.removeEventListener('scroll', place)
    }
    // Place the panel once per opening. Later size changes come through the listeners.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => {
      const node = event.target as Node
      if (!panel.current?.contains(node) && !trigger.current?.contains(node)) close(false)
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])

  useLayoutEffect(() => {
    if (open && activeId) document.getElementById(activeId)?.scrollIntoView({ block: 'nearest' })
  }, [open, activeId])

  const keys = (event: KeyboardEvent) => {
    const at = active ? shown.indexOf(active) : -1
    const move = (index: number) => setActiveValue(shown[index]?.value ?? activeValue)
    switch (event.key) {
      case 'ArrowDown': move(Math.min(at + 1, shown.length - 1)); break
      case 'ArrowUp': move(Math.max(at - 1, 0)); break
      case 'Home': if (event.target === field.current) return; move(0); break
      case 'End': if (event.target === field.current) return; move(shown.length - 1); break
      case 'Enter': if (active) pick(active.value); break
      case 'Escape': close(true); break
      case 'Tab': close(true); event.stopPropagation(); return
      default: return
    }
    event.preventDefault()
    event.stopPropagation()
  }
  const keep = (event: UIEvent) => event.stopPropagation()

  return <>
    <button ref={trigger} id={id} type="button" className={`sm-trigger ${className}`} disabled={disabled} title={selected?.search ?? selected?.detail}
      aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? listId : undefined} aria-label={label ? `${label}: ${selected?.search ?? selected?.label ?? ''}` : undefined}
      onClick={() => open ? close(false) : show()}
      onKeyDown={event => { if (!open && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) { event.preventDefault(); event.stopPropagation(); show() } }}>
      {icon}<span>{selected?.label ?? value}</span><ChevronDown className="sm-chevron" size={14} aria-hidden="true" />
    </button>
    {open && <div ref={panel} className="sm-panel" popover="manual" onKeyDown={keys} onClick={keep} onContextMenu={keep} onTouchStart={keep} onTouchMove={keep} onTouchEnd={keep}>
      {searchable && <label className="sm-search"><Search size={14} aria-hidden="true" />
        <input ref={field} type="search" role="combobox" value={query} placeholder={searchLabel} aria-label={searchLabel} aria-expanded="true" aria-controls={listId} aria-autocomplete="list" aria-activedescendant={activeId} autoComplete="off" onChange={event => setQuery(event.target.value)} />
      </label>}
      <div ref={list} id={listId} className="sm-list" role="listbox" tabIndex={-1} aria-label={label ?? searchLabel} aria-activedescendant={activeId}>
        {shown.map((option, index) => <div key={option.value} role="presentation">
          {option.group && option.group !== shown[index - 1]?.group && <div className="sm-group" role="presentation">{option.group}</div>}
          <div id={optionId(index)} role="option" aria-selected={option.value === value} title={option.search ?? option.detail} className={`sm-option ${option === active ? 'active' : ''}`}
            onPointerMove={() => setActiveValue(option.value)} onClick={() => pick(option.value)}>
            <span className="sm-check">{option.value === value && <Check size={15} aria-hidden="true" />}</span>
            <span className="sm-text"><span className="sm-label">{option.label}</span>{option.detail && <bdi className="sm-detail" dir="ltr">{option.detail}</bdi>}</span>
            {option.count !== undefined && <span className="sm-count">{option.count}</span>}
          </div>
        </div>)}
        {!shown.length && <p className="sm-none" role="status">{emptyText}</p>}
      </div>
    </div>}
  </>
}

const TEMPORARY = /^(\/tmp|\/private\/tmp|\/var\/folders|\/private\/var\/folders)(\/|$)/

/** "~/apps/tether" gives the name "tether" and the parent "~/apps". */
function folderParts(path: string) {
  const trimmed = path.length > 1 ? path.replace(/\/+$/, '') : path
  const cut = trimmed.lastIndexOf('/')
  if (cut < 0) return { name: trimmed, parent: '' }
  if (cut === 0) return { name: trimmed.slice(1) || '/', parent: trimmed === '/' ? '' : '/' }
  return { name: trimmed.slice(cut + 1), parent: trimmed.slice(0, cut) }
}

/** Filter by folder. Real project folders come first, and temporary agent folders go last. */
export function FolderMenu({ value, folders, onChange }: { value: string; folders: { dir: string; count: number }[]; onChange: (dir: string) => void }) {
  const options = useMemo<MenuOption[]>(() => {
    const row = ({ dir, count }: { dir: string; count: number }, group?: string): MenuOption => {
      const { name, parent } = folderParts(dir)
      return { value: dir, label: name, detail: parent || undefined, count: count || undefined, group, search: dir }
    }
    const known = folders.some(item => item.dir === value) || !value ? folders : [...folders, { dir: value, count: 0 }]
    return [
      { value: '', label: 'All folders' },
      ...known.filter(item => !TEMPORARY.test(item.dir)).map(item => row(item, 'Folders')),
      ...known.filter(item => TEMPORARY.test(item.dir)).map(item => row(item, 'Temporary')),
    ]
  }, [folders, value])
  return <SelectMenu value={value} options={options} onChange={onChange} label="Folder" className="cp-folder" minWidth={340} searchLabel="Search folders" emptyText="No folders match."
    icon={<Folder size={13} aria-hidden="true" />} />
}
