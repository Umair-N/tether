import { useCallback, useEffect, useState } from 'react'
import { SelectMenu } from './SelectMenu'
import { errorText } from './api'
import type { Api, SavedAccount, SavedAccounts, UsageAccount } from './api'
import { usePolling } from './hooks'
import { useHost } from './host'
import { LoginLink } from './LoginLink'
import { CopyButton } from './Messages'

type LoginState = { phase: 'idle' | 'waiting_code' | 'waiting_browser' | 'done' | 'failed'; url: string | null; user_code: string | null; message: string; account: UsageAccount | null }

export function AccountLogin({ agent, api, account, onDone }: { agent: 'claude' | 'codex'; api: Api; account?: UsageAccount | null; onDone: () => void }) {
  const host = useHost()
  const fetcher = useCallback((signal: AbortSignal) => api<LoginState>(`login/state?agent=${agent}`, undefined, signal), [api, agent])
  const { data, error: pollError, refresh } = usePolling(fetcher, 1500)
  const [state, setState] = useState<LoginState>()
  const [choosing, setChoosing] = useState(false)
  const [method, setMethod] = useState('claudeai')
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [previousData, setPreviousData] = useState(data)
  if (data !== previousData) { setPreviousData(data); if (data) setState(data) }
  useEffect(() => { if (state?.phase === 'done') onDone() }, [state?.phase, onDone])
  const active = state?.phase === 'waiting_code' || state?.phase === 'waiting_browser'
  const current = state?.phase === 'done' ? state.account : account
  const name = agent === 'claude' ? 'Claude' : 'Codex'
  async function run(action: 'start' | 'code' | 'cancel') {
    setPending(true); setError('')
    try {
      const body = action === 'start' ? { agent, ...(agent === 'claude' ? { method, email } : {}) } : action === 'code' ? { agent, code } : { agent }
      setState(await api<LoginState>(`login/${action}`, body)); setChoosing(false)
      if (action === 'code') setCode('')
      await refresh()
    } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  const [saved, setSaved] = useState<SavedAccount[]>([])
  return <div className="account-login">
    {!active && !choosing && <SavedList agent={agent} api={api} onList={setSaved} onDone={onDone} />}
    {current && saved.length < 2 && <p className="usage-account">{[current.email, current.plan && `${current.plan} plan`].filter(Boolean).join(' · ')}</p>}
    {!active && !choosing && <button type="button" className="secondary" disabled={pending || !state} onClick={() => setChoosing(true)}>{saved.length > 1 ? 'Sign in to another account' : 'Switch account'}</button>}
    {choosing && <form onSubmit={event => { event.preventDefault(); void run('start') }}>
      <p className="login-warning">Switching signs in every {name} session on {host} to the new account. Running jobs, such as codex exec lanes, can fail when they next refresh their sign-in. Switch when no long job runs.</p>
      {agent === 'claude' && <>
        <label htmlFor={`${agent}-login-method`}>Sign-in method</label>
        <SelectMenu id={`${agent}-login-method`} value={method} disabled={pending} onChange={setMethod} options={[{ value: 'claudeai', label: 'Claude subscription' }, { value: 'console', label: 'Anthropic Console' }, { value: 'sso', label: 'SSO' }]} />
        <label htmlFor={`${agent}-login-email`}>Email (optional)</label>
        <input id={`${agent}-login-email`} type="email" autoComplete="email" maxLength={320} value={email} disabled={pending} onChange={event => setEmail(event.target.value)} />
      </>}
      <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setChoosing(false)}>Cancel</button><button className="primary" disabled={pending}>{pending ? 'Starting…' : 'Start sign-in'}</button></div>
    </form>}
    {active && <>
      {state.message && <p className="hint" role="status">{state.message}</p>}
      {state.url && <LoginLink url={state.url} label={`Open ${name} sign-in`} />}
      {agent === 'claude' && state.phase === 'waiting_code' && <form onSubmit={event => { event.preventDefault(); void run('code') }}>
        <label htmlFor="claude-login-code">Sign-in code</label>
        <input id="claude-login-code" type="password" autoComplete="off" autoCapitalize="none" spellCheck={false} value={code} maxLength={2000} disabled={pending} onChange={event => setCode(event.target.value)} />
        <button className="primary" disabled={pending || !code.trim()}>Submit code</button>
      </form>}
      {agent === 'codex' && state.user_code && <div className="login-device"><code>{state.user_code}</code><CopyButton text={state.user_code} label="Copy code" className="copy-chip" wide /></div>}
      <button type="button" className="secondary" disabled={pending} onClick={() => void run('cancel')}>Cancel</button>
    </>}
    {state?.phase === 'failed' && <button type="button" className="secondary" disabled={pending} onClick={() => void run('cancel')}>Cancel</button>}
    {state?.message && !active && <p className="hint" role="status">{state.message}</p>}
    {(error || pollError) && <p className="error" role="alert">{error || pollError}</p>}
  </div>
}

const pct = (account: SavedAccount, label: string) => account.windows.find(window => window.label === label)?.percent

// Accounts saved with ccswap on the server. "Use" makes one the active account for every session of that agent.
function SavedList({ agent, api, onList, onDone }: { agent: 'claude' | 'codex'; api: Api; onList: (list: SavedAccount[]) => void; onDone: () => void }) {
  const host = useHost()
  const [list, setList] = useState<SavedAccount[]>([])
  const [confirm, setConfirm] = useState<SavedAccount | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [available, setAvailable] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const name = agent === 'claude' ? 'Claude' : 'Codex'
  const show = useCallback((data: SavedAccounts) => { const next = data.available ? data[agent] : []; setAvailable(data.available); setList(next); onList(next) }, [agent, onList])
  useEffect(() => { void api<SavedAccounts>('accounts').then(show, () => {}) }, [api, show])
  async function use(account: SavedAccount) {
    setPending(true); setError(''); setNote('')
    try {
      show(await api<SavedAccounts>('accounts/switch', { agent, number: account.number }))
      setConfirm(null); setNote(agent === 'codex' ? `Now using ${account.email}. Restart Codex panes to use it.` : `Now using ${account.email}. Running Claude sessions switch on their next request.`)
      onDone()
    } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  async function refresh() {
    setPending(true); setError('')
    try { show(await api<SavedAccounts>('accounts/refresh', { agent })); setLoaded(true) }
    catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  if (!available) return null
  return <div className="saved-accounts">
    {!loaded && <><button type="button" className="secondary" disabled={pending} onClick={() => void refresh()}>{pending ? 'Loading…' : 'Load saved accounts'}</button>
      <p className="hint">Loading can refresh the sign-in that running jobs use.</p></>}
    {loaded && list.length < 2 && <p className="hint">No other saved {name} accounts.</p>}
    {list.length > 1 && <ul aria-label={`Saved ${name} accounts`}>{list.map(account => {
      const week = pct(account, '7d'), five = pct(account, '5h')
      return <li key={account.number} className={account.active ? 'active' : undefined}>
        <span className="saved-who"><strong>{account.email ?? `Account ${account.number}`}</strong>
          <small>{[account.org && account.org !== `${account.email}'s Organization` ? account.org : account.plan, week != null && `week ${Math.round(week)}%`, five != null && `5-hour ${Math.round(five)}%`].filter(Boolean).join(' · ')}</small></span>
        {account.active ? <span className="saved-badge">In use</span>
          : <button type="button" className="secondary" disabled={pending || account.disabled} aria-label={`Use ${account.email ?? `account ${account.number}`}`} onClick={() => { setConfirm(account); setError(''); setNote('') }}>Use</button>}
      </li>})}</ul>}
    {confirm && <div className="saved-confirm" role="group" aria-label="Confirm switch">
      <p className="login-warning">Switch every {name} session on {host} to {confirm.email}?{agent === 'codex' ? ' Codex panes need a restart to use it.' : ' Running sessions switch on their next request.'}{(pct(confirm, '7d') ?? 0) >= 90 ? ` This account has used ${Math.round(pct(confirm, '7d')!)}% of its weekly limit.` : ''}</p>
      <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setConfirm(null)}>Cancel</button><button type="button" className="primary" disabled={pending} onClick={() => void use(confirm)}>{pending ? 'Switching…' : 'Switch'}</button></div>
    </div>}
    {note && <p className="hint" role="status">{note}</p>}
    {error && <p className="error" role="alert">{error}</p>}
  </div>
}
