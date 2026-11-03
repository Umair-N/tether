"""Quiet, daily release discovery. Requests only read cache and start a worker."""
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.request import Request, urlopen

import paths

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400
_LOCK = threading.Lock()
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
                checked_at=cached.get('checked_at'), mode=setting, checking=checking)
