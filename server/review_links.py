"""Persistent worker/reviewer links and one bounded watcher step."""
import hashlib
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
REVIEW_WAIT = 450
MAX_ROUNDS = 3
GRACE = 30
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


def _digest(text):
    return hashlib.sha256((text or '').encode()).hexdigest()


def _step(link, entries, status, send, last_reply):
    worker = entries.get(link['worker'])
    reviewer = entries.get(link['reviewer'])
    if reviewer is not None and reviewer.get('thread') != link['reviewer_thread']:
        return None
    if worker is None or reviewer is None:
        return link if time.time() - link.get('created', 0) < GRACE else None
    thread = worker.get('thread')
    if isinstance(thread, str) and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', thread):
        if link.get('worker_thread') is None:
            link['worker_thread'] = thread
        elif thread != link['worker_thread']:
            return None
    state = status(worker)['state']
    if link.get('awaiting', False):
        reviewer_state = status(reviewer)['state']
        if reviewer_state == 'Working':
            link['reviewer_seen_working'] = True
        elif reviewer_state == 'Idle' and not link.get('reviewer_seen_working', False) \
                and _digest(last_reply(reviewer)) == link.get('reviewer_before'):
            # No turn seen yet. A turn shorter than one tick still changes the last reply; none for 30 ticks: give up.
            link['idle_ticks'] = link.get('idle_ticks', 0) + 1
            if link['idle_ticks'] >= 30:
                link['awaiting'] = False
        elif reviewer_state == 'Idle':
            reply = last_reply(reviewer).strip()
            if reply.upper().startswith('APPROVED') or link.get('rounds', 0) >= MAX_ROUNDS:
                link['awaiting'] = False
            elif state != 'Idle':
                link['wait_ticks'] = link.get('wait_ticks', 0) + 1
                if link['wait_ticks'] >= REVIEW_WAIT:
                    link['awaiting'] = False
            else:
                link.pop('wait_ticks', None)
                text = (f"[Tether review] Feedback from the reviewer tab ({link['reviewer']}):\n\n"
                        f'{reply[:3000]}\n\nFix what it raises, then stop and wait.')
                try:
                    send(worker, text, state)
                    link['rounds'] = link.get('rounds', 0) + 1
                    link['awaiting'] = False
                except (DraftError, RuntimeError):
                    link['retries'] = link.get('retries', 0) + 1
                    if link['retries'] >= MAX_RETRIES:
                        link['awaiting'] = False
        if not link['awaiting']:
            for key in ('wait_ticks', 'idle_ticks', 'reviewer_before', 'reviewer_seen_working', 'retries'):
                link.pop(key, None)
        return link
    if state == 'Working':
        link['seen_working'] = True
        link.pop('retries', None)
        link.pop('wait_ticks', None)
    elif link['seen_working'] and state == 'Idle':
        reviewer_state = status(reviewer)['state']
        if reviewer_state != 'Idle':
            link['wait_ticks'] = link.get('wait_ticks', 0) + 1
            if link['wait_ticks'] >= REVIEW_WAIT:
                link['seen_working'] = False
                link.pop('wait_ticks', None)
                link.pop('retries', None)
            return link
        link.pop('wait_ticks', None)
        delivered = False
        if reviewer.get('writable'):
            agent = 'Claude' if link['agent'] == 'claude' else 'Codex'
            text = (f"[Tether review] The {agent} tab “{link['name']}” ({link['worker']}) finished a turn. "
                    'Review its work: check the changes and the claims, then list what is wrong or missing. '
                    'Start your reply with APPROVED if nothing is. Tether sends your reply to that tab. '
                    f"Its last reply:\n\n{last_reply(worker)[:1500]}\n\nTranscript: {worker.get('transcript', '')}")
            if link.get('rounds', 0) == MAX_ROUNDS:
                text += ' This is the last round: your reply stays here.'
            before = _digest(last_reply(reviewer))
            try:
                send(reviewer, text, reviewer_state)
                delivered = True
            except (DraftError, RuntimeError):
                pass
        if delivered:
            link['seen_working'] = False
            link['awaiting'] = True
            link['reviewer_seen_working'] = False
            link['reviewer_before'] = before
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
