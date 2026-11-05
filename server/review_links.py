"""Persistent worker/reviewer links and one bounded watcher step."""
import json
import os
import re
import tempfile
import threading
import time

import paths
from terminal_input import DraftError

FILE = paths.CACHE / 'review-links.json'
MAX_LINKS = 50
MAX_RETRIES = 30
_lock = threading.Lock()


def _read():
    try:
        links = json.loads(FILE.read_text())
    except (FileNotFoundError, ValueError):
        return []
    return links if isinstance(links, list) else []


def _write(links):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=FILE.parent,
                                         prefix=FILE.name + '.', delete=False) as stream:
            temporary = stream.name
            json.dump(links, stream)
        os.replace(temporary, FILE)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def add(*, worker, reviewer, reviewer_thread, name, agent):
    link = dict(worker=worker, reviewer=reviewer, reviewer_thread=reviewer_thread,
                name=name, agent=agent, created=time.time(), seen_working=False, worker_thread=None)
    with _lock:
        links = [item for item in _read() if isinstance(item, dict) and item.get('worker') != worker]
        _write((links + [link])[-MAX_LINKS:])


def reviewed_by():
    with _lock:
        return {link['worker']: link['reviewer'] for link in _read()
                if isinstance(link, dict)
                and all(isinstance(link.get(key), str) and re.fullmatch(r'%[0-9]+', link[key])
                        for key in ('worker', 'reviewer'))}


def remove(worker):
    with _lock:
        links = _read()
        remaining = [link for link in links if not isinstance(link, dict) or link.get('worker') != worker]
        if len(remaining) == len(links):
            return False
        _write(remaining)
        return True


def _step(link, entries, status, send, last_reply):
    worker = entries.get(link['worker'])
    reviewer = entries.get(link['reviewer'])
    if worker is None or reviewer is None or reviewer.get('thread') != link['reviewer_thread']:
        return None
    thread = worker.get('thread')
    if link['worker_thread'] is None:
        if thread:
            link['worker_thread'] = thread
    elif thread != link['worker_thread']:
        return None
    state = status(worker)['state']
    if state == 'Working':
        link['seen_working'] = True
        link.pop('retries', None)
    elif link['seen_working'] and state == 'Idle':
        reviewer_state = status(reviewer)['state']
        delivered = False
        if reviewer.get('writable') and reviewer_state not in ('Approval', 'Input needed', 'Unavailable'):
            agent = 'Claude' if link['agent'] == 'claude' else 'Codex'
            text = (f"[Tether review] The {agent} tab “{link['name']}” ({link['worker']}) finished a turn. "
                    'Review its work: check the changes and the claims, then tell me what is wrong or missing. '
                    f"Its last reply:\n\n{last_reply(worker)[:1500]}\n\nTranscript: {worker.get('transcript', '')}")
            try:
                send(reviewer, text, reviewer_state)
                delivered = True
            except (DraftError, RuntimeError):
                pass
        if delivered:
            link['seen_working'] = False
            link.pop('retries', None)
        else:
            link['retries'] = link.get('retries', 0) + 1
            if link['retries'] >= MAX_RETRIES:
                link['seen_working'] = False
                link.pop('retries', None)
    return link


def observe(entries, status, send, last_reply):
    """Callbacks run outside the store lock: send takes SEND_LOCK, while add already holds it."""
    try:
        with _lock:
            original = _read()
        by_pane = {entry['id']: entry for entry in entries}
        changes = []
        for link in original:
            try:
                updated = _step(dict(link), by_pane, status, send, last_reply)
            except Exception:  # a malformed link or one bad pane must not stop other reviews
                continue
            if updated != link:
                changes.append((link, updated))
        if not changes:
            return
        with _lock:
            current = _read()
            changed = False
            for old, updated in changes:
                if old in current:  # preserve any links added or replaced during a callback
                    index = current.index(old)
                    if updated is None:
                        current.pop(index)
                    else:
                        current[index] = updated
                    changed = True
            if changed:
                _write(current)
    except Exception:  # disk and discovery errors must never stop the monitor
        return
