"""Plain shells for the desktop terminal panel, kept apart from the user's own tmux sessions.

Each shell is one window in a dedicated tmux session (SESSION). A window remembers its folder in the window option
@tether_folder, so opening the panel again for the same folder reuses the shell. Nothing here touches any other session:
every lookup and kill first checks that the pane belongs to SESSION. Session discovery (agents.discover) hides SESSION."""
import os
import re
import subprocess
import shutil
import shlex
import paths
import files

SESSION = 'tether-shells'
PANE = re.compile(r'%[0-9]+')
FORMAT = '#{pane_id}\t#{session_name}\t#{@tether_folder}'


def _tmux(run, *args):
    return run([paths.TMUX, *args], capture_output=True, text=True, timeout=5)


def _panes(run):
    """(pane id, folder) for the first pane of each shell window. Empty when the session does not exist."""
    result = _tmux(run, 'list-panes', '-s', '-t', '=' + SESSION, '-f', '#{&&:#{==:#{@zoom_for},},#{!=:#{window_name},tether-zoom}}', '-F', FORMAT)
    if result.returncode:
        return []
    out = []
    for line in result.stdout.splitlines():
        fields = line.split('\t', 2)
        if len(fields) == 3 and fields[1] == SESSION and PANE.fullmatch(fields[0]):
            out.append((fields[0], fields[2]))
    return out


def literal(text):
    """tmux expands formats in -c and -n, and #(...) runs a shell command. ## is a plain #."""
    return text.replace('#', '##')


def folder_in_root(text, root=None):
    """The real folder for a Files explorer path (~/... or absolute). It must resolve inside the files root and not be secret.
    Raises files.FilesError (a ValueError) with a sentence for the page."""
    root = files.files_root(root)
    _, real, is_dir, _ = files.locate(text, root)
    if not is_dir:
        raise files.invalid('Choose a folder.')
    return str(real)


def owns(pane, run=subprocess.run):
    """True when `pane` is a shell window of SESSION."""
    if not isinstance(pane, str) or not PANE.fullmatch(pane):
        return False
    result = _tmux(run, 'display-message', '-p', '-t', pane, '#{session_name}')
    return result.returncode == 0 and result.stdout.strip() == SESSION


# btop draws nothing below 80x24 with every box. With only the CPU and process boxes it needs 60x24, which fits a phone.
# btop 1.3.0 ignores --preset when it checks the size and has no config flag, so the phone layout gets its own config folder.
BTOP_SIZE = {False: (80, 24), True: (60, 30)}


def compact_btop_home():
    """An XDG_CONFIG_HOME whose btop.conf shows only the CPU and process boxes. Written on every start: btop rewrites it on exit."""
    home = paths.CACHE / 'btop-compact'
    (home / 'btop').mkdir(parents=True, exist_ok=True)
    (home / 'btop' / 'btop.conf').write_text('shown_boxes = "cpu proc"\n')
    return home


def open_shell(folder, pane=None, new=False, run=subprocess.run, program=None, compact=False):
    """The pane id of a shell in `folder`.
    `pane`: keep showing that shell while it lives. Otherwise reuse the folder's shell, unless `new` asks for another one.
    `program` 'btop' starts btop in a window of a fixed size (`compact`: the phone layout); the view fits its font to that size."""
    folder = folder if isinstance(folder, str) and os.path.isabs(folder) and os.path.isdir(folder) else str(paths.HOME)
    command = []
    if program is not None:
        executable = shutil.which('btop') if program == 'btop' else None
        if not executable:
            raise ValueError('btop is unavailable.')
        command = [shlex.quote(executable)]
        if compact:
            command = ['env', shlex.quote(f'XDG_CONFIG_HOME={compact_btop_home()}'), *command]
        new = True
    shells = _panes(run)
    if pane and any(item[0] == pane and (program is None or item[1] == '') for item in shells):
        return pane
    if not new:
        found = next((item[0] for item in shells if item[1] == folder), None)
        if found:
            return found
    name = os.path.basename(folder.rstrip('/')) or 'shell'
    if shells:
        result = _tmux(run, 'new-window', '-d', '-t', '=' + SESSION + ':', '-c', literal(folder), '-n', literal(name), '-P', '-F', '#{pane_id}', *command)
    else:
        # A new tmux session gets an 80x24 window; the live view resizes nothing, so start it wide enough for a panel.
        result = run([*paths.tmux_new_session(), 'new-session', '-d', '-s', SESSION, '-x', '120', '-y', '32', '-c', literal(folder),
                      '-n', literal(name), '-P', '-F', '#{pane_id}', *command], capture_output=True, text=True, timeout=5)
    created = result.stdout.strip()
    if result.returncode or not PANE.fullmatch(created):
        raise RuntimeError(result.stderr.strip() or 'Could not start a shell.')
    # A monitor must not become the folder's reusable interactive shell.
    _tmux(run, 'set-option', '-w', '-t', created, '@tether_folder', '' if program else folder)
    if program:
        # resize-window also sets window-size to manual, so a client that attaches later does not shrink it.
        width, height = BTOP_SIZE[bool(compact)]
        _tmux(run, 'resize-window', '-t', created, '-x', str(width), '-y', str(height))
    return created


def end_shell(pane, run=subprocess.run):
    """Close one shell window. Refuses any pane outside SESSION."""
    if not owns(pane, run):
        raise ValueError('That shell has already ended.')
    result = _tmux(run, 'kill-window', '-t', pane)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'Could not end the shell.')
