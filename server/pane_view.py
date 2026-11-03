"""Show one tmux pane in the live terminal, not the whole window, without changing the user's layout.

A tmux control-mode client (attach -f ignore-size, so it never resizes the window) reports when the pane prints.
On each change this view redraws the pane from capture-pane. Typed bytes go to that pane only, as send-keys -H.
The pane's size goes to the page in the terminal title ("tether-pane 80x24"), so the page can fit its font to it.
The mouse wheel scrolls back through the pane's tmux history, like copy mode in the whole-window view. A key goes back to the bottom.
With zoom (attach.py "z%7"), the pane fills this view: the window takes this view's size and the pane is zoomed as with prefix+z.
On exit the zoom ends and the window's active pane comes back. The window keeps the new size until another client resizes it."""
import os
import re
import select
import signal
import subprocess
import sys
import termios
import time
import tty
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


def run(pane, zoom=False):
    result = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{session_id}'], capture_output=True, text=True)
    session = result.stdout.strip()
    if result.returncode or not session:
        raise SystemExit('That pane has ended. Refresh the session tabs.')
    # Attach to the session, not the pane: a pane target would make it the active pane on every screen.
    # ignore-size until this view knows its own size; with zoom, that flag goes once the size is set.
    zooming, active = zoom_plan(pane) if zoom else (False, None)
    control = subprocess.Popen([paths.TMUX, '-C', 'attach-session', '-f', 'ignore-size', '-t', session],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    saved = termios.tcgetattr(0) if os.isatty(0) else None
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
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
                first = size is None
                size, dirty = os.get_terminal_size(1), True
                if zoom and size.columns and size.lines:
                    control.stdin.write(b'refresh-client -C %dx%d\n' % (size.columns, size.lines))
                    if first:
                        control.stdin.write(b"refresh-client -f '!ignore-size'\n")
                        if zooming:
                            control.stdin.write(b'resize-pane -Z -t %s\n' % pane.encode())
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
        os.write(1, MOUSE_OFF)
        if zooming:
            unzoom(pane, active)
        if saved:
            termios.tcsetattr(0, termios.TCSADRAIN, saved)
        control.kill()
        control.wait(timeout=5)
