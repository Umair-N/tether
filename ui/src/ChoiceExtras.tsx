import { useCallback, useEffect, useRef, useState } from 'react'
import { Check } from 'lucide-react'
import type { AskBridge, ChoiceState } from './api'
import './ChoiceExtras.css'

// The tabs and the filter text the agent shows above a list. Read only: the keys below change them.
export function ChoiceMeta({ choice }: { choice: ChoiceState }) {
  if (!choice.tabs?.length && choice.filter === null) return null
  return <div className="choice-meta">
    {!!choice.tabs?.length && <ul className="choice-tabs" aria-label="Tabs">{choice.tabs.map(tab => <li key={tab.label} className={tab.active ? 'active' : ''} aria-current={tab.active || undefined}>{tab.label}</li>)}</ul>}
    {choice.filter !== null && <p className="choice-filter"><span>Filter</span>{choice.filter ? <code>{choice.filter}</code> : <em>empty</em>}</p>}
  </div>
}

const more = (count: number, arrow: string) => count === -1 ? `${arrow} more` : `${arrow} ${count} more`

// An unnumbered list (pickers): the mark, the ticked boxes and the hidden rows come from the server's choice record.
export function ChoiceList({ choice, label }: { choice: ChoiceState; label: string }) {
  return <ul className="picker-list" aria-label={label}>
    {choice.more_above !== 0 && <li className="picker-more">{more(choice.more_above, '↑')}</li>}
    {choice.items.map((item, at) => <li key={at} className={[item.selected ? 'current' : '', item.disabled ? 'disabled' : ''].filter(Boolean).join(' ')} aria-current={item.selected || undefined}>
      {item.checked !== undefined && <Box checked={item.checked} multi={choice.selection === 'multi'} />}
      <span className="choice-item-text">{item.text}{item.detail && <small>{item.detail}</small>}</span></li>)}
    {choice.more_below !== 0 && <li className="picker-more">{more(choice.more_below, '↓')}</li>}
  </ul>
}

// decorative: the form's real input already tells assistive tech its state.
export function Box({ checked, multi, decorative }: { checked: boolean; multi: boolean; decorative?: boolean }) {
  return <span className={`choice-box ${multi ? 'multi' : 'single'} ${checked ? 'on' : ''}`} {...decorative ? { 'aria-hidden': true } : { role: 'img', 'aria-label': checked ? 'Selected' : 'Not selected' }}>{checked && <Check size={12} strokeWidth={3} />}</span>
}

type Answers = Record<string, string | string[]>

function secondsLeft(expires: number) { return Math.max(0, Math.round(expires - Date.now() / 1000)) }

// Claude's question, sent to the phone by the ask bridge before the terminal draws it. Answers go back as data, not keys.
// "Answer in the terminal" gives the question back at once. The question stays here while the person uses the form:
// each tap or keystroke renews the server's deadline. Showing the form does not: only use proves a person is answering.
export function AskForm({ ask, disabled, onAnswer, onEngage }: { ask: AskBridge; disabled: boolean; onAnswer: (answers: Answers | null) => Promise<string | null>; onEngage?: () => Promise<number | null> }) {
  const [picked, setPicked] = useState<Record<number, string[]>>({})
  const [other, setOther] = useState<Record<number, string>>({})
  const [busy, setBusy] = useState<'send' | 'back' | ''>('')
  const [failed, setFailed] = useState('')
  // The newest deadline: from the attention record, or sooner from an engage reply. Never moves back.
  const [renewed, setRenewed] = useState(0)
  const expires = Math.max(ask.expires, renewed)
  const [left, setLeft] = useState(() => secondsLeft(expires))
  const lastEngage = useRef(0)
  const [identity, setIdentity] = useState({ id: ask.id, nonce: ask.nonce })
  if (identity.id !== ask.id || identity.nonce !== ask.nonce) {
    setIdentity({ id: ask.id, nonce: ask.nonce }); setPicked({}); setOther({}); setFailed(''); setRenewed(0)
  }
  useEffect(() => { lastEngage.current = 0 }, [ask.id, ask.nonce])
  // eslint-disable-next-line react-hooks/set-state-in-effect -- Resync the clock immediately when the deadline changes instead of waiting up to one second for the interval.
  useEffect(() => { const timer = setInterval(() => setLeft(secondsLeft(expires)), 1000); setLeft(secondsLeft(expires)); return () => clearInterval(timer) }, [expires])
  // At most one call per 5 s. A hidden page never renews: a phone left on a desk must not hold the question.
  const engage = useCallback(() => {  // at most one call per 5 s
    if (!onEngage || document.visibilityState !== 'visible' || Date.now() - lastEngage.current < 5000) return
    lastEngage.current = Date.now()
    void onEngage().then(next => { if (next) setRenewed(old => Math.max(old, next)) })
  }, [onEngage])
  const answerOf = (at: number) => {
    const typed = other[at]?.trim()
    const labels = picked[at] ?? []
    if (ask.questions[at].multiSelect) return typed ? [...labels, typed] : labels
    return typed || labels[0] || ''
  }
  const missing = ask.questions.findIndex((_, at) => { const value = answerOf(at); return Array.isArray(value) ? !value.length : !value })
  const toggle = (at: number, label: string, multi: boolean) => {
    setPicked(old => { const now = old[at] ?? []; return { ...old, [at]: multi ? now.includes(label) ? now.filter(item => item !== label) : [...now, label] : [label] } })
    if (!multi) setOther(old => ({ ...old, [at]: '' }))
  }
  const send = async (answers: Answers | null) => {
    if (busy) return
    setBusy(answers ? 'send' : 'back'); setFailed('')
    try { setFailed(await onAnswer(answers) ?? '') } finally { setBusy('') }
  }
  const submit = () => { if (missing === -1) void send(Object.fromEntries(ask.questions.map((item, at) => [item.question, answerOf(at)]))) }
  const off = disabled || !!busy || left === 0
  return <form className="ask-form" onSubmit={event => { event.preventDefault(); submit() }} onPointerDown={engage} onKeyDown={engage} onInput={engage}>
    {ask.questions.map((item, at) => {
      const multi = !!item.multiSelect
      const name = `ask-${ask.id}-${at}`
      return <fieldset key={at} className="ask-form-question">
        <legend>{ask.questions.length > 1 && <span className="ask-header">Question {at + 1} of {ask.questions.length}{item.header ? ` · ${item.header}` : ''}</span>}
          {ask.questions.length === 1 && item.header && <span className="ask-header">{item.header}</span>}
          <span className="ask-question">{item.question}</span>
          {multi && <span className="ask-form-hint">Choose one or more.</span>}</legend>
        {item.options.map(option => {
          const on = (picked[at] ?? []).includes(option.label)
          return <label key={option.label} className={`ask-form-option ${on ? 'on' : ''}`}>
            <input type={multi ? 'checkbox' : 'radio'} name={name} checked={on} disabled={off} onChange={() => toggle(at, option.label, multi)} />
            <Box checked={on} multi={multi} decorative />
            <span><strong>{option.label}</strong>{option.description && <small>{option.description}</small>}</span></label>
        })}
        <label className="ask-form-other"><span>{multi ? 'Add your own answer' : 'Or write your own answer'}</span>
          <input type="text" value={other[at] ?? ''} disabled={off} maxLength={2000} onChange={event => { const value = event.target.value; setOther(old => ({ ...old, [at]: value })); if (!multi && value.trim()) setPicked(old => ({ ...old, [at]: [] })) }} /></label>
      </fieldset>
    })}
    <p className="ask-form-time" role="timer" aria-live="off">{left === 0 ? 'The question is back in the terminal.' : onEngage ? renewed ? `Stays here while you answer. Back to the terminal after ${ask.idle ?? 60} s without a tap (${left} s left).` : `Back to the terminal in ${left} s unless you tap here. Each tap keeps it here ${ask.idle ?? 60} s more.` : `The question goes back to the terminal in ${left} s.`}</p>
    {failed && <p className="info-error" role="alert">{failed}</p>}
    <div className="ask-form-actions">
      <button type="submit" className="primary" disabled={off || missing !== -1}>{busy === 'send' ? 'Sending…' : ask.questions.length > 1 ? 'Send answers' : 'Send answer'}</button>
      <button type="button" disabled={off} onClick={() => void send(null)}>{busy === 'back' ? 'Sending…' : 'Answer in the terminal'}</button>
    </div>
    {missing !== -1 && ask.questions.length > 1 && <p className="ask-form-hint">Answer question {missing + 1} to send.</p>}
  </form>
}

// Tether's terminal view hides the form, but the terminal itself shows only a spinner while the bridge waits.
// This bar says where the question is and gives it back to the terminal in one tap.
export function AskBar({ agent, disabled, onOpen, onTerminal }: { agent: string; disabled: boolean; onOpen: () => void; onTerminal: () => Promise<string | null> }) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState('')
  return <div className="ask-bar" role="status">
    <p>{agent} asks a question. It is waiting in the chat view, not in the terminal.</p>
    <div className="ask-form-actions">
      <button type="button" className="primary" onClick={onOpen}>Answer in chat</button>
      <button type="button" disabled={disabled || busy} onClick={() => { setBusy(true); setFailed(''); void onTerminal().then(error => setFailed(error ?? '')).finally(() => setBusy(false)) }}>{busy ? 'Sending…' : 'Show in terminal'}</button>
    </div>
    {failed && <p className="info-error" role="alert">{failed}</p>}
  </div>
}
