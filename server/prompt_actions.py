"""Short-lived, single-use navigation handles for proven terminal report surfaces.

The browser supplies an opaque action ID, never a tmux key. Screen identity is an
observation, not a protocol ID: an identical close/reopen between polls cannot be
detected. Only bounded, read-only navigation is allowed here.
"""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import re
import subprocess
import threading
import time
import uuid

import paths

SGR = re.compile(r'\x1b\[[0-9;:]*m')
OSC = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
RULE = re.compile(r'^\s*[─━═-]{3,}\s*$')
# No keys are obtained from tmux's permissive string-to-key conversion.
KEYS = {'pgup': ('PPage', 'PgUp'), 'pageup': ('PPage', 'PgUp'),
        'pgdn': ('NPage', 'PgDn'), 'pagedown': ('NPage', 'PgDn'),
        'tab': ('Tab', 'Tab'), 'esc': ('Escape', 'Esc'), 'escape': ('Escape', 'Esc'),
        'z': ('z', 'z'), '↑': ('Up', '↑'), 'up': ('Up', '↑'),
        '↓': ('Down', '↓'), 'down': ('Down', '↓'),
        'home': ('Home', 'Home'), 'end': ('End', 'End')}
META = '#{pane_id}\t#{pane_width}\t#{pane_height}\t#{alternate_on}\t#{cursor_flag}\t#{pane_in_mode}\t#{cursor_x}\t#{cursor_y}'
EXPIRY = 30
LIMIT = 256


def plain(frame):
    """Remove SGR and terminal links only. Keep all physical rows and spaces."""
    return SGR.sub('', OSC.sub('', frame))


def metadata(entry, run=subprocess.run):
    fields = run([paths.TMUX, 'display-message', '-p', '-t', entry['id'], META],
                 check=True, capture_output=True, text=True, timeout=5).stdout.strip().split('\t')
    if len(fields) != 8 or fields[0] != entry['id']:
        raise RuntimeError('The terminal changed. Reload its tab.')
    return tuple(fields)


def _styles(rows):
    """SGR state crosses rows. RGB components never count as attributes."""
    bold = dim = underline = background = False
    result = []
    for row in rows:
        spans = []
        for part in re.split(r'(\x1b\[[0-9;:]*m)', OSC.sub('', row)):
            if not SGR.fullmatch(part):
                if part.strip():
                    spans.append((bold, dim, underline, background))
                continue
            codes = [int(code or 0) for code in part[2:-1].replace(':', ';').split(';')]
            index = 0
            while index < len(codes):
                code = codes[index]
                if code in (38, 48, 58) and index + 1 < len(codes):
                    count = 2 if codes[index + 1] == 5 else 4 if codes[index + 1] == 2 else len(codes)
                    if code == 48:
                        background = True
                    index += count + 1
                    continue
                if code == 0:
                    bold = dim = underline = background = False
                elif code == 1: bold = True
                elif code == 2: dim = True
                elif code == 4: underline = True
                elif code == 22: bold = dim = False
                elif code == 24: underline = False
                elif code == 49: background = False
                elif 40 <= code <= 47 or 100 <= code <= 107: background = True
                index += 1
        result.append(spans)
    return result


def _footer(rows, styles):
    """Only the terminal's bottom footer band. Never search the report body.

    Native analytics has two fixed rows. Extend only for a split key/label
    pair. A dim report row above the footer must stay in the report body.
    """
    end = len(rows)
    while end and not plain(rows[end - 1]).strip():
        end -= 1
    start = max(0, end - 2)
    def dim(index):
        return bool(styles[index] and any(span[1] for span in styles[index]))
    # A single full hint row can follow a blank first native footer row.
    if not plain(rows[start]).strip():
        start += 1
    elif not dim(start):
        return [], start
    while start >= 2 and end - start < 6:
        previous = plain(rows[start - 1]).strip()
        before = plain(rows[start - 2]).strip()
        if (' ' not in previous and previous and dim(start - 1) and dim(start - 2)
                and all(token.lower() in KEYS for token in before.split('/'))):
            start -= 2
        elif (' ' not in plain(rows[start]).strip() and dim(start - 1)
              and all(token.lower() in KEYS for token in previous.split('/'))):
            start -= 1
        else:
            break
    if start == end or not all(dim(index) for index in range(start, end)):
        return [], start
    # Preserve raw segments; join a wrap with a space, not arbitrary body rows.
    segments, pending = [], ''
    for row in rows[start:end]:
        text = plain(row).strip()
        if pending:
            text, pending = pending + ' ' + text, ''
        if re.match(r'^\S+(?:/\S+)*\s+\S', text):
            segments.extend(part.strip() for part in text.split('·') if part.strip())
        elif segments:
            segments[-1] += ' ' + text
        else:
            pending = text
    if pending:
        segments.append(pending)
    return segments, start


def panel(entry, frame, meta):
    """Recognize the Codex report chrome, independently of report titles.

    The adapter matches render.rs's title/tab/rule and bottom dim hint band.
    Editable/modal evidence disables every action. Unknown effects stay visible.
    """
    if entry.get('agent') != 'codex':
        return None
    rows = frame.splitlines()
    styles = _styles(rows)
    if (len(rows) < 7 or not plain(rows[0]).strip() or not any(span[0] for span in styles[0])
            or not any(bold and (underline or background) for bold, _, underline, background in styles[1])
            or not RULE.fullmatch(plain(rows[2]))):
        return None
    hints, start = _footer(rows, styles)
    if not hints or start < 5:
        return None
    # A normal composer/history frame must never become a report surface.
    if meta[3] != '1':
        return None
    import terminal_input as terminal
    body = '\n'.join(plain(frame).splitlines()[:start])
    unsafe = (meta[4] != '0' or meta[5] != '0' or terminal.approval(plain(frame))
              or re.search(r'(?im)^\s*(?:[›❯>]\s|```|filter\s*[:>]|search\s*[:>]|.*\b(?:input|filter|search)\s+(?:focused|active)\b)', body)
              or any(re.search(r'[╭┌].*[╮┐]', plain(row)) for row in rows[3:start]))
    actions = []
    for hint in hints:
        match = re.fullmatch(r'(\S+)\s+(.+)', hint)
        if not match:
            continue
        binding, effect = match.groups()
        effect = effect.lower()
        tokens = binding.split('/')
        for index, token in enumerate(tokens):
            if token.lower() not in KEYS or len(token) == 1 and token != token.lower():
                continue
            key, display = KEYS[token.lower()]
            label = None
            if effect in ('scroll', 'scroll report'):
                # hints.rs emits configured PageUp/PageDown in that order.
                # A remap can reverse arrows; the glyph does not prove effect.
                if len(tokens) == 2:
                    label = ('Scroll up', 'Scroll down')[index]
                else:
                    label = {'PPage': 'Scroll up', 'NPage': 'Scroll down'}.get(key)
            elif effect in ('report', 'next report') and key == 'Tab':
                label = 'Next report'
            elif effect in ('all', 'dashboard') and key == 'z':
                label = 'All reports'
            elif effect == 'back' and key == 'Escape':
                label = 'Back'
            reason = ('Use the terminal because its input focus is not clear.' if unsafe else
                      'Use the terminal because this action is not supported.' if label is None else None)
            action = {'label': label or match[2], 'key': display, 'enabled': reason is None,
                      '_key': key, '_hint': hint}
            if reason:
                action['disabledReason'] = reason
            actions.append(action)
    return {'body': body, 'hints': hints, 'actions': actions, '_focus': 'unknown' if unsafe else 'report'}


def identity(entry):
    return tuple(entry.get(key) for key in ('id', 'agent', 'owner_pid', 'process_start', 'thread'))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Ledger:
    """Bounded in-memory handles and request results. No raw frames on disk."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.surfaces = OrderedDict()
        self.results = OrderedDict()

    def forget(self, pane):
        with self.lock:
            self.surfaces.pop(pane, None)

    def observe(self, entry, frame, meta):
        found = panel(entry, frame, meta)
        with self.lock:
            now = self.clock()
            for pane, old in list(self.surfaces.items()):
                if now >= old['expires']:
                    self.surfaces.pop(pane)
            if found is None:
                self.forget(entry['id'])
                return None
            revision = digest([identity(entry), frame, meta, found])
            old = self.surfaces.get(entry['id'])
            if not old or old['identity'] != identity(entry) or old['revision'] != revision or old['used']:
                prompt_id = old['promptId'] if old and old['identity'] == identity(entry) and not old['used'] else uuid.uuid4().hex
                old = {'identity': identity(entry), 'revision': revision, 'promptId': prompt_id,
                       'expires': now + EXPIRY, 'used': False, 'meta': meta, 'actions': deepcopy(found['actions'])}
                for action in old['actions']:
                    action['id'] = uuid.uuid4().hex
                self.surfaces[entry['id']] = old
            self.surfaces.move_to_end(entry['id'])
            while len(self.surfaces) > LIMIT:
                self.surfaces.popitem(last=False)
            text = '\n'.join(plain(frame).rstrip().splitlines()[-30:])
            return {'id': digest(text), 'text': text, 'reason': 'info', 'promptId': old['promptId'],
                    'revision': revision, 'requiresResponse': False, 'body': found['body'],
                    'hints': found['hints'],
                    'actions': [{k: v for k, v in action.items() if not k.startswith('_')} for action in old['actions']]}

    def perform(self, body, choose, run=subprocess.run):
        """Caller holds SEND_LOCK from discovery through the final fresh capture."""
        import terminal_input as terminal
        fields = ('session', 'thread', 'promptId', 'expectedRevision', 'actionId', 'requestId')
        if any(not isinstance(body.get(key), str) or not body[key].strip() or len(body[key]) > 256 for key in fields):
            raise ValueError('Reload the panel before choosing an action.')
        with self.lock:
            now = self.clock()
            # Keep deduplication results for five minutes, with a hard bound.
            for key, value in list(self.results.items()):
                if now >= value[0]:
                    self.results.pop(key)
            fingerprint = digest([body[key] for key in fields])
            request = self.results.get(body['requestId'])
            if request:
                if request[1] == fingerprint:
                    return deepcopy(request[2])
                return 409, {'error': 'This request ID was already used. Reload the panel.', 'attention': None}

            def record(status, payload):
                result = (status, payload)
                self.results[body['requestId']] = (now + 300, fingerprint, deepcopy(result))
                while len(self.results) > 1024:
                    self.results.popitem(last=False)
                return result

            current = None
            try:
                try:
                    entry = choose(body['session'])
                except ValueError as exc:
                    raise RuntimeError(str(exc)) from exc
                if not entry.get('writable'):
                    raise RuntimeError('Messaging is unavailable for this session.')
                if body['thread'] != entry.get('thread'):
                    current = read(entry, run=run, ledger=self)
                    return record(409, {'error': 'This session changed conversations. Reload its tab.', 'attention': current})
                terminal.verify(entry, run)
                # Keep the submitted handle before observe can replace it.
                handle = deepcopy(self.surfaces.get(entry['id']))
                current = read(entry, run=run, ledger=self)
                if (not handle or now >= handle['expires'] or handle['used']
                        or handle['identity'] != identity(entry) or not current
                        or body['promptId'] != handle['promptId']
                        or body['promptId'] != current['promptId']
                        or body['expectedRevision'] != current['revision']):
                    return record(409, {'error': 'The panel changed or expired. Review it before trying again.', 'attention': current})
                action = next((a for a in handle['actions'] if a['id'] == body['actionId']), None)
                if not action or not action['enabled']:
                    return record(409, {'error': 'Use the terminal because this action is not available.', 'attention': current})
                # Verify owner/conversation/copy mode once more after capture.
                terminal.verify(entry, run)
                if metadata(entry, run) != handle['meta']:
                    current = read(entry, run=run, ledger=self)
                    return record(409, {'error': 'The terminal focus changed. Review the panel before trying again.', 'attention': current})
                if self.clock() >= handle['expires']:
                    return record(409, {'error': 'The panel expired. Review it before trying again.', 'attention': current})
                self.surfaces[entry['id']]['used'] = True
                # Record uncertainty before delivery. A timeout must never resend.
                record(409, {'error': 'Delivery could not be confirmed. Check the terminal before trying again.', 'attention': None})
                run([paths.TMUX, 'send-keys', '-t', entry['id'], action['_key']],
                    check=True, capture_output=True, timeout=5)
                try:
                    current = read(entry, run=run, ledger=self)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    current = None
                return record(200, {'ok': True, 'attention': current})
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                return record(409, {'error': str(exc) if isinstance(exc, RuntimeError) else
                                   'The terminal is unavailable. Reload its tab.', 'attention': current})


ledger = Ledger()


def read(entry, run=subprocess.run, ledger=ledger):
    """One styled frame, bracketed by consistent geometry and focus snapshots."""
    import terminal_input as terminal
    before = metadata(entry, run)
    frame = terminal.capture(entry, run, styled=True)
    after = metadata(entry, run)
    if before != after:
        ledger.forget(entry['id'])
        return None
    return ledger.observe(entry, frame, after)
