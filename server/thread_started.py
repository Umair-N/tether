"""Read a conversation's first timestamp without scanning its message history."""
from collections import OrderedDict
from datetime import datetime, timezone
import json
import os
import threading

HEAD_BYTES = 262144
CACHE_LIMIT = 512
_cache = OrderedDict()
_lock = threading.Lock()


def _utc(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except (ValueError, OverflowError):
        return None


def _read(path, agent):
    with open(path, 'rb') as stream:
        head = stream.read(HEAD_BYTES)
    first = None
    for line in head.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        value = _utc(record.get('created_at') if agent == 'antigravity' else record.get('timestamp'))
        if agent != 'codex' and value:
            return value
        if agent == 'codex':
            payload = record.get('payload')
            if record.get('type') == 'session_meta':
                start = value or (_utc(payload.get('timestamp')) if isinstance(payload, dict) else None)
                if start:
                    return start
            first = first or value
    return first


def started(entry):
    """Cache by path and inode. Appends keep a known start; unknown starts retry after writes."""
    agent, path = entry.get('agent'), entry.get('transcript')
    if agent not in ('claude', 'codex', 'antigravity') or not entry.get('thread') or not path or entry.get('ambiguous'):
        return None
    try:
        info = os.stat(path)
        key = (os.fspath(path), agent, info.st_dev, info.st_ino)
        with _lock:
            previous = _cache.get(key)
            if previous:
                size, modified, value = previous
                unchanged = (info.st_size, info.st_mtime_ns) == (size, modified)
                if unchanged or (value is not None and info.st_size > size):
                    _cache[key] = (info.st_size, info.st_mtime_ns, value)
                    _cache.move_to_end(key)
                    return value
            value = _read(path, agent)
            _cache[key] = (info.st_size, info.st_mtime_ns, value)
            _cache.move_to_end(key)
            while len(_cache) > CACHE_LIMIT:
                _cache.popitem(last=False)
            return value
    except (OSError, TypeError, ValueError):
        return None
