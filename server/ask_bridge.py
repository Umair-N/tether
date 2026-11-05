"""Opt-in AskUserQuestion bridge. Payloads live only in this server's memory.

The command hook prints nothing unless a person supplies a complete answer.
Exit 0 with empty stdout leaves Claude's native permission flow unchanged.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler

import auth
import paths

HOOK_PATH = '/mobile/api/ask-hook'
ANSWER_PATH = '/mobile/api/ask-answer'
WAIT_SECONDS = 30
MAX_WAIT_SECONDS = 120
WATCH_WINDOW = 60
WATCH_GRACE = 10
LEASE_SECONDS = 5
MAX_PENDING = 32
MAX_PAYLOAD = 32768
MAX_ANSWER = 16384


def install_settings():
    target = Path(os.environ.get('TETHER_INSTALL_STATE') or paths.HOME / '.config/tether/install.json')
    try:
        saved = json.loads(target.read_text())
        options = saved.get('options') if isinstance(saved, dict) else None
        return options if isinstance(options, dict) else {}
    except (OSError, ValueError):
        return {}


def enabled():
    return install_settings().get('ask_bridge') is True


def wait_seconds():
    value = install_settings().get('ask_bridge_wait', WAIT_SECONDS)
    return max(5, min(MAX_WAIT_SECONDS, value)) if type(value) is int else WAIT_SECONDS


def identity(entry):
    return tuple(entry.get(key) for key in ('id', 'thread', 'owner_pid', 'process_start'))


def process_alive(pid, started):
    from agents import process_start
    try:
        return bool(started and process_start(pid) == started)
    except (OSError, ValueError, IndexError):
        return False


def hook_process(pid, owner):
    """Only a live descendant of the selected Claude process can register."""
    from agents import process_start
    if type(pid) is not int or pid <= 1 or pid == owner:
        raise ValueError('Invalid hook process.')
    started = process_start(pid)
    ancestor = pid
    for _ in range(64):
        if ancestor == owner:
            if not process_alive(pid, started):
                break
            return started
        if ancestor <= 1:
            break
        ancestor = int((Path('/proc') / str(ancestor) / 'stat').read_text().rsplit(')', 1)[1].split()[1])
    raise ValueError('The hook does not belong to this agent.')


def verify_entry(entry):
    """Verify ownership without requiring keyboard focus in the terminal.

    A hook may be the foreground command. Copy mode and terminal drafts do not
    affect a structured reply, which never writes to the tmux input buffer.
    """
    from agents import process_start
    pid = entry['owner_pid']
    process = Path('/proc') / str(pid)
    if (process_start(pid) != entry['process_start'] or process.joinpath('comm').read_text().strip() != 'claude'
            or entry.get('pending_conversation')):
        raise Conflict()
    registry = json.loads(Path(entry['registry']).read_text())
    if registry.get('pid') != pid or registry.get('sessionId') != entry['thread']:
        raise Conflict()
    result = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', entry['id'],
                             '#{pane_pid}\t#{socket_path}'], check=True, capture_output=True, text=True, timeout=2)
    root, socket = result.stdout.strip().split('\t')
    ancestor = pid
    for _ in range(64):
        if ancestor == int(root):
            return socket
        if ancestor <= 1:
            break
        ancestor = int((Path('/proc') / str(ancestor) / 'stat').read_text().rsplit(')', 1)[1].split()[1])
    raise Conflict()


def questions_input(tool):
    # Pilot only the ordinary choice schema; extended/gated forms use the terminal.
    if not isinstance(tool, dict) or len(json.dumps(tool).encode()) > MAX_PAYLOAD:
        raise ValueError('Invalid question payload.')
    questions = tool.get('questions')
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
        raise ValueError('Invalid questions.')
    texts = set()
    for question in questions:
        if not isinstance(question, dict) or question.get('kind', 'choice') != 'choice':
            raise ValueError('Unsupported question kind.')
        text = question.get('question')
        options = question.get('options')
        if (not isinstance(text, str) or not text.strip() or text in texts
                or not isinstance(question.get('header'), str)
                or type(question.get('multiSelect', False)) is not bool
                or not isinstance(options, list) or not 2 <= len(options) <= 4):
            raise ValueError('Invalid question.')
        texts.add(text)
        labels = []
        for option in options:
            if (not isinstance(option, dict) or not isinstance(option.get('label'), str)
                    or not option['label'].strip()
                    or not isinstance(option.get('description', ''), str)
                    or not isinstance(option.get('preview', ''), str)):
                raise ValueError('Invalid option.')
            labels.append(option['label'])
        if len(set(labels)) != len(labels):
            raise ValueError('Duplicate option labels.')
    return questions


def answer_input(questions, answers):
    """The pinned CLI accepts question text -> string, with comma-joined multi labels.

    Arrays are accepted at our boundary only for multi-select option labels.
    A string can be a single label, a comma-joined multi answer, or free text.
    Require every question; an empty/partial map must never approve the tool.
    """
    if (not isinstance(answers, dict) or set(answers) != {q['question'] for q in questions}
            or len(json.dumps(answers).encode()) > MAX_ANSWER):
        raise ValueError('Answer every question.')
    result = {}
    for question in questions:
        value = answers[question['question']]
        if isinstance(value, list):
            labels = [option['label'] for option in question['options']]
            if (not question.get('multiSelect') or not value
                    or any(not isinstance(item, str) or item not in labels for item in value)
                    or len(set(value)) != len(value)):
                raise ValueError('Invalid multi-select answer.')
            value = ', '.join(value)
        if not isinstance(value, str) or not value.strip() or value == '(notes only)':
            raise ValueError('Empty answer.')
        result[question['question']] = value
    return result


class Conflict(Exception):
    """A stale, consumed, disconnected, or expired invocation."""


class Pending:
    def __init__(self, clock=time.time, monotonic=time.monotonic, last_watched=lambda pane: float('-inf')):
        self.clock = clock
        self.monotonic = monotonic
        self.last_watched = last_watched
        self.lock = threading.Lock()
        self.records = {}

    def prune(self):
        now = self.clock()
        for key, item in list(self.records.items()):
            if item['expires'] <= now or (item['state'] in ('pending', 'answered') and
                    (item['lease'] <= now or not process_alive(item['pid'], item['started']) or
                     self.monotonic() - self.last_watched(item['identity'][0]) >= WATCH_GRACE)):
                self.records.pop(key)

    def register(self, entry, body):
        nonce, tool_id = body.get('nonce'), body.get('tool_use_id')
        if (not isinstance(nonce, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{20,100}', nonce)
                or not isinstance(tool_id, str) or not 1 <= len(tool_id) <= 200
                or body.get('session_id') != entry.get('thread')):
            raise ValueError('Invalid invocation.')
        questions = questions_input(body.get('tool_input'))
        with self.lock:
            self.prune()
            if self.monotonic() - self.last_watched(entry['id']) >= WATCH_WINDOW:
                return {'wait': 0}
            started = hook_process(body.get('pid'), entry['owner_pid'])
            if (len(self.records) >= MAX_PENDING or any(item['nonce'] == nonce or
                    item['identity'][0] == entry['id'] and item['state'] == 'pending'
                    for item in self.records.values())):
                raise Conflict()
            identifier = secrets.token_urlsafe(18)
            now = self.clock()
            seconds = wait_seconds()
            self.records[identifier] = dict(identity=identity(entry), nonce=nonce,
                tool_use_id=tool_id, questions=json.loads(json.dumps(questions)),
                pid=body['pid'], started=started, expires=now + seconds,
                lease=now + LEASE_SECONDS, state='pending')
            return dict(id=identifier, nonce=nonce, expires=now + seconds, wait=seconds)

    def lookup(self, entry, body):
        if not isinstance(body.get('id'), str) or not isinstance(body.get('nonce'), str):
            raise ValueError('Invalid invocation identity.')
        self.prune()
        item = self.records.get(body.get('id'))
        if not item or item['identity'] != identity(entry) or item['nonce'] != body.get('nonce'):
            raise Conflict()
        return item

    def poll(self, entry, body):
        with self.lock:
            item = self.lookup(entry, body)
            if item['state'] == 'pending':
                item['lease'] = self.clock() + LEASE_SECONDS
                return {'state': 'pending', 'expires': item['expires']}
            if item['state'] == 'answered' and process_alive(item['pid'], item['started']):
                answers = item.pop('answers')
                item['state'] = 'delivered'
                item.pop('questions', None)
                return {'state': 'answered', 'answers': answers}
            raise Conflict()

    def retire(self, entry, body):
        with self.lock:
            item = self.lookup(entry, body)
            item.update(state='retired', expires=min(item['expires'], self.clock() + 60))
            item.pop('questions', None)
            item.pop('answers', None)
            return {'state': 'retired'}

    def answer(self, entry, body):
        request_id = body.get('requestId')
        if 'answers' not in body or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError('A requestId is required.')
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with self.lock:
            item = self.lookup(entry, body)
            if item.get('requestId') == request_id:
                if item['digest'] != digest:
                    raise Conflict()
                return item['result']
            if item['state'] != 'pending':
                raise Conflict()
            answers = body.get('answers')
            result = {'status': 'submitted' if answers is not None else 'handoff'}
            if answers is not None:
                item['answers'] = answer_input(item['questions'], answers)
            item.update(state='answered' if answers is not None else 'retired',
                        requestId=request_id, digest=digest, result=result)
            if answers is None:
                item.pop('questions', None)
            return result

    def attention(self, entry):
        with self.lock:
            self.prune()
            for item_id, item in self.records.items():
                if item['identity'] == identity(entry) and item['state'] == 'pending':
                    return dict(id=item_id, nonce=item['nonce'],
                                questions=json.loads(json.dumps(item['questions'])), expires=item['expires'])
        return None


pending = Pending()


def overlay(entry, status):
    if not enabled():
        with pending.lock:
            pending.records.clear()
        return status
    ask = pending.attention(entry)
    if not ask:
        return status
    return dict(status, state='Input needed', approval=None, attention={
        'id': ask['id'], 'reason': 'prompt', 'text': 'Claude has questions for you.', 'askBridge': ask})


def handles(path):
    return path in (HOOK_PATH, ANSWER_PATH)


def serve(handler, body, choose):
    """Called after the normal cookie, Origin, and write-header checks."""
    if not enabled():
        handler.respond(409, {'error': 'Ask bridge is off.'})
        return
    try:
        if handler.path == HOOK_PATH:
            # Proxy auth alone cannot authorize local hook calls. Use the same
            # owner-only token and derived session credential as token login.
            if (not auth.is_loopback(handler.client_address[0]) or not handler.headers.get('X-Tether-Hook') or not hmac.compare_digest(
                    handler.headers.get('X-Tether-Hook', ''), auth.session_value())):
                handler.respond(403, {'error': 'Local hook authentication required.'})
                return
        entry = choose(body.get('session')) if isinstance(body.get('session'), str) else None
        if not entry or entry.get('agent') != 'claude' or not entry.get('writable') or entry.get('ambiguous'):
            raise Conflict()
        socket = verify_entry(entry)
        if handler.path == ANSWER_PATH:
            result = pending.answer(entry, body)
        else:
            action = body.get('action')
            operation = {'register': pending.register, 'poll': pending.poll, 'retire': pending.retire}.get(action)
            if not operation:
                raise ValueError('Invalid hook operation.')
            if action == 'register' and body.get('socket') != socket:
                raise Conflict()
            result = operation(entry, body)
        handler.respond(200, result)
    except ValueError:
        handler.respond(400, {'error': 'Invalid ask request.'})
    except (Conflict, RuntimeError, OSError, KeyError, subprocess.SubprocessError):
        handler.respond(409, {'error': 'The ask is no longer available.'})


def hook(data, request=None, wait=time.sleep, clock=time.monotonic):
    """Return a decision only after an answer; all other paths return None."""
    if (not isinstance(data, dict) or data.get('hook_event_name') != 'PreToolUse'
            or data.get('tool_name') != 'AskUserQuestion' or data.get('agent_id')):
        return None
    try:
        questions = questions_input(data.get('tool_input'))
        if request is None:
            settings = install_settings()
            if settings.get('ask_bridge') is not True:
                return None
            port = int(os.environ.get('MOBILE_TMUX_PORT') or settings['port'])
            if not 1 <= port <= 65535:
                return None
            url = f'http://127.0.0.1:{port}'
            # Read only. A hook must never create or replace an auth token.
            secret = auth.TOKEN_FILE.read_text().strip()
            if not secret:
                return None
            credential = auth.session_value(secret)
            opener = build_opener(ProxyHandler({}))

            def request(body):
                req = Request(url + HOOK_PATH, json.dumps(body).encode(), headers={
                    'Content-Type': 'application/json', 'Origin': url,
                    'X-Codex-Mobile': '1', 'X-Tether-Hook': credential,
                    'Cookie': auth.COOKIE + '=' + credential})
                with opener.open(req, timeout=1) as response:
                    return json.loads(response.read(65536))
        body = dict(session=os.environ.get('TMUX_PANE'), session_id=data.get('session_id'),
                    socket=os.environ.get('TMUX', '').split(',')[0],
                    nonce=secrets.token_urlsafe(24), tool_use_id=data.get('tool_use_id'),
                    pid=os.getpid(), tool_input=data['tool_input'])
        started = clock()
        registered = request(dict(body, action='register'))
        seconds = registered.get('wait')
        if type(seconds) is not int or seconds <= 0:
            return None
        deadline = started + min(seconds, MAX_WAIT_SECONDS)
        body = {key: body[key] for key in ('session', 'nonce')}
        body['id'] = registered['id']
        try:
            while clock() < deadline:
                result = request(dict(body, action='poll'))
                if result.get('state') == 'answered':
                    answers = answer_input(questions, result['answers'])
                    if clock() >= deadline:
                        return None
                    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                            'permissionDecision': 'allow', 'updatedInput': dict(data['tool_input'], answers=answers)}}
                if result.get('state') != 'pending':
                    return None
                wait(.5)
        finally:
            try:
                request(dict(body, action='retire'))
            except Exception:
                pass
    except (Exception, KeyboardInterrupt):
        pass  # No question bodies, answers, credentials, or errors in logs.
    return None


def main():
    try:
        data = json.loads(sys.stdin.read(MAX_PAYLOAD + 1))
        output = hook(data)
        if output is not None:
            print(json.dumps(output))
    except (Exception, KeyboardInterrupt):
        pass


if __name__ == '__main__':
    main()
