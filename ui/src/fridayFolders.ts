import { distance, normalize } from './fridaySessions'

/** Select a project before Friday creates a tab. No terminal access. */
export function fridayChatFolder(
  panes: { dir: string; tmux_session?: string | null; label?: string }[],
  target: { dir: string; tmux_session?: string | null } | undefined,
  project: string | undefined,
  homeSession: string,
): { dir: string; project: string } | { error: string } {
  // Home is excluded in every session, not only Friday's home session.
  void homeSession
  // The sessions API displays home as ~. Also accept absolute home representations.
  const isHome = (dir: string) => dir === '~' || dir === '~/'
    || /^\/(?:home|Users)\/[^/]+\/?$/.test(dir) || /^\/root\/?$/.test(dir)
    || /^\/data\/data\/com\.termux\/files\/home\/?$/.test(dir)
  const nameOf = (dir: string) => dir.replace(/\/+$/, '').split('/').pop() || dir
  const folders = new Map<string, { dir: string; project: string; count: number }>()
  for (const pane of panes) {
    if (!pane.dir || isHome(pane.dir)) continue
    const known = folders.get(pane.dir)
    if (known) known.count++
    else folders.set(pane.dir, { dir: pane.dir, project: nameOf(pane.dir), count: 1 })
  }
  const entries = [...folders.values()].sort((a, b) => b.count - a.count)
  const names = [...new Set(entries.map(entry => entry.project))]
  const error = { error: `Which project? Known projects: ${names.slice(0, 8).join(', ')}` }
  const result = (entry: { dir: string; project: string }) => ({ dir: entry.dir, project: entry.project })
  const wanted = project?.trim()
  if (!wanted) return target?.dir && !isHome(target.dir)
    ? { dir: target.dir, project: nameOf(target.dir) } : error
  if (wanted.startsWith('/') || wanted.startsWith('~/')) return { dir: wanted, project: nameOf(wanted) }
  const query = normalize(wanted)
  if (!query) return error
  const steps = [
    (name: string) => name === query,
    (name: string) => name.startsWith(query),
    (name: string) => name.includes(query),
    (name: string) => distance(query, name) <= Math.max(query.length, name.length) * 0.4,
  ]
  for (const matches of steps) {
    const candidates = entries.filter(entry => matches(normalize(entry.project)))
    if (!candidates.length) continue
    if (candidates.length === 1) return result(candidates[0])
    // A parent folder wins over its own subfolders: "acme" means the repo, not its worktrees.
    const root = candidates.find(entry => candidates.every(other => other === entry || other.dir.startsWith(`${entry.dir.replace(/\/+$/, '')}/`)))
    if (root) return result(root)
    if (new Set(candidates.map(entry => entry.project)).size > 1) return error
    return candidates[0].count > candidates[1].count ? result(candidates[0]) : error
  }
  return error
}
