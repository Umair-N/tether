"""Sidebar and tab choices (pins, order, pane-tab order, seen marks, ...) kept on the server so every device shares them.

Each save sends only the keys that changed; the newest value of a key wins. 'seen' merges per pane and keeps the latest time,
so two devices never undo each other's Done marks. 'drafts' ({pane: {text, at}}) keeps the newest edit per pane; an empty text is a
cleared draft, kept for two days so an old copy on another device cannot bring it back.
'repos' ({folder: [repo, ...]}) is the Changes panel's repository choice per parent folder; it merges per folder.
'files.pinned' ([~/folder, ...]) is the Files explorer's pinned places; the newest list wins. Device-only choices (open pane, text size) never come here.
"""
import json
import os
import threading
import time
import paths

FILE = paths.CACHE / 'prefs.json'
KEYS = {'pinned': list, 'sidePins': list, 'order': list, 'paneOrder': dict, 'sort': str, 'seen': dict, 'collapsed': dict,
        'showPaths': bool, 'tmuxWords': bool, 'liveReplyPreview': bool, 'voiceOff': bool, 'voiceSound': bool, 'theme': str, 'scheme': str, 'drafts': dict,
        'archived': dict, 'layout': dict, 'repos': dict, 'files.pinned': list}
MAX_SEEN = 500
MAX_DRAFTS = 200
MAX_DRAFT_TEXT = 20000
MAX_REPO_FOLDERS = 200
MAX_PINNED_FILES = 200
CLEARED_KEEP = 2 * 86400
_lock = threading.Lock()


def load():
    try:
        data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        data = {}
    prefs = data.get('prefs') if isinstance(data, dict) else None
    return {'prefs': {key: value for key, value in (prefs or {}).items() if isinstance(value, KEYS.get(key, ()))},
            'version': data.get('version', 0) if isinstance(data, dict) and isinstance(data.get('version'), int) else 0}


def save(changes):
    """Merge changed keys and return the stored copy. Raises ValueError for a bad body."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError('Invalid preferences.')
    for key, value in changes.items():
        if key not in KEYS or not isinstance(value, KEYS[key]) or (KEYS[key] is int and isinstance(value, bool)):
            raise ValueError('Invalid preference: %s.' % key)
        if key == 'files.pinned' and not (len(value) <= MAX_PINNED_FILES and all(isinstance(item, str) and 0 < len(item) <= 1024 for item in value)):
            raise ValueError('Invalid preference: %s.' % key)
        if key == 'sidePins':
            if not (len(value) <= 40 and all(isinstance(item, dict) and set(item) <= {'pane', 'note'}
                    and isinstance(item.get('pane'), str) and 0 < len(item['pane']) <= 64
                    and ('note' not in item or isinstance(item['note'], str) and len(item['note']) <= 60)
                    for item in value) and len({item['pane'] for item in value}) == len(value)):
                raise ValueError('Invalid preference: sidePins.')
    with _lock:
        stored = load()
        prefs = stored['prefs']
        for key, value in changes.items():
            if key == 'seen':
                merged = dict(prefs.get('seen') or {})
                for pane, at in value.items():
                    if isinstance(at, (int, float)) and not isinstance(at, bool) and at > merged.get(pane, 0):
                        merged[pane] = at
                # Keep the newest marks so the file stays small.
                value = dict(sorted(merged.items(), key=lambda item: item[1])[-MAX_SEEN:])
            if key == 'drafts':
                value = merge_drafts(prefs.get('drafts') or {}, value)
            if key == 'repos':
                value = merge_repos(prefs.get('repos') or {}, value)
            prefs[key] = value
        stored['version'] += 1
        FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = FILE.with_suffix('.tmp')
        temporary.write_text(json.dumps(stored))
        os.replace(temporary, FILE)
        return stored


def merge_repos(stored, incoming):
    """The Changes panel's repository choice per parent folder: {folder: [repo, ...]}. A folder sent with [] goes back to All repos.
    Only the folders sent change, so two devices that pick repos in different folders keep both. Raises ValueError for a bad entry."""
    merged = dict(stored)
    for folder, chosen in incoming.items():
        if not (isinstance(chosen, list) and len(folder) <= 1024 and len(chosen) <= 100
                and all(isinstance(item, str) and 0 < len(item) <= 1024 for item in chosen)):
            raise ValueError('Invalid repository choice.')
        merged.pop(folder, None)  # re-insert so the newest choice is kept when the list is trimmed
        if chosen:
            merged[folder] = chosen
    return dict(list(merged.items())[-MAX_REPO_FOLDERS:])


def merge_drafts(stored, incoming, now=None):
    """Newest edit per pane wins. Raises ValueError for a bad entry."""
    now = now or time.time()
    merged = dict(stored)
    for pane, entry in incoming.items():
        if not (isinstance(entry, dict) and isinstance(entry.get('text'), str) and len(entry['text']) <= MAX_DRAFT_TEXT
                and isinstance(entry.get('at'), (int, float)) and not isinstance(entry.get('at'), bool)):
            raise ValueError('Invalid draft.')
        if entry['at'] > (merged.get(pane) or {}).get('at', 0):
            merged[pane] = {'text': entry['text'], 'at': entry['at']}
    kept = {pane: entry for pane, entry in merged.items() if entry['text'] or now - entry['at'] < CLEARED_KEEP}
    return dict(sorted(kept.items(), key=lambda item: item[1]['at'])[-MAX_DRAFTS:])
