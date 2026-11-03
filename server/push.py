"""Web Push notifications for agent state changes (finished, needs approval, needs input)."""
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit
import paths

# A private copy of pywebpush lives here; system packages keep priority.
sys.path.append(str(paths.HOME / '.local/share/mobile-tmux/pylib'))
try:
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid02
    from pywebpush import WebPushException, webpush
except ImportError:  # Notifications are optional; the rest of the app must keep working without them.
    webpush = None

KEYS = paths.private_file('push_keys.json')
STORE = paths.CACHE / 'push_subscriptions.json'
MUTED = paths.CACHE / 'push_muted.json'
PANE_ID = re.compile(r'^%\d{1,6}$')
SUBJECT = os.environ.get('MOBILE_TMUX_PUSH_SUBJECT') or 'mailto:mobile-tmux@localhost'
PUSH_SERVICES = ('googleapis.com', 'push.services.mozilla.com', 'push.apple.com', 'notify.windows.com')
LOCK = threading.Lock()


def notification_title(entry):
    """Session name plus the pane's own name, not the "session:0.1" label."""
    # tmux_session is the session id ("$0"); the name is the label without its ":W.P" pane position.
    session = re.sub(r':\d+\.\d+$', '', entry.get('label') or '')
    pane = entry.get('pane_name') or entry.get('agent_name') or ''
    if session and pane and pane != session:
        return f'{session} · {pane}'
    return pane or session or entry.get('label') or 'Agent'


def available():
    return webpush is not None


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def vapid_keys(path=KEYS):
    """The server's VAPID identity, created on first use and kept private to the owner."""
    with LOCK:
        try:
            stored = json.loads(path.read_text())
            return stored['private_pem'], stored['public']
        except (OSError, ValueError, KeyError):
            pass
        vapid = Vapid02()
        vapid.generate_keys()
        pem = vapid.private_pem().decode()
        public = b64url(vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'private_pem': pem, 'public': public}))
        temporary.chmod(0o600)
        temporary.replace(path)
        return pem, public


def valid_subscription(value):
    if not isinstance(value, dict) or not isinstance(value.get('endpoint'), str) or len(value['endpoint']) > 1000:
        return False
    keys = value.get('keys')
    if not isinstance(keys, dict) or not all(isinstance(keys.get(name), str) and 0 < len(keys[name]) < 200 for name in ('p256dh', 'auth')):
        return False
    parts = urlsplit(value['endpoint'])
    host = parts.hostname or ''
    # The server will POST to this address, so only the browsers' own push services are acceptable.
    return parts.scheme == 'https' and any(host == service or host.endswith('.' + service) for service in PUSH_SERVICES)


def load(store=STORE):
    try:
        data = json.loads(store.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data, store=STORE):
    store.parent.mkdir(parents=True, exist_ok=True)
    temporary = store.with_suffix('.tmp')
    temporary.write_text(json.dumps(data))
    temporary.chmod(0o600)
    temporary.replace(store)


def subscribe(subscription, store=STORE):
    if not valid_subscription(subscription):
        raise ValueError('Invalid push subscription.')
    with LOCK:
        data = load(store)
        data[subscription['endpoint']] = {'subscription': {'endpoint': subscription['endpoint'], 'keys': subscription['keys']},
                                          'created': int(time.time())}
        save(data, store)


def unsubscribe(endpoint, store=STORE):
    with LOCK:
        data = load(store)
        if data.pop(endpoint, None) is not None:
            save(data, store)


def tmux_run(clock=time.monotonic, _cache={}):
    """Which tmux server run the pane ids belong to. tmux numbers panes again from %0 after a restart."""
    if clock() - _cache.get('at', float('-inf')) > 30:
        try:
            result = subprocess.run([paths.TMUX, 'display-message', '-p', '#{pid}:#{start_time}'], capture_output=True, text=True, timeout=3)
            _cache['run'] = result.stdout.strip() if result.returncode == 0 else ''
        except (OSError, subprocess.TimeoutExpired):
            _cache['run'] = ''
        _cache['at'] = clock()
    return _cache['run']


def _mutes(store):
    try:
        data = json.loads(store.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {pane: run for pane, run in data.items() if isinstance(pane, str) and PANE_ID.match(pane) and isinstance(run, str)}


def muted(store=MUTED, run=None):
    """Pane ids whose notifications are off. A mute from an earlier tmux run does not count."""
    run = tmux_run() if run is None else run
    return sorted(pane for pane, owner in _mutes(store).items() if run and owner == run)


def set_muted(session, value, store=MUTED, run=None):
    if not isinstance(session, str) or not PANE_ID.match(session) or not isinstance(value, bool):
        raise ValueError('Invalid pane.')
    run = tmux_run() if run is None else run
    if not run:
        raise ValueError('tmux is not running.')
    with LOCK:
        current = {pane: owner for pane, owner in _mutes(store).items() if owner == run}  # drop mutes from old runs
        if value:
            current[session] = run
        else:
            current.pop(session, None)
        save(current, store)
        return sorted(current)


def send_all(payload, urgent=False, store=STORE, sender=None):
    """Deliver one notification to every subscribed browser; forget subscriptions the push service has dropped."""
    sender = sender or webpush
    if sender is None:
        return 0
    pem, _ = vapid_keys()
    delivered = 0
    for endpoint, record in list(load(store).items()):
        try:
            sender(subscription_info=record['subscription'], data=json.dumps(payload), vapid_private_key=Vapid02.from_pem(pem.encode()),
                   vapid_claims={'sub': SUBJECT}, ttl=3600, headers={'Urgency': 'high' if urgent else 'normal'}, timeout=10)
            delivered += 1
        except WebPushException as exc:
            if getattr(exc.response, 'status_code', None) in (404, 410):
                unsubscribe(endpoint, store)
        except (OSError, ValueError):
            continue
    return delivered


SUMMARY_LIMIT = 180
RISKY = re.compile(r'\brm\b|\brmdir\b|\bdelete\b|\bdrop\b|\btruncate\b|--force\b|\bpush\b[^|;&]*\s-\w*f\b|reset\s+--hard|\bclean\s+-\w*f|'
                   r'\bdeploy|\bkill\b|\bsudo\b|\bdd\b|\bmkfs|\bchmod\b|\bchown\b|terraform\s+(?:apply|destroy)|kubectl\s+(?:delete|apply)|gcloud\b.*\b(?:delete|deploy)', re.I)


def quick_approval(approval):
    """The approval id when a notification may offer Approve and Deny: a live plain prompt with a one-time Yes and a No.
    Plan menus and folder-trust prompts need the full view, so they get no buttons."""
    if not isinstance(approval, dict) or not approval.get('actionable') or not approval.get('id'):
        return None
    texts = [str(option.get('text', '')) for option in approval.get('options') or [] if isinstance(option, dict)]
    once = any(re.match(r'Yes\b', text, re.I) and not re.search(r'always|don.t ask|remember|auto|manually|trust', text, re.I) for text in texts)
    deny = any(re.match(r'No\b', text, re.I) for text in texts)
    special = any(re.search(r'keep planning|trust|auto mode|manually approve', text, re.I) for text in texts)
    summary = ' '.join(re.sub(r'[\u2500-\u257f]', ' ', str(approval.get('text') or '')).split())
    # Risky or long requests are approved in the app, where the whole request is on screen.
    risky = len(summary) > SUMMARY_LIMIT or RISKY.search(summary)
    return approval['id'] if once and deny and not special and not risky else None


def approval_summary(text, limit=SUMMARY_LIMIT):
    """The request in one line, without the box drawing around it."""
    line = ' '.join(re.sub(r'[\u2500-\u257f]', ' ', str(text or '')).split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + '…'


def reply_summary(text, limit=160):
    """Plain text at the start of a reply, suitable for a notification."""
    text = str(text or '')
    text = re.sub(r'(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?(?:^\s*\1\s*$|\Z)', ' [code] ', text)
    text = re.sub(r'!?\[([^\]]*)\]\([^\n]*?\)', r'\1', text)
    text = re.sub(r'(?m)^\s*\[[^\]]+\]:\s+\S+.*$', '', text)
    text = re.sub(r'\[([^\]]+)\]\[[^\]]*\]', r'\1', text)
    text = re.sub(r'(?m)^\s*(?:#{1,6}\s+|[-*+•]\s+|\d+[.)]\s+|>\s*)', '', text)
    text = re.sub(r'(?m)^\s*(?:[-=*_]\s*){3,}$', '', text)
    text = re.sub(r'[*`~]', '', text)
    text = re.sub(r'(?<!\w)_|_(?!\w)', '', text)
    return approval_summary(text, limit)


class Notifier:
    """Turns agent state changes into notifications, once per change and never while the page is being watched."""

    MESSAGES = {'done': 'Finished', 'approval': 'Needs approval', 'input': 'Needs your input'}

    def __init__(self, send, watching=lambda session: False, settle=4, minimum_work=5, clock=time.monotonic, muted=lambda: (), reply=lambda entry: ''):
        self.send, self.watching, self.settle, self.minimum_work, self.clock, self.muted = send, watching, settle, minimum_work, clock, muted
        self.reply = reply
        self.seen = {}

    def observe(self, entry, status):
        if not isinstance(status, dict):
            return
        state, now = status.get('state'), self.clock()
        record = self.seen.setdefault(entry['id'], {'state': state, 'since': now, 'worked': None, 'sent': None})
        if state != record['state']:
            record['state'], record['since'] = state, now
        if state == 'Working':
            record['worked'] = record['worked'] if record['worked'] is not None else now
            record['sent'] = None
            return
        kind, token = None, None
        if state == 'Approval':
            kind, token = 'approval', (status.get('approval') or {}).get('id')
        elif state == 'Input needed':
            kind, token = 'input', (status.get('attention') or {}).get('id')
        elif state == 'Idle' and record['worked'] is not None and record['since'] - record['worked'] >= self.minimum_work:
            kind = 'done'
        if state == 'Idle' and kind is None and record['worked'] is not None and now - record['since'] >= self.settle:
            record['worked'] = None  # a turn too short to be worth a notification
        if kind is None or now - record['since'] < self.settle or record['sent'] == (kind, token):
            return
        record['sent'] = (kind, token)
        if kind == 'done':
            record['worked'] = None
        if self.watching(entry['id']) or entry['id'] in self.muted():
            return
        payload = {'title': notification_title(entry), 'body': self.MESSAGES[kind], 'tag': 'session-' + entry['id'],
                   'session': entry['id'], 'url': '/mobile/#session=' + quote(entry['id'], safe='')}
        pending = status.get('approval') if kind == 'approval' and isinstance(status.get('approval'), dict) else {}
        summary = approval_summary(pending.get('text'))
        if kind in ('done', 'input'):
            attention = status.get('attention') if isinstance(status.get('attention'), dict) else {}
            summary = reply_summary(attention.get('question') or attention.get('text')) if kind == 'input' else ''
            if not summary:
                try:
                    summary = reply_summary(self.reply(entry))
                except Exception:  # Transcript previews must never prevent delivery.
                    summary = ''
        if summary:
            payload['body'] = self.MESSAGES[kind] + ': ' + summary  # say what it wants, not only that it waits
        quick = quick_approval(pending)
        if quick:
            # The service worker answers with this exact id; the server refuses it once the prompt on screen changes.
            payload.update(approval=quick, thread=entry.get('thread'))
        self.send(payload, kind != 'done')
