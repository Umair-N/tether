"""Where Mobile Tmux finds its own files and each agent's files. Every location can be overridden with an environment variable."""
import os
from pathlib import Path
import shutil

HOME = Path(os.environ.get('MOBILE_TMUX_HOME') or Path.home())
CACHE = Path(os.environ.get('MOBILE_TMUX_CACHE') or HOME / '.cache/mobile-tmux')
TETHER = Path(os.environ.get('TETHER_HOME') or HOME / '.tether')  # session projects (projects.py)
CONFIG = Path(os.environ.get('MOBILE_TMUX_CONFIG') or HOME / '.config/mobile-tmux')
TMUX = os.environ.get('MOBILE_TMUX_TMUX') or shutil.which('tmux') or '/usr/bin/tmux'
SYSTEMD_RUN = shutil.which('systemd-run')
# server.py sets this at start when it runs as a systemd service. Not read from the environment here: tmux panes
# started by the service inherit INVOCATION_ID too.
SCOPE_TMUX = False


def tmux_new_session():
    """The start of a `tmux new-session` command. When this runs as a systemd service (INVOCATION_ID is set) and no
    tmux server is up yet, new-session starts the server. It must not start inside this service's cgroup: a service
    restart kills every process there, and with it every user session (this happened on 2 Nov 2026). So the command
    runs in its own transient scope."""
    if SYSTEMD_RUN and SCOPE_TMUX:
        return [SYSTEMD_RUN, '--user', '--scope', '--quiet', '--collect', TMUX]
    return [TMUX]

CLAUDE = HOME / '.claude'
CODEX = HOME / '.codex'
ANTIGRAVITY = HOME / '.gemini/antigravity-cli'


def private_file(name):
    """A file under the config folder (owner-only data such as the access token and push keys)."""
    return CONFIG / name
