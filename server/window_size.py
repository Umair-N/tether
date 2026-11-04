"""Own agent window sizes without letting attached clients shrink them.

The status cycle supplies panes from agents.discover. A tmux global option holds
that registry for short-lived hook processes; no second agent discovery is needed.
Window IDs and pane IDs come from tmux on every apply, including after Zoom swaps.
Only windows marked OWNED are reset on release or uninstall. Zoom owns its own size.
"""
import logging
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

import paths

FLOOR = (120, 40)
REGISTRY = '@tether_agent_panes'
CONTROL = '@tether_control_sizes'
OWNED = '@tether_window_size'
ZOOM = '@tether_zoom'
MARK = 'tether-window-size'
HOOKS = ('client-attached', 'client-resized', 'client-detached', 'session-window-changed', 'after-refresh-client')
EVERY = 60
_warned_old = False


def target(clients, floor):
    """Per-dimension maximum of (width, height, comma-separated flags) clients and floor."""
    width, height = floor
    for x, y, flags in clients:
        if 'ignore-size' not in flags.split(','):
            width, height = max(width, x), max(height, y)
    return width, height


def apply(run=subprocess.run, *, entries=None):
    """Apply the registry, or publish fresh discovery entries and apply them.

    Two queries on an unchanged cycle: list-windows and list-clients. The total
    tmux budget leaves room for Python startup in the hook's 300 ms deadline.
    A missing or busy tmux server is harmless; the next hook/status cycle retries.
    """
    deadline = time.monotonic() + .24
    def command(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(paths.TMUX, .24)
        return run([paths.TMUX, *args], capture_output=True, text=True, timeout=remaining)
    try:
        shown = command('list-windows', '-a', '-F',
                        '#{session_id}\t#{window_id}\t#{window_width}\t#{window_height}\t#{window-size}\t'
                        f'#{{{OWNED}}}\t#{{{ZOOM}}}\t#{{P:#{{pane_id}}|}}\t#{{{REGISTRY}}}')
        if shown.returncode:
            return False
        rows = [line.split('\t') for line in shown.stdout.splitlines()]
        if not rows:
            return True
        if any(len(row) != 9 for row in rows):
            return False
        registry = rows[0][-1] if entries is None else ','.join(sorted({
            e['id'] for e in entries if e.get('agent') in ('claude', 'codex', 'antigravity')
            and re.fullmatch(r'%\d+', e['id'])}))
        agents = set(registry.split(',')) - {''}
        shown = command('list-clients', '-F',
                        '#{session_id}\t#{client_width}\t#{client_height}\t#{client_flags}\t'
                        f'#{{client_pid}}\t#{{{CONTROL}}}')
        if shown.returncode:
            return False
        clients = {}
        for line in shown.stdout.splitlines():
            session, width, height, flags, pid, sizes = line.split('\t')
            if 'ignore-size' in flags.split(','):
                continue
            # tmux 3.4 omits client_height for pipe-fed control clients. The
            # after-refresh-client hook records their validated -C dimensions.
            recorded = dict(part.split('=', 1) for part in sizes.split('|') if '=' in part)
            if not height and pid in recorded:
                match = re.fullmatch(r'(\d+)[x,](\d+)', recorded[pid])
                if match:
                    width, height = match.groups()
            clients.setdefault(session, []).append((int(width), int(height or 0), flags))
        windows = {}
        for session, window, width, height, sizing, owned, zoom, panes, _ in rows:
            item = windows.setdefault(window, dict(width=int(width), height=int(height), sizing=sizing,
                                                  owned=owned, zoom=zoom, panes=set(panes.split('|')), clients=[]))
            # Linked windows obey all attached clients of all their sessions.
            item['clients'].extend(clients.get(session, []))
        commands = []
        def add(*args):
            if commands:
                commands.append(';')
            commands.extend(args)
        if entries is not None and registry != rows[0][-1]:
            add('set-option', '-g', REGISTRY, registry)
        for window, item in windows.items():
            if item['zoom']:
                continue
            if not agents.intersection(item['panes']):
                if item['owned']:
                    add('set-option', '-wu', '-t', window, 'window-size')
                    add('set-option', '-wu', '-t', window, OWNED)
                continue
            if not item['owned'] or item['sizing'] != 'manual':
                add('set-option', '-w', '-t', window, 'window-size', 'manual')
                add('set-option', '-w', '-t', window, OWNED, '1')
            width, height = target(item['clients'], FLOOR)
            if (width, height) != (item['width'], item['height']):
                add('resize-window', '-t', window, '-x', str(width), '-y', str(height))
        return not commands or command(*commands).returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _slots(run, hook):
    shown = run([paths.TMUX, 'show-hooks', '-g', hook], capture_output=True, text=True, timeout=5)
    shown.check_returncode()
    return {int(match[1]): match[2] for line in shown.stdout.splitlines()
            if (match := re.match(re.escape(hook) + r'\[(\d+)\] (.*)', line))}


def install(run=subprocess.run):
    """Keep user hooks and replace only our marked array slots. Requires tmux 3.2+."""
    global _warned_old
    try:
        version = run([paths.TMUX, '-V'], capture_output=True, text=True, timeout=5)
        match = re.search(r'tmux (\d+)\.(\d+)', version.stdout)
        if version.returncode or not match:
            return False
        if tuple(map(int, match.groups())) < (3, 2):
            if not _warned_old:
                logging.warning('Tether window size hooks require tmux 3.2+; hooks skipped.')
                _warned_old = True
            return False
        shell = (shlex.join([sys.executable, str(Path(__file__).resolve()), 'apply', '--socket'])
                 + ' #{q:socket_path} # ' + MARK)
        text = 'run-shell -b ' + shlex.quote(shell)
        # Update the map inside tmux's command queue, before the asynchronous
        # apply. This avoids lost updates from simultaneous control resizes.
        cleared = f'#{{s/(^|[|])#{{client_pid}}=[^|]*[|]//:{CONTROL}}}'
        commands = []
        for hook in HOOKS:
            slots = _slots(run, hook)
            command = text
            if hook == 'after-refresh-client':
                valid = '#{m/r:^[0-9]+[^0-9][0-9]+$,#{hook_flag_C}}'
                command = (f"if-shell -F '#{{&&:#{{client_control_mode}},{valid}}}' {{ "
                           f"set-option -gF {CONTROL} '{cleared}|#{{client_pid}}=#{{hook_flag_C}}|' ; {text} }}")
            elif hook == 'client-detached':
                command = f"set-option -gF {CONTROL} '{cleared}' ; {text}"
            ours = sorted(i for i, value in slots.items() if MARK in value)
            index = ours[0] if ours else next(i for i in range(len(slots) + 1) if i not in slots)
            commands += [';', 'set-hook', '-g', f'{hook}[{index}]', command]
            for extra in ours[1:]:
                commands += [';', 'set-hook', '-gu', f'{hook}[{extra}]']
        return run([paths.TMUX, *commands[1:]], capture_output=True, text=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def uninstall(run=subprocess.run):
    """Remove our hooks and window overrides; preserve user hooks and other windows."""
    try:
        for hook in HOOKS:
            for index, value in _slots(run, hook).items():
                if MARK in value:
                    run([paths.TMUX, 'set-hook', '-gu', f'{hook}[{index}]'],
                        capture_output=True, text=True, timeout=5)
        shown = run([paths.TMUX, 'list-windows', '-a', '-F', f'#{{window_id}}\t#{{{OWNED}}}'],
                    capture_output=True, text=True, timeout=5)
        for window in {line.split('\t')[0] for line in shown.stdout.splitlines() if line.endswith('\t1')}:
            run([paths.TMUX, 'set-option', '-wu', '-t', window, 'window-size', ';',
                 'set-option', '-wu', '-t', window, OWNED], capture_output=True, text=True, timeout=5)
        run([paths.TMUX, 'set-option', '-gu', REGISTRY], capture_output=True, text=True, timeout=5)
        run([paths.TMUX, 'set-option', '-gu', CONTROL], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass  # No running tmux server is a normal uninstall case.


def watch(run=subprocess.run):
    """Service-only timer: a restarted tmux server loses global hooks."""
    while True:
        install(run)
        time.sleep(EVERY)


if __name__ == '__main__':
    # Capture every tmux response. Hook jobs must never write to the user's terminal.
    run = subprocess.run
    if len(sys.argv) == 4 and sys.argv[2] == '--socket':
        socket = sys.argv[3]
        def run(argv, **kwargs):
            return subprocess.run([argv[0], '-S', socket, *argv[1:]], **kwargs)
    if len(sys.argv) > 1 and sys.argv[1] == 'apply':
        apply(run)
    elif sys.argv[1:] == ['uninstall']:
        uninstall(run)
