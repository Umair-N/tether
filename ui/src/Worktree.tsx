import { useEffect, useRef, useState } from 'react'
import { GitBranch } from 'lucide-react'
import type { Api } from './api'
import './NewTask.css'
import './Worktree.css'

/** What the server made: the new branch and the folder of its worktree (server/worktrees.py). */
export interface MadeWorktree { branch: string; path: string }
export const worktreeNotice = (made: MadeWorktree) => `Worktree ready at ${made.path}`
// git worktree add can take far longer than a normal request (server/worktrees.py ADD_TIMEOUT is 120 s).
export const WORKTREE_TIMEOUT = 130000

// The folder name for a branch. Keep this rule equal to slug() in server/worktrees.py.
export const worktreeSlug = (branch: string) => branch.replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^[-.]+|[-.]+$/g, '')

/** The branch of a tab that Tether started in a new worktree. */
export function WorktreeMark({ branch }: { branch: string }) {
  return <span className="worktree-mark" title={`Worktree branch: ${branch}`}>
    <GitBranch size={11} aria-hidden="true" /><span className="worktree-mark-name"><span className="visually-hidden">Worktree branch </span><bdi dir="ltr">{branch}</bdi></span>
  </span>
}

/** session: the tab whose folder the new worktree comes from. Without it, the folder for new sessions.
 *  value: null while off. The control stays hidden unless the folder is in a git repository. */
export function WorktreeField({ api, session, value, onChange, disabled }: { api: Api; session?: string; value: string | null; onChange: (value: string | null) => void; disabled: boolean }) {
  const [base, setBase] = useState('')
  const input = useRef<HTMLInputElement>(null)
  useEffect(() => {
    const abort = new AbortController()
    api<{ repo: boolean; base?: string }>(`worktree-info${session ? `?session=${encodeURIComponent(session)}` : ''}`, undefined, abort.signal)
      .then(info => { if (info.repo && info.base) setBase(info.base) }, () => { /* no control without a known repository */ })
    return () => abort.abort()
  }, [api, session])
  if (!base) return null
  const on = value !== null
  const slug = worktreeSlug(value ?? '')
  return <div className="worktree-field">
    <label className="task-switch">
      <span><strong>New worktree</strong><small>The agent works on its own branch and folder.</small></span>
      <input type="checkbox" role="switch" checked={on} disabled={disabled}
        onChange={event => { onChange(event.target.checked ? '' : null); if (event.target.checked) requestAnimationFrame(() => input.current?.focus()) }} />
    </label>
    {on && <div className="worktree-branch">
      <input ref={input} value={value} disabled={disabled} aria-label="Branch name" placeholder="Branch name" maxLength={200}
        autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} dir="ltr" onChange={event => onChange(event.target.value)} />
      <small title={`${base}/${slug}`}><bdi dir="ltr">{base}/{slug || '…'}</bdi></small>
    </div>}
  </div>
}
