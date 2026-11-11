"""Optional urgent notifications through a configured argv, never a shell.

Configure paths.CONFIG/urgent-ping.json with command (a list of strings) and
an optional base_url. Run `python3 urgent_ping.py test` to check delivery.
"""
from collections import deque
import json
import subprocess
import sys
import threading
import time
from urllib.parse import quote

import paths

_LOCK = threading.Lock()
_PANES = {}
_SENT = deque()


def _name(pane):
    """The tab's visible name (tmux @chat_name, else the session name), or '' when tmux cannot tell."""
    if not pane:
        return ''
    try:
        out = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{?@chat_name,#{@chat_name},#{session_name}}'],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ''
    return out.stdout.strip()[:80] if out.returncode == 0 else ''


def _argv(ask):
    try:
        config = json.loads((paths.CONFIG / 'urgent-ping.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    command, base_url = config.get('command'), config.get('base_url', '')
    if (not isinstance(command, list) or not command or not isinstance(base_url, str)
            or any(not isinstance(arg, str) or '\0' in arg for arg in command) or not command[0]):
        return None
    pane = str(ask.get('pane') or '')
    values = {'{title}': 'Tether: ' + str(ask.get('name') or _name(pane) or pane),
              '{question}': str(ask.get('next') or ''), '{pane}': pane,
              '{url}': base_url.rstrip('/') + '/mobile/#session=' + quote(pane, safe='') if base_url else ''}
    argv = []
    for arg in command:
        for placeholder, value in values.items():
            arg = arg.replace(placeholder, value)
        if '\0' in arg:
            return None
        argv.append(arg)
    return argv if argv[0] else None


def _run(argv, runner):
    try:
        return runner(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'Urgent ping failed: {exc}', file=sys.stderr)
        return None


def send(ask, *, clock=time.monotonic, runner=subprocess.run):
    """Start a daemon worker, subject to per-pane and global rolling limits."""
    argv = _argv(ask)
    if argv is None:
        return False
    pane = ask.get('pane')
    with _LOCK:
        now = clock()
        while _SENT and _SENT[0] <= now - 3600:
            _SENT.popleft()
        for old in list(_PANES):
            if _PANES[old] <= now - 600:
                del _PANES[old]
        if pane in _PANES or len(_SENT) >= 6:
            print(f'Urgent ping rate limit: dropped pane {pane}', file=sys.stderr)
            return False
        worker = threading.Thread(target=_run, args=(argv, runner), daemon=True)
        worker.start()
        _PANES[pane] = now
        _SENT.append(now)
    return True


def test():
    """Run a test command in the foreground and show its result."""
    argv = _argv({'pane': '%0', 'next': 'Test ping from Tether'})
    if argv is None:
        print('Urgent ping is off: no valid urgent-ping.json config.')
        return 1
    result = _run(argv, subprocess.run)
    if result is None:
        return 1
    print(f'Exit code: {result.returncode}')
    tail = result.stderr or b''
    if isinstance(tail, bytes):
        tail = tail.decode('utf-8', 'replace')
    print('Stderr tail:\n' + tail[-2000:])
    return result.returncode


if __name__ == '__main__':
    if sys.argv[1:] == ['test']:
        sys.exit(test())
    sys.exit(__doc__)
