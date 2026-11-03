"""Show one tmux pane in the live terminal, not the whole window, without changing the user's layout.

A tmux control-mode client (attach -f ignore-size, so it never resizes the window) reports when the pane prints.
On each change this view redraws the pane from capture-pane. Typed bytes go to that pane only, as send-keys -H.
The pane's size goes to the page in the terminal title ("tether-pane 80x24"), so the page can fit its font to it.
The mouse wheel scrolls back through the pane's tmux history, like copy mode in the whole-window view. A key goes back to the bottom.
With zoom (attach.py "z%7"), the pane fills this view: the window takes this view's size and the pane is zoomed as with prefix+z.
The last zoom view restores the window's size, sizing option and active pane. A later zoom repairs a killed view."""
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


def zoom_plan(pane):
    """(zoom it, the window's active pane before) for a pane in a window of several panes that nobody else has zoomed."""
    shown = tmux('display-message', '-p', '-t', pane, '#{window_zoomed_flag} #{window_panes} #{window_id}').stdout.split()
    if len(shown) != 3:
        return False, None
    active = tmux('display-message', '-p', '-t', shown[2], '#{pane_id}').stdout.strip()
    return shown[0] == '0' and shown[1] != '1', active


def unzoom(pane, active):
    """End the zoom this view started, if the pane is still the zoomed one, and give focus back to the pane that had it."""
    shown = tmux('display-message', '-p', '-t', pane, '#{window_zoomed_flag} #{pane_active}').stdout.split()
    if shown == ['1', '1']:
        tmux('resize-pane', '-Z', '-t', pane)
    if active and active != pane:
        tmux('select-pane', '-t', active)


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


def zoom_restore(window, state):
    if state['zooming']:
        unzoom(state['pane'], state['active'])
    # resize-window sets a local manual option. Restore local scope (including inheritance) afterwards.
    tmux('resize-window', '-t', window, '-x', str(state['width']), '-y', str(state['height'])).check_returncode()
    option = state['window_size']
    args = ['set-option', '-w', '-t', window, 'window-size', option] if option else [
        'set-option', '-wu', '-t', window, 'window-size']
    tmux(*args).check_returncode()
    # Setting the option recalculates from attached clients for automatic policies. The global option
    # is recorded, but never changed: other windows and desktop configuration must not be overwritten.


def zoom_enter(pane):
    """Save before any resize, or join an existing view. Repair abandoned state before taking a new snapshot."""
    shown = tmux('display-message', '-p', '-t', pane, '#{socket_path}\n#{pid}\n#{window_id}').stdout.splitlines()
    if len(shown) != 3:
        raise RuntimeError('That pane has ended.')
    window = shown[2]
    # Window IDs repeat across servers. Include the socket and server PID in the cache key.
    folder = paths.CACHE / 'zoom' / hashlib.sha256('\n'.join(shown).encode()).hexdigest()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    owner = uuid.uuid4().hex
    lease = (folder / owner).open('a')
    fcntl.flock(lease, fcntl.LOCK_EX)
    registered = False
    try:
        with zoom_lock(folder):
            file = folder / 'state.json'
            state = json.loads(file.read_text()) if file.exists() else None
            if state:
                state['owners'] = zoom_owners(folder, state)
                if not state['owners']:
                    zoom_restore(window, state)
                    file.unlink()
                    state = None
            if state is None:
                dimensions = tmux('display-message', '-p', '-t', window, '#{window_width} #{window_height}').stdout.split()
                width, height = map(int, dimensions)
                zooming, active = zoom_plan(pane)
                local = tmux('show-options', '-wqv', '-t', window, 'window-size')
                global_size = tmux('show-options', '-gwv', 'window-size')
                local.check_returncode()
                global_size.check_returncode()
                state = dict(width=width, height=height, pane=pane, zooming=zooming, active=active,
                             window_size=local.stdout.strip(), global_size=global_size.stdout.strip(), owners=[])
            first = not state['owners']
            state['owners'].append(owner)
            registered = True
            zoom_save(folder, state)
            if first and state['zooming']:
                tmux('resize-pane', '-Z', '-t', pane).check_returncode()
        return folder, window, lease
    except BaseException:
        try:
            if registered and (folder / 'state.json').exists():
                zoom_leave((folder, window, lease))
        except Exception:
            pass  # if cleanup also fails, keep the durable record for repair
        finally:
            lease.close()
        raise


def zoom_leave(view):
    folder, window, lease = view
    try:
        with zoom_lock(folder):
            state = json.loads((folder / 'state.json').read_text())
            lease.close()
            state['owners'] = zoom_owners(folder, state)
            if state['owners']:
                zoom_save(folder, state)
            else:
                zoom_restore(window, state)
                (folder / 'state.json').unlink()
    finally:
        lease.close()


def run(pane, zoom=False):
    result = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{session_id}'], capture_output=True, text=True)
    session = result.stdout.strip()
    if result.returncode or not session:
        raise SystemExit('That pane has ended. Refresh the session tabs.')
    # Attach to the session, not the pane: a pane target would make it the active pane on every screen.
    # ignore-size until this view knows its own size; with zoom, that flag goes once the size is set.
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
                    first = view is None
                    if first:
                        view = zoom_enter(pane)
                    control.stdin.write(b'refresh-client -C %dx%d\n' % (size.columns, size.lines))
                    if first:
                        control.stdin.write(b"refresh-client -f '!ignore-size'\n")
                    control.stdin.flush()
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
            # Detach the sizing client first, or it can shrink the restored window again.
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
