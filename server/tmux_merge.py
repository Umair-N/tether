"""Merge tmux sessions and move panes between them, for drag and drop in the sidebar.

Sessions are named by tmux session id ($N) and panes by pane id (%N), the ids the session list already sends to the
page. Every move keeps the pane ids, so open chats stay attached. The terminal panel's own session
(side_shells.SESSION) is never a source or a target."""
import re
import subprocess
import sys
import time

import groups
import paths
import projects
import side_shells

PANE = re.compile(r'%[0-9]+')
SESSION = re.compile(r'\$[0-9]+')
GONE = 'That session or pane is gone. Refresh the list and try again.'


def _tmux(run, *args):
    return run([paths.TMUX, *args], capture_output=True, text=True, timeout=5)


def _info(run, target, fmt):
    result = _tmux(run, 'display-message', '-p', '-t', target, fmt)
    fields = result.stdout.strip().split('\t')
    if result.returncode or len(fields) != fmt.count('\t') + 1 or not all(fields):
        raise ValueError(GONE)
    return fields


def _session(run, session_id):
    """The session id, checked: it exists and it is not the terminal panel's session."""
    if not isinstance(session_id, str) or not SESSION.fullmatch(session_id):
        raise ValueError('Choose a session.')
    found, name = _info(run, session_id + ':', '#{session_id}\t#{session_name}')
    if found != session_id:
        raise ValueError(GONE)
    if name == side_shells.SESSION:
        raise ValueError('Terminal panel shells cannot be moved.')
    return session_id


def _pane(run, pane_id):
    """The session id that holds the pane, checked like _session."""
    if not isinstance(pane_id, str) or not PANE.fullmatch(pane_id):
        raise ValueError('Choose a pane.')
    found, session_id = _info(run, pane_id, '#{pane_id}\t#{session_id}')
    if found != pane_id:
        raise ValueError(GONE)
    return _session(run, session_id)


def _check(result, message):
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or message)


def _moved(run, session_id, pane):
    """What the page needs to offer a rename: the target session, its name, and a pane in it."""
    name, first = _info(run, session_id + ':', '#{session_name}\t#{pane_id}')
    return {'session': session_id, 'name': name, 'pane': pane or first}


def _names(run, session_id):
    """Pane id -> Tether pane name (tmux option @chat_name, '' when unnamed) for every pane of the session."""
    result = _tmux(run, 'list-panes', '-s', '-t', session_id + ':', '-F', '#{pane_id}\t#{@chat_name}')
    _check(result, GONE)
    return dict(line.split('\t', 1) for line in result.stdout.splitlines() if '\t' in line)


def _name_plan(run, source, target, panes):
    """Names for the moving panes that have no Tether name: the session they leave ('alpha', then 'alpha 2', ...),
    never a name already used in either session. tmux's own numbered session names ('3') carry no name, so they give none."""
    base = ' '.join(_info(run, source + ':', '#{session_name}')[0].split())[:34]
    if base.isdigit():
        return {}
    named = _names(run, source)
    taken = set(named.values()) | set(_names(run, target).values())
    plan, name, number = {}, base, 1
    for pane in panes:
        if named.get(pane):
            continue
        while name in taken:
            number += 1
            name = f'{base} {number}'
        plan[pane] = name
        taken.add(name)
    return plan


def _apply_names(run, plan):
    """No @chat_name_at is set, so the name counts as the oldest: a name given later in Claude or Codex still wins."""
    for pane, name in plan.items():
        _tmux(run, 'set-option', '-p', '-t', pane, '@chat_name', name)


KEEP = ('source', 'target')
CHOOSE = 'Both sessions have a project. Choose which one to keep.'


def _project(run, session_id):
    """The session's @project id (projects.py), or '' when it has none."""
    found = _tmux(run, 'display-message', '-p', '-t', session_id + ':', '#{@project}').stdout.strip()
    return found if projects.ID.fullmatch(found) else ''


def _project_plan(run, source, target, keep):
    """(project the merged session keeps, project to archive). Asked before any window moves, so a missing choice
    changes nothing. When only one session's project gives agents text, that one stays and nothing is lost."""
    if keep is not None and keep not in KEEP:
        raise ValueError('Choose which project to keep.')
    ours, theirs = _project(run, source), _project(run, target)
    if not ours or ours == theirs:
        return None, None
    if not theirs:
        return ours, None
    if keep is None:
        ours_text, theirs_text = projects.has_context(ours), projects.has_context(theirs)
        if ours_text and theirs_text:
            raise ValueError(CHOOSE)
        keep = 'source' if ours_text else 'target'
    return (ours, theirs) if keep == 'source' else (theirs, ours)


def _apply_project(run, target, kept, dropped):
    """The windows already moved, so a failure here does not fail the merge: an archive left in place is harmless."""
    if kept:
        _tmux(run, 'set-option', '-t', target, '@project', kept)
    if dropped:
        try:
            projects.archive(dropped)
        except (OSError, projects.ProjectError) as exc:
            print(f'merge: project {dropped} not archived: {exc}', file=sys.stderr, flush=True)


def merge_session(source, target, run=subprocess.run, keep=None):
    """Move every window of source into target, with its layout. The emptied source session closes;
    its name stays on the moved panes. keep ('source' or 'target') says whose project the merged session keeps;
    the other project's folder goes to the archive."""
    source, target = _session(run, source), _session(run, target)
    if source == target:
        raise ValueError('Drop the session on a different session.')
    kept, dropped = _project_plan(run, source, target, keep)
    windows = _tmux(run, 'list-windows', '-t', source + ':', '-F', '#{window_id}')
    _check(windows, GONE)
    plan = _name_plan(run, source, target, list(_names(run, source)))
    groups.merge_into(source, target, run)  # before the move, so no moved pane points to a missing group
    for window in windows.stdout.split():
        _check(_tmux(run, 'move-window', '-d', '-s', window, '-t', target + ':'), 'Could not move the window.')
    _apply_names(run, plan)
    _apply_project(run, target, kept, dropped)
    return _moved(run, target, None)


def move_pane(pane, target, run=subprocess.run):
    """Move one pane into a new window of target. A session left with no panes closes."""
    source, target = _pane(run, pane), _session(run, target)
    if source == target:
        raise ValueError('The pane is already in that session.')
    plan = _name_plan(run, source, target, [pane])
    _check(_tmux(run, 'break-pane', '-d', '-s', pane, '-t', target + ':'), 'Could not move the pane.')
    _ungroup(run, pane)
    _apply_names(run, plan)
    return _moved(run, target, pane)


def _ungroup(run, pane):
    """A pane that leaves its session goes to Unstacked: groups belong to one session (groups.py)."""
    _tmux(run, 'set-option', '-p', '-u', '-t', pane, '@group')


def _free_name(run, wanted=''):
    """The pane's Tether name when it has one (tmux turns '.' and ':' in a session name into '_'), else split-HHMMSS."""
    names = set(_tmux(run, 'list-sessions', '-F', '#{session_name}').stdout.split('\n'))
    base = re.sub(r'[.:]', '_', ' '.join(wanted.split())) or time.strftime('split-%H%M%S')
    name, n = base, 2
    while name in names:
        name, n = f'{base} {n}' if wanted.strip() else f'{base}-{n}', n + 1
    return name


def break_out(pane, run=subprocess.run):
    """Move one pane into a new session of its own, started in the pane's folder."""
    source = _pane(run, pane)
    count = _tmux(run, 'list-panes', '-s', '-t', source + ':', '-F', '#{pane_id}')
    _check(count, GONE)
    if len(count.stdout.split()) < 2:
        raise ValueError('That pane is already a session of its own.')
    folder = _info(run, pane, '#{pane_current_path}')[0]
    wanted = _names(run, source).get(pane, '')
    created = run([*paths.tmux_new_session(), 'new-session', '-d', '-s', _free_name(run, wanted), '-c', folder,
                   '-P', '-F', '#{session_id}\t#{pane_id}'], capture_output=True, text=True, timeout=5)
    _check(created, 'Could not start a session.')
    session_id, placeholder = created.stdout.strip().split('\t')
    moved = _tmux(run, 'break-pane', '-d', '-s', pane, '-t', session_id + ':')
    if moved.returncode:
        _tmux(run, 'kill-session', '-t', session_id)
        _check(moved, 'Could not move the pane.')
    _tmux(run, 'kill-pane', '-t', placeholder)
    _ungroup(run, pane)
    return _moved(run, session_id, pane)


def merge(kind, source, target, run=subprocess.run, keep=None):
    if kind == 'session':
        return merge_session(source, target, run, keep)
    if kind == 'pane':
        return move_pane(source, target, run)
    if kind == 'break':
        return break_out(source, run)
    raise ValueError('Unknown move.')
