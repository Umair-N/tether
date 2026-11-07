"""Create worktrees for new tabs, without ever removing user work."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess

CHECK_TIMEOUT = 5
ADD_TIMEOUT = 120


def _git(folder, *args, timeout=CHECK_TIMEOUT):
    env = dict(os.environ)
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
        env.pop(key, None)
    env.update(GIT_TERMINAL_PROMPT='0', LC_ALL='C')
    return subprocess.run(['git', '-C', str(folder), *args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, env=env, timeout=timeout)


def slug(branch):
    return re.sub(r'[^A-Za-z0-9._-]+', '-', branch).strip('-.')


def info(folder):
    try:
        inside = _git(folder, 'rev-parse', '--is-inside-work-tree')
        if inside.returncode or inside.stdout.strip() != 'true':
            return None
        result = _git(folder, 'worktree', 'list', '--porcelain')
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    first = result.stdout.split('\n\n', 1)[0].splitlines()
    if 'bare' in first or not first or not first[0].startswith('worktree '):
        return None
    main = Path(first[0][len('worktree '):])
    return {'main': str(main), 'base': str(main.parent / (main.name + '-wt'))}


def create(folder, branch):
    if not isinstance(branch, str) or not branch:
        raise ValueError('Enter a branch name for the new worktree.')
    repo = info(folder)
    if repo is None:
        raise ValueError(f'{folder} is not in a git repository.')
    main = repo['main']
    checked = _git(main, 'check-ref-format', '--branch', branch)
    if checked.returncode or checked.stdout.rstrip('\n') != branch:
        raise ValueError(f'"{branch}" is not a valid branch name.')
    if branch.endswith(';'):
        raise ValueError(f'"{branch}" ends in ";", which tmux cannot store. Choose another branch name.')
    name = slug(branch)
    if not name:
        raise ValueError(f'"{branch}" needs a letter or a number for its folder name.')
    if _git(main, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch).returncode == 0:
        raise ValueError(f'Branch "{branch}" already exists.')
    base = Path(repo['base'])
    path = base / name
    if os.path.lexists(path):
        raise ValueError(f'{path} already exists.')
    if os.path.lexists(base) and not base.is_dir():
        raise ValueError(f'{base} is not a folder.')
    head = _git(main, 'rev-parse', '--verify', '--quiet', 'HEAD^{commit}')
    if head.returncode:
        raise ValueError('The repository has no commits yet.')
    try:
        added = _git(main, 'worktree', 'add', '-b', branch, '--', str(path), head.stdout.strip(),
                     timeout=ADD_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f'Worktree creation timed out at {path}. Check it before retrying.') from exc
    if added.returncode:
        lines = added.stderr.strip().splitlines()
        raise RuntimeError('Could not create the worktree: ' + (lines[-1] if lines else 'git failed.'))
    return {'branch': branch, 'path': str(path)}


def from_body(body, folder):
    return create(folder, body['worktree']) if 'worktree' in body else None


@contextmanager
def keeping(made):
    try:
        yield
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        if made is None:
            raise
        raise RuntimeError(f'The worktree is ready at {made["path"]} on branch {made["branch"]}, '
                           f'but the agent did not start: {exc}') from exc
