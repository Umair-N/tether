"""Live Codex plan limits, read through Codex's own app-server (the protocol its IDE extensions use).

Tether reads no login token: Codex loads and renews its own sign-in. There is no live Claude check,
because Anthropic's terms allow Claude sign-in tokens only in Claude Code and native Anthropic apps.
Claude's numbers come from the status-line report that Claude Code itself writes after each reply."""
import json
import os
import select
import subprocess
import threading
import time

TIMEOUT = 15  # seconds for the whole app-server exchange
_cache = {}  # {'at', 'report', 'error'}
_lock = threading.Lock()


def codex_report(result):
    limits = result.get('rateLimits') or {}
    windows = []
    for key in ('primary', 'secondary'):
        value = limits.get(key)
        if isinstance(value, dict) and isinstance(value.get('usedPercent'), (int, float)):
            minutes = value.get('windowDurationMins') or 0
            label = '7d' if minutes == 10080 else f'{minutes / 60:g}h' if minutes else key
            windows.append({'label': label, 'percent': value['usedPercent'], 'resets_at': value.get('resetsAt')})
    return {'windows': windows}


def ask_app_server(command=('codex', '-s', 'read-only', '-a', 'never', 'app-server'), timeout=TIMEOUT):
    """One JSON-RPC exchange: initialize, then account/rateLimits/read. Returns the result object."""
    process = subprocess.Popen(list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, cwd=os.path.expanduser('~'))
    deadline = time.monotonic() + timeout
    try:
        def send(message):
            process.stdin.write(json.dumps(message) + '\n')
            process.stdin.flush()

        def reply(wanted):
            while time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
                if not ready:
                    break
                line = process.stdout.readline()
                if not line:
                    raise OSError('Codex app-server stopped.')
                message = json.loads(line)
                if message.get('id') == wanted:
                    if 'error' in message:
                        raise ValueError((message['error'] or {}).get('message') or 'Codex returned an error.')
                    return message.get('result') or {}
            raise TimeoutError('Codex did not answer in time.')

        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'tether', 'version': '1'}}})
        reply(1)
        send({'jsonrpc': '2.0', 'method': 'initialized'})
        send({'jsonrpc': '2.0', 'id': 2, 'method': 'account/rateLimits/read'})
        return reply(2)
    finally:
        process.kill()
        process.wait(timeout=5)


def check(max_age, now=None, ask=ask_app_server):
    """The live Codex report, at most max_age seconds old: {'report': {...}, 'error': str|None}."""
    now = now or time.time()
    with _lock:
        if now - _cache.get('at', 0) < max_age:
            return _cache
        entry = {'at': now, 'report': _cache.get('report'), 'error': None}
        try:
            entry['report'] = dict(codex_report(ask()), reported_at=now)
        except FileNotFoundError:
            entry['error'] = 'Codex is not installed on this computer.'
        except TimeoutError as exc:
            entry['error'] = str(exc)
        except (OSError, ValueError, TypeError) as exc:
            entry['error'] = 'The live Codex check failed: ' + (str(exc) or exc.__class__.__name__)[:200]
        _cache.clear()
        _cache.update(entry)
        return entry
