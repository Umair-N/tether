import { ChevronDown, ClipboardPaste, PencilLine, X } from 'lucide-react'
import './Pasted.css'

import type { Paste } from './pasteText'

function size(text: string) {
  const lines = text.split('\n').length
  return lines > 1 ? `${lines.toLocaleString()} lines` : `${text.length.toLocaleString()} chars`
}

interface Props { paste: Paste; open: boolean; onToggle: () => void; onEdit: () => void; onRemove: () => void }
export function PasteChip({ paste, open, onToggle, onEdit, onRemove }: Props) {
  const label = size(paste.text)
  const preview = paste.text.split('\n').find(line => line.trim())?.trim() ?? ''
  return <li className={open ? 'chip paste-chip open' : 'chip paste-chip'}>
    <button type="button" className="paste-toggle" aria-expanded={open} aria-label={`${open ? 'Hide' : 'Show'} pasted text, ${label}`} onClick={onToggle}>
      <ClipboardPaste size={14} aria-hidden="true" />
      <strong>{label}</strong>
      {!open && preview && <span className="paste-preview">{preview}</span>}
      <ChevronDown size={14} className="paste-chevron" aria-hidden="true" />
    </button>
    {open && <button type="button" aria-label="Edit pasted text in the message box" title="Edit as text" onClick={onEdit}><PencilLine size={14} /></button>}
    <button type="button" aria-label={`Remove pasted text, ${label}`} onClick={onRemove}><X size={14} /></button>
    {open && <pre className="paste-body" tabIndex={0} aria-label="Pasted text">{paste.text}</pre>}
  </li>
}
