"""Attach ttyd clients to an existing tmux session ID ($N) or pane ID (%N), without arbitrary arguments.
A session ID attaches the whole session. A pane ID shows only that pane (pane_view), and the layout stays as it is.
"w" and a pane ID ("w%7") attach the pane's whole window and select that pane: the classic tmux view.
"z" and a pane ID ("z%7") show only that pane, zoomed to fill the view (pane_view)."""
import os
import re
import subprocess
import sys
import paths


def target(arguments):
    if len(arguments) != 1 or not re.fullmatch(r'[$%][0-9]+|[wz]%[0-9]+', arguments[0]):
        raise ValueError('Select a session or pane from the terminal tabs.')
    return arguments[0]


if __name__ == '__main__':
    try:
        session = target(sys.argv[1:])
    except ValueError as error:
        raise SystemExit(str(error))
    if session.startswith(('%', 'z%')):
        import pane_view
        pane_view.run(session.removeprefix('z'), zoom=session.startswith('z'))
        raise SystemExit(0)
    session = session.removeprefix('w')
    result = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', session, ''], capture_output=True)
    if result.returncode:
        raise SystemExit('That tmux session has ended. Refresh the session tabs.')
    os.execv(paths.TMUX, ['tmux', 'attach-session', '-t', session])
