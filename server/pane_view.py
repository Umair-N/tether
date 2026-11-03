"""Show one tmux pane in the live terminal, not the whole window, without changing the user's layout.

A tmux control-mode client (attach -f ignore-size, so it never resizes the window) reports when the pane prints.
On each change this view redraws the pane from capture-pane. Typed bytes go to that pane only, as send-keys -H.
The pane's size goes to the page in the terminal title ("tether-pane 80x24"), so the page can fit its font to it.
The mouse wheel scrolls back through the pane's tmux history, like copy mode in the whole-window view. A key goes back to the bottom.
With zoom (attach.py "z%7"), the pane swaps into a private window in the same session.
A placeholder keeps its home slot, size and focus; other panes never resize. The control client always ignores size.
Views share the private window with manual sizing; the latest resize wins. The last owner swaps the pane home.
Locked leases and durable state repair killed views at startup, discovery or the next zoom, including old resize state."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import termios
import time
import tty
import uuid
import paths

FRAME = 0.05     # seconds between redraws while the pane prints
IDLE = 0.5       # seconds between checks for a phone resize when nothing happens
HEARTBEAT = 1.0  # a full redraw at least this often: a missed event, and the size title for a page that just loaded
CHUNK = 256      # bytes per send-keys command
WHEEL = 3        # history lines for each wheel step
MOUSE_ON, MOUSE_OFF = b'\x1b[?1000h\x1b[?1006h', b'\x1b[?1000l\x1b[?1006l'  # report buttons and the wheel, SGR format
SGR_MOUSE = re.compile(rb'\x1b\[<(\d+);\d+;\d+[Mm]')


def split_mouse(data):
    """(typed bytes, wheel lines up (+) or down (-), an unfinished mouse report to keep for the next read).
    Mouse reports are taken out; only the wheel does something. Clicks stay with the browser's own selection (Shift+drag)."""
    lines = 0
    for match in SGR_MOUSE.finditer(data):
        button = int(match.group(1)) & ~0b11100  # drop Shift, Alt and Ctrl
        lines += WHEEL if button == 64 else -WHEEL if button == 65 else 0
    keys = SGR_MOUSE.sub(b'', data)
    start = keys.rfind(b'\x1b[<')
    if start >= 0 and re.fullmatch(rb'\x1b\[<[\d;]*', keys[start:]):
        return keys[:start], lines, keys[start:]
    return keys, lines, b''


def key_commands(pane, data):
    """send-keys lines for typed bytes. Hex keys pass every byte as is, UTF-8 included, with no quoting."""
    return [('send-keys -t %s -H %s\n' % (pane, ' '.join('%02x' % byte for byte in data[start:start + CHUNK]))).encode()
            for start in range(0, len(data), CHUNK)]


def frame(meta, lines, rows, back=0, history=0):
    """The escape sequences that draw the pane at the top of a terminal with `rows` rows.
    A pane taller than the terminal shows its bottom rows, where the prompt is.
    `back` lines up in the history, the cursor hides and a [back/history] tag shows at the top right, as in tmux copy mode."""
    cursor_x, cursor_y, cursor_on, width, height = meta
    top = max(0, height - rows)
    out = ['\x1b[?2026h\x1b[?25l\x1b]2;tether-pane %dx%d\x07' % (width, height)]
    for row, line in enumerate(lines[top:top + rows]):
        out.append('\x1b[%d;1H\x1b[0m%s\x1b[0m\x1b[K' % (row + 1, line))
    out.append('\x1b[0m\x1b[J')
    if back:
        tag = '[%d/%d]' % (back, history)
        out.append('\x1b[1;%dH\x1b[0;7m%s\x1b[0m' % (max(1, width - len(tag) + 1), tag))
    y = cursor_y - top
    if cursor_on and not back and 0 <= y < rows:
        out.append('\x1b[%d;%dH\x1b[?25h' % (y + 1, cursor_x + 1))
    out.append('\x1b[?2026l')
    return ''.join(out)


def snapshot(pane, back=0):
    """(cursor_x, cursor_y, cursor_on, width, height), screen lines with colours, history size. None when the pane has ended.
    `back` > 0 shows the screen that many lines up in the history."""
    capture = ['capture-pane', '-p', '-e', '-t', pane] + (['-S', str(-back)] if back else [])
    result = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane,
                             '#{cursor_x} #{cursor_y} #{cursor_flag} #{pane_width} #{pane_height} #{history_size}', ';',
                             *capture], capture_output=True, timeout=5)
    if result.returncode:
        return None
    text = result.stdout.decode('utf-8', 'replace').split('\n')
    try:
        meta = tuple(int(value) for value in text[0].split())
    except ValueError:
        return None
    if len(meta) != 6:
        return None
    return meta[:5], text[1:1 + meta[4]], meta[5]


def tmux(*args):
    return subprocess.run([paths.TMUX, *args], capture_output=True, text=True, timeout=5)


@contextmanager
def zoom_lock(folder):
    # Never unlink this lock: every process must lock the same inode.
    with (folder / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def zoom_save(folder, state):
    temporary = folder / 'state.tmp'
    with temporary.open('w') as file:
        json.dump(state, file)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(folder / 'state.json')
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def zoom_owners(folder, state):
    """Keep only views whose lease is still locked. Locks also handle SIGKILL and PID reuse."""
    live = []
    for owner in state['owners']:
        file = folder / owner
        with file.open('a') as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                live.append(owner)
            else:
                file.unlink()
    return live


def zoom_command(state, *args):
    return tmux('-S', state['socket'], *args) if state.get('socket') else tmux(*args)


def zoom_restore(state):
    """Restore a swap, or migrate an abandoned record from the old window resize design."""
    command = lambda *args: zoom_command(state, *args)
    if 'width' in state:
        # Old records lack socket/PID fields. Find their window by its original cache key,
        # even if the pane has ended but other panes still need their width restored.
        windows = command('list-windows', '-a', '-F', '#{socket_path}\t#{pid}\t#{window_id}').stdout.splitlines()
        window = next((fields[2] for line in windows if len(fields := line.split('\t')) == 3
                       and hashlib.sha256('\n'.join(fields).encode()).hexdigest() == state['key']), None)
        if not window:
            return
        if state['zooming']:
            flags = command('display-message', '-p', '-t', state['pane'],
                            '#{window_zoomed_flag} #{pane_active}').stdout.split()
            if flags == ['1', '1']:
                command('resize-pane', '-Z', '-t', state['pane']).check_returncode()
            if state['active'] and state['active'] != state['pane']:
                command('select-pane', '-t', state['active'])
        command('resize-window', '-t', window, '-x', str(state['width']), '-y', str(state['height'])).check_returncode()
        option = state['window_size']
        command('set-option', '-w' if option else '-wu', '-t', window, 'window-size',
                *([option] if option else [])).check_returncode()
        return
    if command('display-message', '-p', '#{pid}').stdout.strip() != state['server_pid']:
        return  # IDs can repeat after a server restart.
    pane, placeholder, window = state['pane'], state['placeholder'], state['window']
    location = command('display-message', '-p', '-t', pane, '#{window_id}').stdout.strip()
    home = command('display-message', '-p', '-t', placeholder, '#{window_id}').stdout.strip()
    if location == window and home and home != window:
        command('swap-pane', '-dZ', '-s', placeholder, '-t', pane).check_returncode()
        location = home
    if location == window:
        # The home slot was deleted. Keep the live pane visible in its surviving window, named after its program.
        # No rename-window: Tether's window-renamed hook would copy that name to the pane's chat name.
        command('set-option', '-w', '-t', window, 'automatic-rename', 'on').check_returncode()
    else:
        # Never kill a window that still holds the real pane, including after a partial restore.
        if command('display-message', '-p', '-t', window, '#{window_id}').stdout.strip():
            command('kill-window', '-t', window).check_returncode()
        if home and home != window and location != home:
            command('kill-pane', '-t', placeholder)  # real pane ended, or was moved elsewhere


def zoom_repair_all():
    """Repair records with no locked owner. An empty cache needs no tmux calls."""
    for file in (paths.CACHE / 'zoom').glob('*/state.json'):
        try:
            with zoom_lock(file.parent):
                if not file.exists():
                    continue
                state = json.loads(file.read_text())
                state['owners'] = zoom_owners(file.parent, state)
                if not state['owners']:
                    state['key'] = file.parent.name
                    zoom_restore(state)
                    file.unlink()
        except (OSError, ValueError, subprocess.SubprocessError):
            continue  # Keep failed restores for the next discovery; one failure must not block others.


def zoom_enter(pane):
    """Join a pane's private window, or repair its abandoned view and swap it out again."""
    zoom_repair_all()
    shown = tmux('display-message', '-p', '-t', pane, '#{socket_path}\n#{pid}\n#{pane_id}\n#{session_id}').stdout.splitlines()
    if len(shown) != 4:
        raise RuntimeError('That pane has ended.')
    folder = paths.CACHE / 'zoom' / hashlib.sha256('\n'.join(shown[:3]).encode()).hexdigest()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    owner = uuid.uuid4().hex
    lease = (folder / owner).open('a')
    fcntl.flock(lease, fcntl.LOCK_EX)
    registered = False
    window = None
    try:
        with zoom_lock(folder):
            file = folder / 'state.json'
            state = json.loads(file.read_text()) if file.exists() else None
            if state:
                state['owners'] = zoom_owners(folder, state)
                if not state['owners']:
                    zoom_restore(state)
                    file.unlink()
                    state = None
            if state is None:
                created = tmux('new-window', '-d', '-t', shown[3] + ':', '-n', 'tether-zoom',
                               '-P', '-F', '#{window_id} #{pane_id}',
                               'printf "Shown on the phone (Zoom).\\n"; exec tail -f /dev/null')
                created.check_returncode()
                window, placeholder = created.stdout.split()
                state = dict(socket=shown[0], server_pid=shown[1], pane=pane, window=window,
                             placeholder=placeholder, owners=[])
            first = not state['owners']
            window = state['window']
            state['owners'].append(owner)
            zoom_save(folder, state)  # IDs are durable before any swap.
            registered = True
            if first:
                zoom_command(state, 'set-option', '-p', '-t', state['placeholder'], '@zoom_for', pane).check_returncode()
                zoom_command(state, 'set-option', '-w', '-t', window, 'window-size', 'manual').check_returncode()
                zoom_command(state, 'swap-pane', '-dZ', '-s', pane, '-t', state['placeholder']).check_returncode()
        return folder, window, lease
    except BaseException:
        try:
            if registered:
                zoom_leave((folder, window, lease))
        except Exception:
            pass  # Keep the durable record when cleanup also fails.
        finally:
            lease.close()
        raise


def zoom_resize(view, width, height):
    folder, window, _ = view
    with zoom_lock(folder):
        state = json.loads((folder / 'state.json').read_text())
        zoom_command(state, 'resize-window', '-t', window, '-x', str(width), '-y', str(height)).check_returncode()


def zoom_leave(view):
    folder, _, lease = view
    try:
        with zoom_lock(folder):
            file = folder / 'state.json'
            lease.close()
            if not file.exists():
                return
            state = json.loads(file.read_text())
            state['owners'] = zoom_owners(folder, state)
            if state['owners']:
                zoom_save(folder, state)
            else:
                zoom_restore(state)
                file.unlink()
    finally:
        lease.close()


def run(pane, zoom=False):
    result = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{session_id}'], capture_output=True, text=True)
    session = result.stdout.strip()
    if result.returncode or not session:
        raise SystemExit('That pane has ended. Refresh the session tabs.')
    # Attach to the session, not the pane: a pane target would make it the active pane on every screen.
    # Keep ignore-size for the entire view; only the private window is resized.
    control = saved = view = None
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        control = subprocess.Popen([paths.TMUX, '-C', 'attach-session', '-f', 'ignore-size', '-t', session],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        saved = termios.tcgetattr(0) if os.isatty(0) else None
        if saved:
            tty.setraw(0)
        os.write(1, b'\x1b[?1049h\x1b[?7l\x1b[H\x1b[2J' + MOUSE_ON)  # own screen, no line wrap: a wide line is cut, not folded
        marker = ('%%output %s ' % pane).encode()
        pending = typed = b''
        dirty, drawn, size = True, 0.0, None
        back, history = 0, 0  # lines scrolled up; the history size at the last draw
        while True:
            now = time.monotonic()
            if os.get_terminal_size(1) != size:
                size, dirty = os.get_terminal_size(1), True
                if zoom and size.columns and size.lines:
                    if view is None:
                        view = zoom_enter(pane)
                    zoom_resize(view, size.columns, size.lines)
            if dirty and now - drawn >= FRAME or now - drawn >= HEARTBEAT:
                shot = snapshot(pane, back)
                if shot is None:
                    os.write(1, MOUSE_OFF + b'\x1b[?1049l\r\nThe pane has ended.\r\n')
                    return
                if back and shot[2] > history:  # new output pushed lines into the history: stay on the same text
                    back = min(back + shot[2] - history, shot[2])
                    shot = snapshot(pane, back) or shot
                history = shot[2]
                os.write(1, frame(shot[0], shot[1], size.lines, back, history).encode())
                dirty, drawn = False, time.monotonic()
            wait = max(0.0, FRAME - (time.monotonic() - drawn)) if dirty else IDLE
            ready, _, _ = select.select([0, control.stdout], [], [], wait)
            if 0 in ready:
                data = os.read(0, 4096)
                if not data:
                    return
                data, wheel, typed = split_mouse(typed + data)
                if wheel:
                    back, dirty = max(0, min(history, back + wheel)), True
                if data and back:
                    back, dirty = 0, True  # typing goes back to the live screen
                for command in key_commands(pane, data):
                    control.stdin.write(command)
                control.stdin.flush()
            if control.stdout in ready:
                data = os.read(control.stdout.fileno(), 65536)
                if not data:
                    os.write(1, MOUSE_OFF + b'\x1b[?1049l\r\nThe tmux session has ended.\r\n')
                    return
                lines = (pending + data).split(b'\n')
                pending = lines.pop()
                for line in lines:
                    if line.startswith(marker) or line.startswith((b'%layout-change', b'%window-pane-changed')):
                        dirty = True
                    elif line.startswith(b'%exit'):
                        os.write(1, MOUSE_OFF + b'\x1b[?1049l\r\nThe tmux session has ended.\r\n')
                        return
    finally:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            # Stop control events before swapping the pane home.
            if control:
                control.kill()
                control.wait(timeout=5)
        finally:
            try:
                if view:
                    zoom_leave(view)
            finally:
                try:
                    os.write(1, MOUSE_OFF)
                finally:
                    if saved:
                        termios.tcsetattr(0, termios.TCSADRAIN, saved)
