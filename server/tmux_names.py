"""Names given in tmux flow back into Tether: tmux hooks copy a user's window or session rename into the pane
options @chat_name and @chat_name_at, which agents.discover reads. The hooks run inside tmux; they never call Tether.

A hook imports a name only when it is safe to say which pane it names:
- window-renamed: the window holds one pane. A window with several panes is left alone (no guess).
- session-renamed: the session holds one window with one pane.
It skips:
- automatic-rename (program names such as "zsh" or "claude"): tmux renames with automatic-rename on, while
  rename-window (and the prefix-, prompt) turns it off first.
- Tether's own rename: Tether sets @chat_name before it renames the window (and the session, for a pane rename), so
  the names already match. A session rename in Tether renames first; the hook then writes the same name Tether
  writes right after. The hooks only set pane options and never rename anything, so nothing can ping-pong.
- Names with "\\" or "$": tmux stores them escaped ("a\\$b"), so they can never match @chat_name, and the hook would
  write the escaped text. Such names stay tmux-only.

install() keeps the user's own hooks: it puts Tether's command in its own array slot of each hook (set-hook -g
hook[N]), finds that slot again by MARK, and uses a free slot the first time."""
import re
import shlex
import subprocess
import time

import paths

MARK = '@chat_name_at'  # every Tether name hook writes it; this is how install() finds its own slot
EVERY = 60  # seconds between checks: a restarted tmux server has lost its global hooks


def _command(name, required):
    """The hook command for one hook: name is the format of the new name, required the formats that must all be 1."""
    tests = [f'#{{==:{test},1}}' for test in required]
    tests += [name, f'#{{!=:{name},#{{@chat_name}}}}', f'#{{?#{{m:*\\\\*,{name}}},0,1}}']
    condition = tests[-1]
    for test in reversed(tests[:-1]):
        condition = f'#{{&&:{test},{condition}}}'
    # Formats have no clock, so a short shell job stamps the time (ms, as Tether writes it). It talks to this server.
    stamp = (f'{shlex.quote(paths.TMUX)} -S #{{q:socket_path}} set-option -p -t #{{pane_id}} {MARK} '
             '$(($(date +%s)*1000))')
    return f"if-shell -F '{condition}' {{ set-option -p -F @chat_name '{name}' ; run-shell -b '{stamp}' }}"


def hooks():
    return {'window-renamed': _command('#{window_name}', ['#{window_panes}', '#{?automatic-rename,0,1}']),
            'session-renamed': _command('#{session_name}', ['#{session_windows}', '#{window_panes}'])}


def install(run=subprocess.run):
    """Put Tether's name hooks in place (idempotent). False when tmux does not answer (no server yet)."""
    command = []
    for hook, text in hooks().items():
        # Ask per hook: a bare show-hooks -g leaves out window hooks such as window-renamed.
        shown = run([paths.TMUX, 'show-hooks', '-g', hook], capture_output=True, text=True, timeout=5)
        if shown.returncode:
            return False
        slots = {}
        for line in shown.stdout.splitlines():
            match = re.match(re.escape(hook) + r'\[(\d+)\] (.*)', line)
            if match:
                slots[int(match[1])] = match[2]
        ours = sorted(index for index, value in slots.items() if MARK in value)
        index = ours[0] if ours else next(i for i in range(len(slots) + 1) if i not in slots)
        command += [';', 'set-hook', '-g', f'{hook}[{index}]', text]
        for extra in ours[1:]:
            command += [';', 'set-hook', '-gu', f'{hook}[{extra}]']
    return run([paths.TMUX, *command[1:]], capture_output=True, text=True, timeout=5).returncode == 0


def watch(run=subprocess.run):
    """install() now and every EVERY seconds, for the service only (server.py __main__): a restarted tmux server
    has lost its global hooks. Never call it from tests; it talks to the user's own tmux."""
    while True:
        try:
            install(run)
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(EVERY)
