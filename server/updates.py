"""Daily release discovery and detached, reported release updates."""
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen

import paths

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400
_LOCK = threading.Lock()
_START_LOCK = threading.Lock()
_RUNNING = False
_ATTEMPTED = {}


def version(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-rc(?:\.?)(\d+))?', str(value))
    if not match:
        raise ValueError('Invalid release version')
    major, minor, patch, rc = match.groups()
    return (int(major), int(minor), int(patch), rc is None, int(rc or 0))


def current_version():
    try:
        return ROOT.joinpath('VERSION').read_text().strip()
    except OSError:
        return '0.1.0'


def install_file():
    return Path(os.environ.get('TETHER_INSTALL_STATE') or paths.HOME / '.config/tether/install.json')


def mode():
    try:
        state = json.loads(install_file().read_text())
        value = state.get('updates', state.get('options', {}).get('updates', 'notify'))
        return value if value in ('notify', 'auto', 'off') else 'notify'
    except (OSError, ValueError, AttributeError):
        return 'notify'


def run_file():
    return paths.CACHE / 'update-run.json'


def write_run(data):
    file = run_file()
    file.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.update-run-', dir=file.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(data, stream)
        temp.replace(file)
    finally:
        temp.unlink(missing_ok=True)


def read_run():
    try:
        data = json.loads(run_file().read_text())
        if (not isinstance(data, dict) or data.get('state') not in ('running', 'failed', 'done') or
                not isinstance(data.get('target'), str) or not isinstance(data.get('error'), str) or
                type(data.get('started_at')) not in (int, float) or not math.isfinite(data['started_at'])):
            return None
        version(data['target'])
        data = dict(state=data['state'], target=data['target'].removeprefix('v'),
                    error=data['error'], started_at=float(data['started_at']))
        if data['state'] == 'running' and time.time() - data['started_at'] > 900:
            data.update(state='failed', error='The update stopped without a result. Run tether update on the host.')
        return data
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def can_update():
    try:
        return (ROOT.parent == (paths.HOME / '.local/share/tether/releases').resolve() and
                ROOT.name == ROOT.joinpath('VERSION').read_text().strip() and
                not (ROOT / '.git').exists() and not (ROOT / '.git').is_symlink() and
                isinstance(json.loads(install_file().read_text()), dict))
    except (OSError, ValueError):
        return False


class UpdateRefused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def launch(target):
    command = [sys.executable, str(ROOT / 'tether'), 'update', '--yes', '--to', target, '--report']
    env = os.environ.copy()
    termux = sys.platform != 'darwin' and (bool(env.get('TERMUX_VERSION')) or
                                         env.get('PREFIX', '').startswith('/data/data/com.termux/'))
    if sys.platform.startswith('linux') and paths.SYSTEMD_RUN and not termux:
        settings = ['--setenv=' + key + '=' + value for key, value in env.items()
                    if key == 'PATH' or key.startswith(('TETHER_', 'MOBILE_TMUX_', 'XDG_'))]
        result = subprocess.run([paths.SYSTEMD_RUN, '--user', '--collect', '--quiet',
                                 '--unit=tether-self-update', *settings, *command],
                                timeout=10, capture_output=True, text=True, env=env)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'Could not start the update.')
    else:
        paths.CACHE.mkdir(parents=True, exist_ok=True)
        with (paths.CACHE / 'update-run.log').open('ab') as log:
            proc = subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log, env=env)
        threading.Thread(target=proc.wait, daemon=True).start()


def start(target):
    with _START_LOCK:
        if mode() == 'off':
            raise UpdateRefused(409, 'Updates are disabled.')
        if not can_update():
            raise UpdateRefused(409, 'This install updates with git pull.')
        if not isinstance(target, str):
            raise UpdateRefused(400, 'Choose a release version.')
        try:
            latest = read_cache(paths.CACHE / 'updates.json').get('latest')
            if version(target) != version(latest) or version(target) <= version(current_version()):
                raise ValueError('Unavailable release')
        except (ValueError, TypeError):
            raise UpdateRefused(409, 'That update is no longer available.') from None
        active = read_run()
        if active and active['state'] == 'running':
            raise UpdateRefused(409, 'An update is already running.')
        data = dict(state='running', target=target.removeprefix('v'), error='', started_at=time.time())
        write_run(data)
        try:
            launch(data['target'])
        except Exception as error:
            data.update(state='failed', error=str(error) or 'Could not start the update.')
            write_run(data)
            raise UpdateRefused(500, data['error']) from error
        return data


def release_endpoint(tag=None):
    repo = os.environ.get('TETHER_REPO', 'incmak/tether')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid TETHER_REPO')
    return f'https://api.github.com/repos/{repo}/releases/' + ('tags/' + tag if tag else 'latest')


def fetch_release(tag=None):
    with urlopen(Request(release_endpoint(tag), headers={'Accept': 'application/vnd.github+json',
                                                      'User-Agent': 'Tether-updater'}), timeout=10) as response:
        data = json.load(response)
    version(data['tag_name'])
    return data


def read_cache(file):
    try:
        data = json.loads(file.read_text())
        if not isinstance(data, dict) or not isinstance(data.get('checked_at'), (int, float)):
            return {}
        if data.get('latest'):
            version(data['latest'])
        return data
    except (OSError, ValueError, TypeError):
        return {}


def refresh(file, cached):
    global _RUNNING
    try:
        data = dict(cached, checked_at=time.time())
        try:
            release = fetch_release()
            url = release.get('html_url', '')
            data.update(latest=release['tag_name'], url=url if str(url).startswith('https://') else '',
                        notes=str(release.get('body') or ''))
        except Exception:
            pass  # Cache failed attempts too, without hiding a previously known release.
        file.parent.mkdir(parents=True, exist_ok=True)
        temp = file.with_suffix('.new')
        temp.write_text(json.dumps(data))
        temp.replace(file)
    except (OSError, ValueError):
        pass
    finally:
        with _LOCK:
            _RUNNING = False


def payload():
    global _RUNNING
    file = paths.CACHE / 'updates.json'
    cached = read_cache(file)
    setting = mode()
    now = time.time()
    with _LOCK:
        checked = max(cached.get('checked_at', 0), _ATTEMPTED.get(str(file), 0))
        if setting != 'off' and not _RUNNING and now - checked >= DAY:
            _RUNNING = True
            _ATTEMPTED[str(file)] = now
            threading.Thread(target=refresh, args=(file, cached), daemon=True).start()
        checking = setting != 'off' and _RUNNING
    current = current_version()
    latest = cached.get('latest')
    return dict(current=current, latest=latest, available=bool(latest and version(latest) > version(current)),
                url=cached.get('url', ''), notes=cached.get('notes', ''),
                checked_at=cached.get('checked_at'), mode=setting, checking=checking,
                can_update=can_update(), run=read_run())
