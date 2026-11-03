"""The Changes and Files side panels: what changed in the Git repositories of a tab's folder, and a read-only folder tree.

Read-only. Git runs with no input, a time limit, no optional locks (so `status` does not rewrite the index), no fsmonitor,
no external diff or textconv commands and no filter commands (a repo's config could name any program there). Nothing here
commits, fetches or runs hooks. Folders must be under the home folder and outside folders that hold keys or tokens.
"""
import os
import re
import subprocess
import time
from pathlib import Path

import media_files

GIT_TIMEOUT = 5
MAX_REPOS = 40
MAX_DEPTH = 3
SCAN_SECONDS = 2.0
MAX_FILES = 500
MAX_DIFF = 400_000
MAX_ENTRIES = 1000
COUNT_LIMIT = 512 * 1024  # untracked files larger than this get no line count
# Folders never searched for repositories: dependencies, build output and caches.
SKIP = {'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build', 'target', '.next', '.cache', '.tox', '.mypy_cache',
        '.pytest_cache', '.gradle', 'Pods', 'vendor', 'site-packages'}
BINARY = 'binary'
FILTER_KEY = re.compile(r'^filter\.(.+)\.(clean|smudge|process)$')


def home():
    return Path(os.path.expanduser('~')).resolve()


def shown(path, base=None):
    """A path as people see it: <home>/x becomes ~/x."""
    base = base or home()
    path = Path(path)
    return '~' if path == base else '~/' + str(path.relative_to(base)) if base in path.parents else str(path)


def private(path, base):
    return any(part in media_files.PRIVATE for part in path.relative_to(base).parts) if base in path.parents else False


def folder_path(folder, base=None):
    """The real folder for `folder` (absolute or ~/...), or ValueError with a message for the panel."""
    base = Path(base).resolve() if base else home()
    folder = (folder or '').strip()
    if not folder or '\0' in folder or len(folder) > 1024:
        raise ValueError('This tab has no folder.')
    if folder == '~' or folder.startswith('~/'):
        folder = str(base) + folder[1:]
    path = Path(folder)
    if not path.is_absolute():
        raise ValueError('This tab has no folder.')
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError('That folder is not on the server.') from exc
    if path != base and base not in path.parents:
        raise ValueError('Tether shows folders in your home folder only.')
    if private(path, base):
        raise ValueError('Tether does not show that folder.')
    if not path.is_dir():
        raise ValueError('That is not a folder.')
    return path


def git(repo, *args, timeout=GIT_TIMEOUT, safe=()):
    """Run a read-only git command in `repo`. Returns (exit code, stdout bytes)."""
    env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0', 'GIT_PAGER': 'cat', 'PAGER': 'cat',
           'LC_ALL': 'C'}
    env.pop('GIT_DIR', None)
    env.pop('GIT_WORK_TREE', None)
    command = ['git', '-c', 'core.quotepath=off', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
               '-c', 'diff.external=', *safe, '-C', str(repo), *args]
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return -1, b''
    return done.returncode, done.stdout


def no_filters(repo):
    """-c options that blank every filter driver the repo's config names, so git never starts one."""
    code, out = git(repo, 'config', '-z', '--get-regexp', r'^filter\..*\.(clean|smudge|process)$')
    options = []
    for item in out.split(b'\0') if code == 0 else []:
        key = item.split(b'\n', 1)[0].decode(errors='replace')
        if FILTER_KEY.match(key):
            options += ['-c', key + '=']
    return tuple(options)


def toplevel(folder):
    """The root of the Git repository that holds `folder`, or None."""
    code, out = git(folder, 'rev-parse', '--show-toplevel')
    if code != 0 or not out.strip():
        return None
    try:
        return Path(out.decode().strip()).resolve()
    except (OSError, UnicodeDecodeError):
        return None


def find_repos(folder, base=None):
    """Repositories below `folder` (depth <= 3), as (paths, truncated). Dependency, cache and hidden folders are skipped."""
    base = base or home()
    found, queue, started, truncated = [], [(folder, 0)], time.monotonic(), False
    while queue:
        current, depth = queue.pop(0)
        if time.monotonic() - started > SCAN_SECONDS:
            truncated = True
            break
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name.lower())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith('.') or entry.name in SKIP or not entry.is_dir(follow_symlinks=False):
                continue
            path = Path(entry.path)
            if os.path.exists(path / '.git'):
                found.append(path)
                if len(found) >= MAX_REPOS:
                    return found, True
            elif depth + 1 < MAX_DEPTH:
                queue.append((path, depth + 1))
    return found, truncated


def parse_status(out):
    """(branch info, {path: entry}) from `git status --porcelain=v2 --branch -z`."""
    branch = {'branch': None, 'ahead': None, 'behind': None, 'upstream': None}
    files = {}
    items = out.decode('utf-8', errors='replace').split('\0')
    at = 0
    while at < len(items):
        item = items[at]
        at += 1
        if item.startswith('# branch.head '):
            head = item[14:]
            branch['branch'] = None if head == '(detached)' else head
        elif item.startswith('# branch.upstream '):
            branch['upstream'] = item[18:]
        elif item.startswith('# branch.ab '):
            parts = item[12:].split()
            if len(parts) == 2:
                branch['ahead'], branch['behind'] = int(parts[0].lstrip('+')), int(parts[1].lstrip('-'))
        elif item.startswith(('1 ', '2 ', 'u ')):
            fields = item.split(' ', 10 if item[0] == 'u' else 9 if item[0] == '2' else 8)
            xy, path = fields[1], fields[-1]
            entry = {'path': path, 'status': letter(xy, item[0]), 'staged': xy[0] not in '.', 'unstaged': xy[1] not in '.'}
            if item[0] == '2':
                entry['from'] = items[at]
                at += 1
            files[path] = entry
        elif item.startswith('? '):
            files[item[2:]] = {'path': item[2:], 'status': '?', 'staged': False, 'unstaged': True}
    return branch, files


def letter(xy, kind):
    if kind == 'u' or 'U' in xy:
        return 'U'
    for mark in xy:
        if mark != '.':
            return {'T': 'M'}.get(mark, mark)
    return 'M'


def parse_numstat(out):
    """{path: (added, deleted) or None for binary} from `git diff --numstat -z`."""
    counts = {}
    items = out.decode('utf-8', errors='replace').split('\0')
    at = 0
    while at < len(items):
        item = items[at]
        at += 1
        if not item:
            continue
        parts = item.split('\t', 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if not path:  # a rename: the old and new paths follow
            path = items[at + 1] if at + 1 < len(items) else ''
            at += 2
        counts[path] = None if added == '-' else (int(added), int(deleted))
    return counts


def has_head(repo, safe):
    return git(repo, 'rev-parse', '--verify', '--quiet', 'HEAD', safe=safe)[0] == 0


def count_lines(path):
    """Lines in a small untracked text file; BINARY for binary data; None when too large or unreadable."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > COUNT_LIMIT:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b'\0' in data[:8000]:
        return BINARY
    return data.count(b'\n') + (1 if data and not data.endswith(b'\n') else 0)


def repo_changes(root, folder, base=None):
    """One repository: branch, ahead/behind and its changed files with +/- line counts (staged and unstaged together)."""
    safe = no_filters(root)
    result = {'id': str(root.relative_to(folder)) if folder in root.parents else '.', 'name': root.name, 'path': shown(root, base), 'files': [], 'truncated': False}
    code, out = git(root, 'status', '--porcelain=v2', '--branch', '-z', '--untracked-files=normal', '--ignore-submodules=all', safe=safe)
    if code != 0:
        result['error'] = 'Git could not read this repository.'
        return result
    branch, files = parse_status(out)
    result.update(branch)
    if files:
        head = has_head(root, safe)
        options = ('--numstat', '-z', '-M', '--no-ext-diff', '--no-textconv', '--ignore-submodules=all')
        counts = parse_numstat(git(root, 'diff', 'HEAD', *options, safe=safe)[1]) if head else {}
        if not head:
            for extra in (git(root, 'diff', '--cached', *options, safe=safe)[1], git(root, 'diff', *options, safe=safe)[1]):
                for path, value in parse_numstat(extra).items():
                    old = counts.get(path, (0, 0))
                    counts[path] = None if value is None or old is None else (old[0] + value[0], old[1] + value[1])
        counted = 0
        for path in sorted(files):
            entry = files[path]
            if path in counts:
                value = counts[path]
                entry['binary'] = value is None
                entry['added'], entry['deleted'] = value if value else (None, None)
            elif entry['status'] == '?' and not path.endswith('/') and counted < 200 and not media_files.SECRET.search(Path(path).name):
                counted += 1
                lines = count_lines(root / path)
                entry['binary'] = lines == BINARY
                entry['added'], entry['deleted'] = (lines, 0) if isinstance(lines, int) else (None, None)
            if len(result['files']) >= MAX_FILES:
                result['truncated'] = True
                break
            result['files'].append(entry)
    return result


def changes(folder, base=None):
    """Changes for a tab's folder. In a repository: that repository. Otherwise every repository found below the folder."""
    base = Path(base).resolve() if base else home()
    path = folder_path(folder, base)
    root = toplevel(path)
    if root and (root == base or base in root.parents) and not private(root, base):
        return {'folder': shown(path, base), 'mode': 'repo', 'repos': [repo_changes(root, path, base)], 'truncated': False}
    repos, truncated = find_repos(path, base)
    return {'folder': shown(path, base), 'mode': 'parent', 'repos': [repo_changes(repo, path, base) for repo in repos], 'truncated': truncated}


def pick_repo(folder, repo, base):
    """The repository `repo` names for this folder: '.' (or the folder's own repo) or a folder path below it."""
    path = folder_path(folder, base)
    own = toplevel(path)
    if repo in ('', '.'):
        if not own:
            raise ValueError('This folder is not in a Git repository.')
        return own, path
    if '\0' in repo or repo.startswith('/') or '..' in Path(repo).parts:
        raise ValueError('Choose a repository in this folder.')
    target = (path / repo)
    try:
        target = target.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError('That repository is gone.') from exc
    if path not in target.parents or private(target, base) or toplevel(target) != target:
        raise ValueError('Choose a repository in this folder.')
    return target, path


def diff(folder, repo, name, base=None):
    """The diff of one changed file (staged and unstaged together), at most 400 KB. Untracked files show as all added."""
    base = Path(base).resolve() if base else home()
    root, _ = pick_repo(folder, repo or '.', base)
    name = (name or '').strip()
    if not name or '\0' in name or name.startswith('/') or '..' in Path(name).parts or len(name) > 1024:
        raise ValueError('Choose a file.')
    if '.git' in Path(name).parts:
        raise ValueError('Choose a file.')
    if media_files.SECRET.search(Path(name).name):
        raise ValueError('Tether does not show files that may hold secrets.')
    safe = no_filters(root)
    code, out = git(root, 'status', '--porcelain=v2', '-z', '--untracked-files=all', '--ignore-submodules=all', '--', ':(literal)' + name, safe=safe)
    _, files = parse_status(out) if code == 0 else ({}, {})
    entry = files.get(name)
    if not entry:
        return {'path': name, 'diff': '', 'binary': False, 'truncated': False, 'note': 'This file has no changes.'}
    if entry['status'] == '?':
        return untracked(root, name)
    specs = [':(literal)' + name] + ([':(literal)' + entry['from']] if entry.get('from') else [])
    options = ('--no-color', '--no-ext-diff', '--no-textconv', '-M', '--ignore-submodules=all')
    if has_head(root, safe):
        text = git(root, 'diff', 'HEAD', *options, '--', *specs, safe=safe)[1]
    else:
        text = git(root, 'diff', '--cached', *options, '--', *specs, safe=safe)[1] + git(root, 'diff', *options, '--', *specs, safe=safe)[1]
    return shape(name, text)


def shape(name, text):
    truncated = len(text) > MAX_DIFF
    body = text[:MAX_DIFF].decode('utf-8', errors='replace')
    if truncated:
        body = body.rsplit('\n', 1)[0]
    binary = bool(re.search(r'^Binary files .* differ$', body, re.M)) or 'GIT binary patch' in body
    return {'path': name, 'diff': '' if binary else body, 'binary': binary, 'truncated': truncated,
            'note': 'Binary file. Tether does not show binary changes.' if binary else None}


def untracked(root, name):
    path = root / name
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return {'path': name, 'diff': '', 'binary': False, 'truncated': False, 'note': 'That file is gone.'}
    if root not in real.parents or not real.is_file():
        return {'path': name, 'diff': '', 'binary': False, 'truncated': False, 'note': 'Tether shows new files in this repository only.'}
    with open(real, 'rb') as handle:
        data = handle.read(MAX_DIFF + 1)
    if b'\0' in data[:8000]:
        return {'path': name, 'diff': '', 'binary': True, 'truncated': False, 'note': 'Binary file. Tether does not show binary changes.'}
    truncated = len(data) > MAX_DIFF
    lines = data[:MAX_DIFF].decode('utf-8', errors='replace').split('\n')
    if truncated:
        lines = lines[:-1]
    elif lines and lines[-1] == '':
        lines.pop()
    head = ['new file', '--- /dev/null', '+++ b/' + name, '@@ -0,0 +1,%d @@' % len(lines)]
    return {'path': name, 'diff': '\n'.join(head + ['+' + line for line in lines]) + '\n', 'binary': False, 'truncated': truncated, 'note': None}


def listing(folder, sub='', base=None):
    """One folder level for the Files panel: folders first, then files. Hides .git, secret-looking names and private folders."""
    base = Path(base).resolve() if base else home()
    root = folder_path(folder, base)
    sub = (sub or '').strip().strip('/')
    if '\0' in sub or '..' in Path(sub).parts or sub.startswith('~'):
        raise ValueError('Choose a folder in this tab.')
    path = root / sub if sub else root
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError('That folder is gone.') from exc
    if path != root and root not in path.parents:
        raise ValueError('Choose a folder in this tab.')
    if private(path, base) or not path.is_dir():
        raise ValueError('Tether does not show that folder.')
    entries = []
    try:
        items = list(os.scandir(path))
    except OSError as exc:
        raise ValueError('Tether cannot read that folder.') from exc
    for entry in items:
        name = entry.name
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if name == '.git' or (is_dir and name in media_files.PRIVATE) or (not is_dir and media_files.SECRET.search(name)):
            continue
        item = {'name': name, 'dir': is_dir}
        if not is_dir:
            try:
                item['size'] = entry.stat().st_size
            except OSError:
                item['size'] = None
        entries.append(item)
    entries.sort(key=lambda item: (not item['dir'], item['name'].lower()))
    return {'folder': shown(root, base), 'path': str(path.relative_to(root)) if path != root else '',
            'entries': entries[:MAX_ENTRIES], 'truncated': len(entries) > MAX_ENTRIES}
