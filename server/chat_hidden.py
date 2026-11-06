"""Hide history rows without changing agent data."""
import json
import math
import os
import tempfile
import threading
import time

import history
import prefs_sync

_lock = threading.Lock()
MODES = ('collapsed', 'open', 'hidden')


def _chat_key(agent, identifier):
    if not isinstance(agent, str) or agent not in ('claude', 'codex'):
        raise ValueError('Choose Claude or Codex.')
    if not isinstance(identifier, str) or not history.UUID.fullmatch(identifier):
        raise ValueError('Use a valid chat id.')
    return agent + ':' + identifier


def _valid_dir(directory):
    return isinstance(directory, str) and bool(directory.strip()) and len(directory) <= 4096


def _load():
    defaults = {'chats': {}, 'dirs': {}, 'temporary': 'collapsed'}
    try:
        data = json.loads(prefs_sync.FILE.with_name('chat-hidden.json').read_text())
    except (OSError, ValueError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    for kind, limit in (('chats', 5000), ('dirs', 500)):
        entries = data.get(kind, {})
        if not isinstance(entries, dict):
            continue
        valid = {}
        for key, at in entries.items():
            if not isinstance(at, (int, float)) or isinstance(at, bool) or not math.isfinite(at):
                continue
            if kind == 'chats':
                try:
                    _chat_key(*key.split(':', 1))
                except (ValueError, TypeError):
                    continue
            elif not _valid_dir(key):
                continue
            valid[key] = at
        defaults[kind] = dict(sorted(valid.items(), key=lambda item: item[1])[-limit:])
    if data.get('temporary') in MODES:
        defaults['temporary'] = data['temporary']
    return defaults


def load():
    with _lock:
        return _load()


def _save(kind, key, hidden=True):
    with _lock:
        state = _load()
        if kind == 'temporary':
            state[kind] = key
        else:
            state[kind].pop(key, None)
            if hidden:
                state[kind][key] = time.time()
            limit = 5000 if kind == 'chats' else 500
            state[kind] = dict(sorted(state[kind].items(), key=lambda item: item[1])[-limit:])
        file = prefs_sync.FILE.with_name('chat-hidden.json')
        file.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=file.name + '.', suffix='.tmp', dir=file.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(state, stream)
            os.replace(temporary, file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def set_chat(agent, id, hidden):
    key = _chat_key(agent, id)
    if not isinstance(hidden, bool):
        raise ValueError('Use true or false for hidden.')
    _save('chats', key, hidden)


def set_dir(dir, hidden):
    if not _valid_dir(dir):
        raise ValueError('Use a non-empty folder path of at most 4096 characters.')
    if not isinstance(hidden, bool):
        raise ValueError('Use true or false for hidden.')
    _save('dirs', dir, hidden)


def set_temporary(mode):
    if mode not in MODES:
        raise ValueError('Choose collapsed, open, or hidden for temporary folders.')
    _save('temporary', mode)


def is_temporary(cwd):
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return False
    cwd = os.path.normpath(cwd)
    roots = ('/tmp', '/private/tmp', '/var/folders', '/private/var/folders', tempfile.gettempdir())
    return any(cwd == root or cwd.startswith(root.rstrip('/') + '/') for root in roots)
