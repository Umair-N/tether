"""The Files explorer: browse, preview, download and change files under one root folder (MOBILE_TMUX_FILES_ROOT, else home).

Paths go in and out as ~/... where ~ is the root. Every path is resolved (symlinks too) and must stay inside the root.

Secret paths (folders in media_files.PRIVATE, file names that match media_files.SECRET) are hidden. POST files/secrets opens a
10-minute window, kept on the server, in which list, stat and preview show them. Downloads, zips, search and every write stay
blocked for secret paths, window or not.

Writes never follow symlinks after the root check: folders are opened one component at a time from the root with O_NOFOLLOW,
and every change uses those folder descriptors (openat-style). A symlink as the item to change is refused. Delete moves items
to the freedesktop trash (~/.local/share/Trash) with a .trashinfo file; nothing is deleted for good.
"""
import codecs
import datetime
import json
import mimetypes
import os
import pwd
import re
import shutil
import stat
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import media_files
import prefs_sync
import repo_panel

PREFIX = '/mobile/api/files/'
UPLOAD_LIMIT = 2 * 1024 ** 3
CHUNK = 1024 * 1024
JSON_LIMIT = 262144
MAX_PATHS = 1000
MAX_ENTRIES = 5000
SCAN_LIMIT = 50000
SEARCH_HITS = 500
SEARCH_SECONDS = 5.0
SIZE_SECONDS = 15.0
SECRET_WINDOW = 600
TEXT_BYTES = 256 * 1024
TEXT_LINES = 2000
LINE_CHARS = 2000
IMAGE_LIMIT = 25 * 1024 * 1024
ITEMS_LIMIT = 10000
PINNED_LIMIT = 200
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK: opening a named pipe (FIFO) to read would otherwise wait for a writer forever. It changes nothing for regular files.
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
# Folders search does not go into (their own names can still match).
SEARCH_SKIP = repo_panel.SKIP | {'.git'}

KINDS = {
    'image': {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.ico', '.avif', '.heic', '.tif', '.tiff'},
    'video': {'.mp4', '.webm', '.mov', '.m4v', '.mkv', '.avi'},
    'audio': {'.mp3', '.m4a', '.wav', '.ogg', '.flac', '.aac', '.opus'},
    'pdf': {'.pdf'},
    'archive': {'.zip', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.zst', '.7z', '.rar', '.jar', '.whl', '.deb', '.dmg'},
    'code': {'.py', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.css', '.scss', '.html', '.htm', '.xml', '.sh', '.zsh',
             '.bash', '.sql', '.go', '.rs', '.java', '.kt', '.swift', '.c', '.h', '.cc', '.cpp', '.hpp', '.rb', '.php',
             '.lua', '.tf', '.graphql', '.proto', '.vue', '.svelte', '.ipynb', '.diff', '.patch'},
}
TEXT_SUFFIXES = (media_files.TEXT_SUFFIXES - KINDS['code'] - KINDS['image']) | {'.rst', '.env.example'}
INLINE_TYPES = {'image', 'video', 'audio', 'pdf'}

clock = time.time  # tests replace this to move time
_window = {'until': 0.0}
_window_lock = threading.Lock()


class FilesError(ValueError):
    """An answer for the page: HTTP status, a short code and a plain sentence."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


def invalid(message):
    return FilesError(400, 'invalid', message)


def gone():
    return FilesError(404, 'missing', 'That file or folder is gone.')


def outside():
    return FilesError(403, 'outside', 'Tether shows files in your home folder only.')


def hidden_secret():
    return FilesError(403, 'secret', 'Tether keeps this hidden because it may hold keys or tokens.')


def changed():
    return FilesError(409, 'changed', 'That folder changed while Tether worked. Try again.')


# ---- the secret window ----

def secrets_state():
    until = _window['until']
    on = clock() < until
    return {'on': on, 'expires_at': until if on else None}


def set_secrets(on):
    if not isinstance(on, bool):
        raise invalid('Send {"on": true} or {"on": false}.')
    with _window_lock:
        _window['until'] = clock() + SECRET_WINDOW if on else 0.0
    return secrets_state()


def window_open():
    return clock() < _window['until']


# ---- paths ----

def files_root(root=None):
    return Path(root or os.environ.get('MOBILE_TMUX_FILES_ROOT') or os.path.expanduser('~')).resolve()


def shown(path, root):
    path = Path(path)
    return '~' if path == root else '~/' + str(path.relative_to(root))


def parts_of(path, root):
    return () if path == root else Path(path).relative_to(root).parts


def inside(path, root):
    return path == root or root in path.parents


def secret_parts(parts, is_dir):
    """True when a folder on the way is private, or the item itself is a private folder or a secret-looking file."""
    if not parts:
        return False
    if any(part in media_files.PRIVATE for part in parts[:-1]):
        return True
    last = parts[-1]
    return last in media_files.PRIVATE if is_dir else bool(media_files.SECRET.search(last))


def is_secret(path, root, is_dir):
    return secret_parts(parts_of(path, root), is_dir)


def lexical(text, root):
    """The absolute path that `text` (~/... or absolute) names, with . and .. folded. Not resolved yet."""
    if not isinstance(text, str) or not text or '\0' in text or len(text) > 4096:
        raise invalid('Choose a file or folder.')
    if text == '~' or text.startswith('~/'):
        text = str(root) + text[1:]
    if not os.path.isabs(text):
        raise invalid('Use a path that starts with ~/.')
    path = Path(os.path.normpath(text))
    if not inside(path, root):
        raise outside()
    return path


def locate(text, root, allow_secret=False):
    """(lexical path, real path, is_dir, secret) for a read. A secret path passes only when allowed and the window is open."""
    lex = lexical(text, root)
    try:
        real = lex.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise gone() from exc
    if not inside(real, root):
        raise outside()
    is_dir = real.is_dir()
    secret = is_secret(lex, root, is_dir) or is_secret(real, root, is_dir)
    if secret and not (allow_secret and window_open()):
        raise hidden_secret()
    return lex, real, is_dir, secret


def open_dir(root, real):
    """A descriptor for folder `real` (resolved, under root), opened one component at a time without following symlinks."""
    fd = os.open(root, DIR_FLAGS)
    try:
        for part in parts_of(real, root):
            following = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = following
    except OSError as exc:
        os.close(fd)
        raise changed() if not isinstance(exc, FileNotFoundError) else gone() from exc
    return fd


def open_read(root, real):
    """A read descriptor for regular file `real` that follows no symlink on the way."""
    folder = open_dir(root, real.parent)
    try:
        fd = os.open(real.name, READ_FLAGS, dir_fd=folder)
    except FileNotFoundError as exc:
        raise gone() from exc
    except OSError as exc:
        raise changed() from exc
    finally:
        os.close(folder)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise invalid('That is not a file.')
    return fd


def check_name(name):
    if not isinstance(name, str) or name in ('', '.', '..') or '/' in name or '\0' in name or len(name.encode()) > 255:
        raise invalid('Use a name without "/" (at most 255 bytes).')
    return name


def numbered(name, is_dir):
    """Candidate names: name, then "stem (2).ext", "stem (3).ext", ..."""
    stem, ext = (name, '') if is_dir else os.path.splitext(name)
    base, start = stem, 2
    found = re.fullmatch(r'(.*) \((\d{1,6})\)', stem)
    if found:
        base, start = found.group(1), int(found.group(2)) + 1
    yield name
    for number in range(start, start + 10000):
        yield '%s (%d)%s' % (base, number, ext)


def exists_at(folder, name):
    try:
        os.stat(name, dir_fd=folder, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def claim(name, is_dir, create):
    """Run create(candidate) for name, "name (2)", ... until one does not raise FileExistsError. Returns (name, result)."""
    for candidate in numbered(name, is_dir):
        if len(candidate.encode()) > 255:
            break
        try:
            return candidate, create(candidate)
        except FileExistsError:
            continue
    raise FilesError(409, 'exists', 'Too many items with that name.')


def free_name(folder, name, is_dir):
    for candidate in numbered(name, is_dir):
        if len(candidate.encode()) <= 255 and not exists_at(folder, candidate):
            return candidate
    raise FilesError(409, 'exists', 'Too many items with that name.')


# ---- kinds and entries ----

def kind_of(name, is_dir):
    if is_dir:
        return 'folder'
    suffix = os.path.splitext(name)[1].lower()
    for kind, suffixes in KINDS.items():
        if suffix in suffixes:
            return kind
    if suffix in TEXT_SUFFIXES or name in media_files.TEXT_NAMES or name.lower().endswith(('rc', '.example')):
        return 'text'
    return 'other'


def mime_of(name, is_dir):
    if is_dir:
        return 'inode/directory'
    return mimetypes.guess_type(name)[0] or ('text/plain' if kind_of(name, False) in ('text', 'code') else 'application/octet-stream')


def raw_url(path_text):
    return PREFIX + 'raw?path=' + quote(path_text, safe='/~')


def entry_row(name, path_text, is_dir, link, st, secret):
    return {'name': name, 'path': path_text, 'dir': is_dir, 'link': link, 'size': None if is_dir else st.st_size,
            'modified': st.st_mtime, 'kind': kind_of(name, is_dir), 'hidden': name.startswith('.'), 'secret': secret,
            'git': None}


def scan_entry(dirent, folder_lex, root, show_hidden, window):
    """One list/search row for a scandir entry, or None when it is left out."""
    name = dirent.name
    if name.startswith('.') and not show_hidden:
        return None
    try:
        link = dirent.is_symlink()
        if link:
            target = Path(dirent.path).resolve(strict=True)
            if not inside(target, root):
                return None
            st = target.stat()
        else:
            target = Path(dirent.path)
            st = dirent.stat(follow_symlinks=False)
    except (OSError, RuntimeError):
        return None
    is_dir = stat.S_ISDIR(st.st_mode)
    secret = is_secret(folder_lex / name, root, is_dir) or is_secret(target, root, is_dir)
    if secret and not window:
        return None
    return entry_row(name, shown(folder_lex / name, root), is_dir, link, st, secret)


def git_info(folder, root):
    """(repo ~path, branch, {child name: 'M'|'A'|'?'}) for a real folder inside a repository, else None."""
    top = repo_panel.toplevel(folder)
    if not top or not inside(top, root) or is_secret(top, root, True):
        return None
    rel = '' if folder == top else str(folder.relative_to(top))
    safe = repo_panel.no_filters(top)
    code, out = repo_panel.git(top, 'status', '--porcelain=v2', '--branch', '-z', '--untracked-files=normal',
                               '--ignore-submodules=all', '--', ':(literal)' + rel if rel else '.', safe=safe)
    if code != 0:
        return shown(top, root), None, {}
    branch, changes = repo_panel.parse_status(out)
    states, top_parts = {}, parts_of(top, root)
    for path, item in changes.items():
        if secret_parts(top_parts + Path(path).parts, path.endswith('/')):
            continue  # a badge must not reveal a hidden file
        if rel:
            if not path.startswith(rel + '/'):
                continue
            path = path[len(rel) + 1:]
        first, _, rest = path.partition('/')
        if not first:
            continue
        if rest or path.endswith('/'):  # something inside a child folder
            state = '?' if item['status'] == '?' else 'M'
        else:
            state = item['status'] if item['status'] in ('A', '?') else 'M'
        if states.get(first) != 'M':
            states[first] = 'M' if states.get(first) and states[first] != state else state
    return shown(top, root), branch.get('branch'), states


def sort_entries(entries, sort, order):
    if sort not in ('name', 'modified', 'size', 'kind') or order not in ('asc', 'desc'):
        raise invalid('Sort by name, modified, size or kind; order asc or desc.')
    by_name = lambda item: (item['name'].lower(), item['name'])
    keys = {'name': by_name, 'modified': lambda item: (item['modified'], by_name(item)),
            'size': lambda item: (item['size'] or 0, by_name(item)), 'kind': lambda item: (item['kind'], by_name(item))}
    folders = sorted((item for item in entries if item['dir']), key=keys['name' if sort in ('size', 'kind') else sort],
                     reverse=order == 'desc')
    others = sorted((item for item in entries if not item['dir']), key=keys[sort], reverse=order == 'desc')
    return folders + others


# ---- reads ----

def list_folder(text='~', hidden=False, sort='name', order='asc', root=None):
    root = files_root(root)
    lex, real, is_dir, secret = locate(text or '~', root, allow_secret=True)
    if not is_dir:
        raise invalid('That is not a folder.')
    window = window_open()
    entries, truncated = [], False
    try:
        with os.scandir(real) as items:
            for count, dirent in enumerate(items):
                if count >= SCAN_LIMIT:
                    truncated = True
                    break
                row = scan_entry(dirent, lex, root, hidden, window)
                if row:
                    entries.append(row)
    except OSError as exc:
        raise FilesError(403, 'unreadable', 'Tether cannot read that folder.') from exc
    info = None if secret else git_info(real, root)
    if info:
        for row in entries:
            row['git'] = info[2].get(row['name'])
    entries = sort_entries(entries, sort, order)
    truncated = truncated or len(entries) > MAX_ENTRIES
    return {'path': shown(lex, root), 'name': lex.name if lex != root else '~', 'parent': shown(lex.parent, root) if lex != root else None,
            'repo': info[0] if info else None, 'entries': entries[:MAX_ENTRIES], 'truncated': truncated, 'secrets': secrets_state()}


def stat_path(text, root=None):
    root = files_root(root)
    lex, real, is_dir, secret = locate(text, root, allow_secret=True)
    st = real.stat()
    items = None
    if is_dir:
        items = 0
        try:
            with os.scandir(real) as children:
                for _ in children:
                    items += 1
                    if items >= ITEMS_LIMIT:
                        break
        except OSError:
            items = None
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    git = None
    if not secret:
        info = git_info(real.parent, root) if real != root else None
        if info:
            git = {'repo': info[0], 'branch': info[1], 'state': info[2].get(real.name)}
        elif is_dir:
            own = git_info(real, root)
            git = {'repo': own[0], 'branch': own[1], 'state': None} if own else None
    name = lex.name if lex != root else '~'
    return {'path': shown(lex, root), 'name': name, 'parent': shown(lex.parent, root) if lex != root else None, 'dir': is_dir,
            'link': lex.is_symlink(), 'size': None if is_dir else st.st_size, 'items': items, 'modified': st.st_mtime,
            'owner': owner, 'mode': stat.filemode(st.st_mode)[1:], 'mime': mime_of(real.name, is_dir),
            'kind': kind_of(real.name, is_dir), 'hidden': name.startswith('.'), 'secret': secret, 'git': git}


def preview(text, root=None):
    root = files_root(root)
    lex, real, is_dir, secret = locate(text, root, allow_secret=True)
    path_text = shown(lex, root)
    if is_dir:
        return {'path': path_text, 'kind': 'none', 'reason': 'folder', 'size': None}
    kind = kind_of(real.name, False)
    try:
        fd = open_read(root, real)
    except FilesError:
        return {'path': path_text, 'kind': 'none', 'reason': 'unreadable', 'size': None}
    with os.fdopen(fd, 'rb') as handle:
        size = os.fstat(handle.fileno()).st_size
        if kind in INLINE_TYPES:
            if secret:
                return {'path': path_text, 'kind': 'none', 'reason': 'secret', 'size': size}
            if kind == 'image' and size > IMAGE_LIMIT:
                return {'path': path_text, 'kind': 'none', 'reason': 'too-large', 'size': size}
            return {'path': path_text, 'kind': kind, 'url': raw_url(path_text), 'mime': mime_of(real.name, False), 'size': size}
        data = handle.read(TEXT_BYTES + 1)
    if b'\0' in data[:8000]:
        return {'path': path_text, 'kind': 'none', 'reason': 'binary', 'size': size}
    cut = len(data) > TEXT_BYTES
    try:
        body = codecs.getincrementaldecoder('utf-8')().decode(data[:TEXT_BYTES], final=not cut)
    except UnicodeDecodeError:
        if kind not in ('text', 'code'):
            return {'path': path_text, 'kind': 'none', 'reason': 'binary', 'size': size}
        body = data[:TEXT_BYTES].decode('utf-8', errors='replace')
    lines = body.splitlines()
    if cut and lines:
        lines.pop()  # the last line may be cut in half
    truncated = cut or len(lines) > TEXT_LINES
    return {'path': path_text, 'kind': 'text', 'lines': [line[:LINE_CHARS] for line in lines[:TEXT_LINES]],
            'truncated': truncated, 'size': size}


def search(text='~', q='', hidden=False, root=None):
    root = files_root(root)
    if not isinstance(q, str) or not 1 <= len(q) <= 200:
        raise invalid('Type 1 to 200 characters to search.')
    lex, real, is_dir, _ = locate(text or '~', root, allow_secret=False)
    if not is_dir:
        raise invalid('That is not a folder.')
    needle, hits, truncated, timed_out = q.lower(), [], False, False
    started = time.monotonic()
    queue = [(lex, real)]
    while queue and not truncated:
        folder_lex, folder_real = queue.pop(0)
        try:
            with os.scandir(folder_real) as items:
                children = list(items)
        except OSError:
            continue
        for dirent in sorted(children, key=lambda item: item.name.lower()):
            if time.monotonic() - started > SEARCH_SECONDS:
                timed_out = True
                break
            row = scan_entry(dirent, folder_lex, root, hidden, window=False)
            if not row:
                continue
            if needle in row['name'].lower():
                hits.append(row)
                if len(hits) >= SEARCH_HITS:
                    truncated = True
                    break
            if row['dir'] and not row['link'] and row['name'] not in SEARCH_SKIP:
                queue.append((folder_lex / row['name'], folder_real / row['name']))
        if timed_out:
            break
    return {'path': shown(lex, root), 'q': q, 'hits': hits, 'truncated': truncated, 'timed_out': timed_out}


def places(session_rows=(), root=None):
    root = files_root(root)
    pinned = []
    stored = prefs_sync.load()['prefs'].get('files.pinned') or []
    for item in stored:
        if not isinstance(item, str):
            continue
        try:
            lex, real, is_dir, _ = locate(item, root)
            pinned.append({'path': shown(lex, root), 'name': '~' if lex == root else lex.name, 'exists': is_dir})
        except FilesError as exc:
            if exc.code == 'missing':
                pinned.append({'path': item, 'name': Path(item).name or item, 'exists': False})
    sessions = []
    for row in session_rows:
        cwd = row.get('cwd')
        try:
            lex, real, is_dir, _ = locate(cwd, root)
        except FilesError:
            continue
        if is_dir:
            sessions.append({'session': row.get('session'), 'name': row.get('name'), 'agent': row.get('agent'),
                             'cwd': shown(lex, root)})
    usage = shutil.disk_usage(root)
    return {'home': '~', 'pinned': pinned, 'sessions': sessions,
            'disk': {'total': usage.total, 'used': usage.used, 'free': usage.free}, 'secrets': secrets_state()}


# ---- zip and its size ----

def path_list(values):
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_PATHS or not all(isinstance(item, str) for item in values):
        raise invalid('Choose 1 to %d items.' % MAX_PATHS)
    return values


def zip_tops(texts, root):
    """[(arc name, real path, is_dir)] for the chosen items. A named secret path refuses the whole request."""
    tops, used = [], set()
    for text in path_list(texts):
        lex, real, is_dir, _ = locate(text, root, allow_secret=False)
        name = real.name or 'home'
        arc = next(candidate for candidate in numbered(name, is_dir) if candidate not in used)
        used.add(arc)
        tops.append((arc, real, is_dir))
    return tops


def walk_tree(real, root, counts, deadline=None):
    """(relative parts, is_dir, stat, folder fd, name) for everything below folder `real`. No symlinks, no secrets."""
    for dirpath, dirnames, filenames, dirfd in os.fwalk(real, follow_symlinks=False):
        base = Path(dirpath)
        keep = []
        for name in sorted(dirnames) + sorted(filenames):
            if deadline and time.monotonic() > deadline:
                counts['partial'] = True
                dirnames[:] = []
                return
            try:
                st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
            except OSError:
                counts['unreadable'] += 1
                continue
            if stat.S_ISLNK(st.st_mode):
                counts['links'] += 1
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            if is_secret(base / name, root, is_dir):
                counts['secret'] += 1
                continue
            if not is_dir and not stat.S_ISREG(st.st_mode):
                counts['unreadable'] += 1
                continue
            if is_dir:
                keep.append(name)
            yield (base / name).relative_to(real).parts, is_dir, st, dirfd, name
        dirnames[:] = keep


def new_counts():
    return {'secret': 0, 'links': 0, 'unreadable': 0, 'partial': False}


def size_of(texts, root=None):
    root = files_root(root)
    tops = zip_tops(texts, root)
    counts, files, folders, total = new_counts(), 0, 0, 0
    deadline = time.monotonic() + SIZE_SECONDS
    for _, real, is_dir in tops:
        if not is_dir:
            files += 1
            total += real.stat().st_size
            continue
        folders += 1
        for _, child_dir, st, _, _ in walk_tree(real, root, counts, deadline):
            if child_dir:
                folders += 1
            else:
                files += 1
                total += st.st_size
        if counts['partial']:
            break
    skipped = {key: counts[key] for key in ('secret', 'links', 'unreadable')}
    notes = []
    if skipped['secret']:
        notes.append('%d secret item%s will be left out.' % (skipped['secret'], '' if skipped['secret'] == 1 else 's'))
    if skipped['links']:
        notes.append('%d link%s will be left out.' % (skipped['links'], '' if skipped['links'] == 1 else 's'))
    if skipped['unreadable']:
        notes.append('%d unreadable item%s will be left out.' % (skipped['unreadable'], '' if skipped['unreadable'] == 1 else 's'))
    return {'paths': [shown(lexical(text, root), root) for text in texts], 'files': files, 'folders': folders, 'bytes': total,
            'skipped': skipped, 'partial': counts['partial'], 'note': ' '.join(notes) or None}


def zip_time(st):
    stamp = time.localtime(max(st.st_mtime, 315532800))  # zip cannot hold dates before 1980
    return stamp[:6]


def write_zip(out, tops, root):
    """Write a zip of the chosen items to the stream `out` (needs no seek). Returns the skip counts."""
    counts = new_counts()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
        def add_file(arc, fd, st):
            info = zipfile.ZipInfo(arc, zip_time(st))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (st.st_mode & 0xFFFF) << 16
            with os.fdopen(fd, 'rb') as source, archive.open(info, 'w', force_zip64=True) as target:
                while chunk := source.read(CHUNK):
                    target.write(chunk)

        for arc, real, is_dir in tops:
            if not is_dir:
                fd = open_read(root, real)
                add_file(arc, fd, os.fstat(fd))
                continue
            archive.writestr(zipfile.ZipInfo(arc + '/', zip_time(real.stat())), b'')
            for parts, child_dir, st, dirfd, name in walk_tree(real, root, counts):
                name_in_zip = '/'.join((arc,) + parts)
                if child_dir:
                    archive.writestr(zipfile.ZipInfo(name_in_zip + '/', zip_time(st)), b'')
                    continue
                try:
                    fd = os.open(name, READ_FLAGS, dir_fd=dirfd)
                except OSError:
                    counts['unreadable'] += 1
                    continue
                add_file(name_in_zip, fd, st)
    return counts


class _Counting:
    """A write-only stream with tell(), so zipfile can write to a socket."""

    def __init__(self, raw):
        self.raw, self.at = raw, 0

    def write(self, data):
        self.raw.write(data)
        self.at += len(data)
        return len(data)

    def tell(self):
        return self.at

    def flush(self):
        self.raw.flush()


def disposition(name):
    plain = re.sub(r'[^A-Za-z0-9._ ()-]', '_', name) or 'download'
    return 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (plain, quote(name, safe=''))


def send_zip(handler, texts, root):
    tops = zip_tops(texts, root)
    name = (tops[0][0] if len(tops) == 1 else 'files') + '.zip'
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/zip')
    handler.send_header('Content-Disposition', disposition(name))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Connection', 'close')
    handler.end_headers()
    handler.close_connection = True
    try:
        write_zip(_Counting(handler.wfile), tops, root)
    except (OSError, FilesError, ValueError):
        pass  # headers are out: the cut stream shows as a failed download


def send_raw(handler, text, download, root):
    lex, real, is_dir, _ = locate(text, root, allow_secret=False)
    if is_dir:
        raise invalid('That is a folder. Download it as a zip.')
    fd = open_read(root, real)
    with os.fdopen(fd, 'rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        try:
            span = media_files.byte_range(handler.headers.get('Range'), size)
        except ValueError:
            handler.send_response(416)
            handler.send_header('Content-Range', 'bytes */%d' % size)
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            return
        kind = kind_of(real.name, False)
        if download:
            content_type = 'application/octet-stream'
        elif kind in INLINE_TYPES:
            content_type = mime_of(real.name, False)
        elif kind in ('text', 'code'):
            content_type = 'text/plain; charset=utf-8'
        else:
            content_type = 'application/octet-stream'
        start, end = span or (0, size - 1)
        handler.send_response(206 if span else 200)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Content-Length', str(max(0, end - start + 1)))
        handler.send_header('Accept-Ranges', 'bytes')
        if span:
            handler.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        handler.send_header('Cache-Control', 'private, no-cache')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Content-Security-Policy', 'sandbox')
        handler.send_header('Content-Disposition', disposition(real.name) if download else 'inline')
        handler.end_headers()
        stream.seek(start)
        left = end - start + 1
        try:
            while left > 0 and (chunk := stream.read(min(left, 262144))):
                handler.wfile.write(chunk)
                left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---- writes ----

class Item:
    """An existing item a write changes: its folder descriptor and name. Close it when done."""

    def __init__(self, lex, parent, name, folder, st):
        self.lex, self.parent, self.name, self.folder, self.st = lex, parent, name, folder, st
        self.is_dir = stat.S_ISDIR(st.st_mode)
        self.real = parent / name

    def close(self):
        os.close(self.folder)


def write_folder(text, root):
    """(lexical path, real path, descriptor) of a folder a write puts things into. Secret folders are always refused."""
    lex, real, is_dir, _ = locate(text, root, allow_secret=False)
    if not is_dir:
        raise invalid('Choose a folder.')
    return lex, real, open_dir(root, real)


def write_item(text, root):
    """The item at `text` for a change. Refuses the root, symlinks and secret paths."""
    lex = lexical(text, root)
    if lex == root:
        raise invalid('Tether does not change the home folder itself.')
    try:
        parent = lex.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise gone() from exc
    if not inside(parent, root):
        raise outside()
    folder = open_dir(root, parent)
    try:
        st = os.stat(lex.name, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError as exc:
        os.close(folder)
        raise gone() from exc
    if stat.S_ISLNK(st.st_mode):
        os.close(folder)
        raise FilesError(403, 'link', 'Tether does not change symlinks. Open the folder it points to instead.')
    is_dir = stat.S_ISDIR(st.st_mode)
    if is_secret(lex, root, is_dir) or is_secret(parent / lex.name, root, is_dir):
        os.close(folder)
        raise hidden_secret()
    return Item(lex, parent, lex.name, folder, st)


def refuse_secret_target(folder_lex, folder_real, name, is_dir, root):
    if secret_parts(parts_of(folder_lex, root) + (name,), is_dir) or secret_parts(parts_of(folder_real, root) + (name,), is_dir):
        raise hidden_secret()


def write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def disk_error(exc):
    if getattr(exc, 'errno', None) == 28:
        return FilesError(507, 'failed', 'The disk is full.')
    if getattr(exc, 'errno', None) == 18:
        return FilesError(500, 'failed', 'Tether cannot move items between disks.')
    if isinstance(exc, PermissionError):
        return FilesError(403, 'failed', 'You do not have permission to change that.')
    return FilesError(500, 'failed', 'Tether could not change that: %s.' % (exc.strerror or 'disk error'))


def upload(rfile, length, folder_text, filename, root=None):
    """Stream `length` bytes from rfile into a new file in the folder, in 1 MB chunks. Returns the saved name."""
    root = files_root(root)
    if length in (None, ''):
        raise FilesError(411, 'length', 'Send the file size (Content-Length).')
    try:
        length = int(length)
    except ValueError as exc:
        raise invalid('Send the file size (Content-Length).') from exc
    if length < 0:
        raise invalid('Send the file size (Content-Length).')
    if length > UPLOAD_LIMIT:
        raise FilesError(413, 'too-large', 'That file is too large (the limit is 2 GB).')
    try:
        name = unquote(filename or '', errors='strict')
    except UnicodeDecodeError as exc:
        raise invalid('Send the file name percent-encoded (UTF-8).') from exc
    check_name(name)
    lex, real, folder = write_folder(folder_text or '', root)
    try:
        refuse_secret_target(lex, real, name, False, root)
        final, fd = claim(name, False, lambda candidate: os.open(candidate, NEW_FILE_FLAGS, 0o644, dir_fd=folder))
        done = False
        try:
            left = length
            while left > 0:
                chunk = rfile.read(min(CHUNK, left))
                if not chunk:
                    raise invalid('The upload stopped before the end. Try again.')
                write_all(fd, chunk)
                left -= len(chunk)
            done = True
        except OSError as exc:
            raise disk_error(exc) from exc
        finally:
            os.close(fd)
            if not done:
                try:
                    os.unlink(final, dir_fd=folder)
                except OSError:
                    pass
    finally:
        os.close(folder)
    return {'path': shown(lex / final, root), 'name': final, 'size': length}


def make_folder(text, name, root=None):
    root = files_root(root)
    check_name(name)
    lex, real, folder = write_folder(text, root)
    try:
        refuse_secret_target(lex, real, name, True, root)
        final, _ = claim(name, True, lambda candidate: os.mkdir(candidate, 0o755, dir_fd=folder))
    except OSError as exc:
        raise disk_error(exc) from exc
    finally:
        os.close(folder)
    return {'path': shown(lex / final, root), 'name': final}


def rename(text, name, root=None):
    root = files_root(root)
    check_name(name)
    item = write_item(text, root)
    try:
        if name != item.name:
            refuse_secret_target(item.lex.parent, item.parent, name, item.is_dir, root)
            if exists_at(item.folder, name):
                raise FilesError(409, 'exists', 'An item with that name is already here.')
            os.rename(item.name, name, src_dir_fd=item.folder, dst_dir_fd=item.folder)
    except OSError as exc:
        raise disk_error(exc) from exc
    finally:
        item.close()
    return {'from': shown(item.lex, root), 'path': shown(item.lex.parent / name, root), 'name': name}


def copy_file(source_folder, name, st, target_folder, wanted):
    if not stat.S_ISREG(st.st_mode):
        raise invalid('Tether copies files and folders only.')
    source = os.open(name, READ_FLAGS, dir_fd=source_folder)
    try:
        if not stat.S_ISREG(os.fstat(source).st_mode):
            raise invalid('That is not a file.')
        final, target = claim(wanted, False, lambda candidate: os.open(candidate, NEW_FILE_FLAGS, st.st_mode & 0o777, dir_fd=target_folder))
        try:
            while chunk := os.read(source, CHUNK):
                write_all(target, chunk)
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        finally:
            os.close(target)
    finally:
        os.close(source)
    return final


def copy_tree(source_folder, name, st, target_folder, wanted, source_parts, counts):
    """Copy one item (file or folder) without following symlinks. Symlinks inside folders are copied as links;
    secret items inside folders are skipped. Returns the new name."""
    if not stat.S_ISDIR(st.st_mode):
        return copy_file(source_folder, name, st, target_folder, wanted)
    final, _ = claim(wanted, True, lambda candidate: os.mkdir(candidate, 0o700, dir_fd=target_folder))
    source = os.open(name, DIR_FLAGS, dir_fd=source_folder)
    try:
        target = os.open(final, DIR_FLAGS, dir_fd=target_folder)
        try:
            for child in sorted(os.listdir(source)):
                child_st = os.stat(child, dir_fd=source, follow_symlinks=False)
                parts = source_parts + (child,)
                if stat.S_ISLNK(child_st.st_mode):
                    os.symlink(os.readlink(child, dir_fd=source), child, dir_fd=target)
                elif secret_parts(parts, stat.S_ISDIR(child_st.st_mode)):
                    counts['secret'] += 1
                elif stat.S_ISDIR(child_st.st_mode) or stat.S_ISREG(child_st.st_mode):
                    copy_tree(source, child, child_st, target, child, parts, counts)
            os.chmod(target, st.st_mode & 0o777)
        finally:
            os.close(target)
    finally:
        os.close(source)
    return final


def duplicate(text, root=None):
    root = files_root(root)
    item = write_item(text, root)
    counts = {'secret': 0}
    try:
        final = copy_tree(item.folder, item.name, item.st, item.folder, item.name, parts_of(item.real, root), counts)
    except OSError as exc:
        raise disk_error(exc) from exc
    finally:
        item.close()
    return {'from': shown(item.lex, root), 'path': shown(item.lex.parent / final, root), 'name': final, 'skipped': counts}


def gather(texts, root):
    """Check every item before anything changes (exists, inside the root, not a symlink, not secret). Returns the paths."""
    texts = path_list(texts)
    for text in texts:
        write_item(text, root).close()
    return texts


def transfer(texts, to, move, root=None):
    """Copy or move items into folder `to`, with the "name (2)" rule. Everything is checked before anything changes."""
    root = files_root(root)
    texts = gather(texts, root)
    to_lex, to_real, target = write_folder(to, root)
    done, failed, counts = [], [], {'secret': 0}
    try:
        for text in texts:
            item = write_item(text, root)
            item.close()
            if item.is_dir and (to_real == item.real or item.real in to_real.parents):
                raise invalid('A folder cannot go inside itself.')
        for text in texts:
            try:
                item = write_item(text, root)
            except FilesError as exc:
                failed.append({'path': text, 'error': str(exc)})
                continue
            try:
                if move and item.parent == to_real:
                    final = item.name
                elif move:
                    final = free_name(target, item.name, item.is_dir)
                    os.rename(item.name, final, src_dir_fd=item.folder, dst_dir_fd=target)
                else:
                    final = copy_tree(item.folder, item.name, item.st, target, item.name, parts_of(item.real, root), counts)
                done.append({'from': shown(item.lex, root), 'path': shown(to_lex / final, root)})
            except (OSError, FilesError) as exc:
                failed.append({'path': shown(item.lex, root), 'error': str(disk_error(exc) if isinstance(exc, OSError) else exc)})
            finally:
                item.close()
    finally:
        os.close(target)
    result = {'done': done, 'failed': failed}
    if not move:
        result['skipped'] = counts
    return result


def trash_folders(root):
    """Descriptors for ~/.local/share/Trash/files and info, made if missing, opened without following symlinks."""
    fd = os.open(root, DIR_FLAGS)
    try:
        for part in ('.local', 'share', 'Trash'):
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            following = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = following
        opened = []
        for part in ('files', 'info'):
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            opened.append(os.open(part, DIR_FLAGS, dir_fd=fd))
        return opened
    finally:
        os.close(fd)


def trash_one(item, files_fd, info_fd, now=None):
    """Move one item into the trash with its .trashinfo file. Returns the name in the trash."""
    stamp = datetime.datetime.fromtimestamp(now if now is not None else clock()).strftime('%Y-%m-%dT%H:%M:%S')
    info = ('[Trash Info]\nPath=%s\nDeletionDate=%s\n' % (quote(str(item.real), safe='/'), stamp)).encode()

    def reserve(candidate):
        if exists_at(files_fd, candidate):
            raise FileExistsError(candidate)
        return os.open(candidate + '.trashinfo', NEW_FILE_FLAGS, 0o600, dir_fd=info_fd)

    final, fd = claim(item.name, item.is_dir, reserve)
    try:
        write_all(fd, info)
    finally:
        os.close(fd)
    try:
        os.rename(item.name, final, src_dir_fd=item.folder, dst_dir_fd=files_fd)
    except OSError:
        os.unlink(final + '.trashinfo', dir_fd=info_fd)
        raise
    return final


def delete(texts, root=None):
    root = files_root(root)
    texts = gather(texts, root)
    done, failed = [], []
    try:
        files_fd, info_fd = trash_folders(root)
    except OSError as exc:
        raise FilesError(500, 'failed', 'Tether could not open the trash folder.') from exc
    try:
        for text in texts:
            try:
                item = write_item(text, root)
            except FilesError as exc:
                failed.append({'path': text, 'error': str(exc)})
                continue
            try:
                done.append({'from': shown(item.lex, root), 'trash_name': trash_one(item, files_fd, info_fd)})
            except (OSError, FilesError) as exc:
                failed.append({'path': shown(item.lex, root), 'error': str(disk_error(exc) if isinstance(exc, OSError) else exc)})
            finally:
                item.close()
    finally:
        os.close(files_fd)
        os.close(info_fd)
    return {'done': done, 'failed': failed}


# ---- HTTP ----

def answer_error(handler, exc):
    handler.respond(exc.status, {'error': str(exc), 'code': exc.code})


def serve_get(handler, path, session_rows=None):
    """GET /mobile/api/files/<action>. The caller has checked sign-in."""
    query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)

    def one(key, default=''):
        return query.get(key, [default])[0]

    action = path[len(PREFIX):]
    try:
        root = files_root()
        if action == 'list':
            handler.respond(200, list_folder(one('path', '~'), one('hidden') == '1', one('sort', 'name'), one('order', 'asc'), root))
        elif action == 'stat':
            handler.respond(200, stat_path(one('path'), root))
        elif action == 'preview':
            handler.respond(200, preview(one('path'), root))
        elif action == 'raw':
            send_raw(handler, one('path'), one('download') == '1', root)
        elif action == 'size':
            handler.respond(200, size_of(query.get('path', []), root))
        elif action == 'zip':
            send_zip(handler, query.get('path', []), root)
        elif action == 'search':
            handler.respond(200, search(one('path', '~'), one('q'), one('hidden') == '1', root))
        elif action == 'places':
            handler.respond(200, places(session_rows() if session_rows else (), root))
        else:
            handler.respond(404, {'error': 'Not found', 'code': 'missing'})
    except FilesError as exc:
        answer_error(handler, exc)
    except OSError:
        handler.respond(500, {'error': 'Tether could not read that.', 'code': 'failed'})


def serve_post(handler, path):
    """POST /mobile/api/files/<action>. The caller has checked sign-in, Origin and the X-Codex-Mobile header."""
    action = path[len(PREFIX):]
    try:
        root = files_root()
        if action == 'upload':
            try:
                handler.respond(200, upload(handler.rfile, handler.headers.get('Content-Length'), handler.headers.get('X-Folder'),
                                            handler.headers.get('X-Filename'), root))
            except FilesError:
                handler.close_connection = True  # the body may be unread
                raise
            return
        if action not in ('mkdir', 'rename', 'duplicate', 'copy', 'move', 'delete', 'secrets'):
            handler.respond(404, {'error': 'Not found', 'code': 'missing'})
            return
        try:
            size = int(handler.headers.get('Content-Length', '0'))
        except ValueError:
            size = 0
        if not 0 < size <= JSON_LIMIT:
            handler.close_connection = True
            raise FilesError(413, 'too-large', 'That request is too large.')
        try:
            body = json.loads(handler.rfile.read(size))
        except (ValueError, UnicodeError) as exc:
            raise invalid('Send a JSON body.') from exc
        if not isinstance(body, dict):
            raise invalid('Send a JSON object.')
        if action == 'mkdir':
            result = make_folder(body.get('path'), body.get('name'), root)
        elif action == 'rename':
            result = rename(body.get('path'), body.get('name'), root)
        elif action == 'duplicate':
            result = duplicate(body.get('path'), root)
        elif action in ('copy', 'move'):
            result = transfer(body.get('paths'), body.get('to'), action == 'move', root)
        elif action == 'delete':
            result = delete(body.get('paths'), root)
        else:
            result = set_secrets(body.get('on'))
        handler.respond(200, result)
    except FilesError as exc:
        answer_error(handler, exc)
    except OSError as exc:
        answer_error(handler, disk_error(exc))
