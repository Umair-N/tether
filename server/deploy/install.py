#!/usr/bin/env python3
"""Compatibility entry point, with the agent browser CLI in its install plan."""
import importlib.util
from pathlib import Path
import shlex
import sys


def add_browser_cli(plan, home=None):
    """Use the root installer's command plan so plan/apply output stays truthful."""
    if plan['action'] != 'install':
        return plan
    target = (home or Path.home()) / '.local/bin/tether-browser'
    script = Path(plan['repo']) / 'server/agent_browser_cli.py'
    wrapper = '#!/bin/sh\nexec ' + shlex.join([sys.executable, str(script)]) + ' "$@"\n'
    writer = ('from pathlib import Path; import sys; '
              'p = Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); '
              'p.write_text(sys.argv[2]); p.chmod(0o755)')
    command = [sys.executable, '-c', writer, str(target), wrapper]
    reload = plan['commands'].index(['systemctl', '--user', 'daemon-reload'])
    plan['commands'].insert(reload, command)
    return plan


def main():
    # The old installer applied immediately when no mode was supplied.
    if '--plan' not in sys.argv and '--apply' not in sys.argv:
        sys.argv.insert(1, '--apply')
    spec = importlib.util.spec_from_file_location('tether_installer', Path(__file__).resolve().parents[2] / 'install.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    make_plan = installer.make_plan
    installer.make_plan = lambda args: add_browser_cli(make_plan(args))
    return installer.main()


if __name__ == '__main__':
    sys.exit(main())
