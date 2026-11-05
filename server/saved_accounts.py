"""Saved Claude and Codex accounts from the ccswap CLI: email, plan and usage only. ccswap owns the credentials; this module never reads them."""
import json
import os
import shutil
import subprocess
import tempfile
import time

import paths

AGENTS = ('claude', 'codex')
_snapshot = None


def switched_at(agent) -> float:
    try:
        return float(json.loads((paths.CACHE / 'account_switch.json').read_text()).get(agent, 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def snapshot():
    """Passive reads never start ccswap: even `list` can rotate Claude credentials."""
    return _snapshot or {'available': bool(binary()), 'claude': [], 'codex': []}


def binary():
    found = os.environ.get('MOBILE_TMUX_CCSWAP') or shutil.which('ccswap')
    fallback = paths.HOME / '.local/bin/ccswap'
    return found or (str(fallback) if fallback.exists() else None)


def window(raw, label):
    if not isinstance(raw, dict) or not isinstance(raw.get('pct'), (int, float)):
        return None
    return {'label': label, 'percent': round(float(raw['pct']), 1), 'resets_at': raw.get('resetsAt') if isinstance(raw.get('resetsAt'), str) else None}


def claude_account(raw):
    usage = raw.get('usage') if isinstance(raw.get('usage'), dict) else {}
    windows = [w for w in (window(usage.get('fiveHour'), '5h'), window(usage.get('sevenDay'), '7d')) if w]
    return {'number': raw['number'], 'email': raw.get('email'), 'org': raw.get('organizationName'), 'active': bool(raw.get('active')),
            'status': raw.get('usageStatus'), 'windows': windows}


def codex_account(raw):
    return {'number': raw['number'], 'email': raw.get('email'), 'plan': raw.get('planType'), 'active': bool(raw.get('active')),
            'disabled': bool(raw.get('disabled')), 'windows': []}


def listing(run=subprocess.run):
    """{'available': bool, 'claude': [...], 'codex': [...]}. Missing ccswap is not an error: the sheet then hides the list."""
    global _snapshot
    tool = binary()
    if not tool:
        return {'available': False, 'claude': [], 'codex': []}
    result = run([tool, 'list', '--json'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise RuntimeError('ccswap list failed. Run "ccswap list" on the server to see why.')
    data = json.loads(result.stdout)
    out = {'available': True}
    for agent, shape in (('claude', claude_account), ('codex', codex_account)):
        accounts = ((data.get(agent) or {}).get('accounts')) or []
        out[agent] = [shape(a) for a in accounts if isinstance(a, dict) and isinstance(a.get('number'), int)]
    _snapshot = out
    return out


def switch(agent, number, run=subprocess.run):
    """Make saved account `number` the active one for every new and running Claude session (Codex sessions need a restart)."""
    if agent not in AGENTS:
        raise ValueError('Select Claude or Codex.')
    if not isinstance(number, int) or isinstance(number, bool) or not 0 < number < 100:
        raise ValueError('Select a saved account.')
    tool = binary()
    if not tool:
        raise ValueError('ccswap is not installed on the server.')
    if not any(a['number'] == number for a in listing(run)[agent]):
        raise ValueError('That account is no longer saved. Reload the list.')
    command = [tool, *(['codex'] if agent == 'codex' else []), 'switch', str(number)]
    result = run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        detail = next((line.strip() for line in reversed((result.stderr or result.stdout or '').splitlines()) if line.strip()), '')
        raise ValueError(f'Could not switch: {detail[:200]}' if detail else 'Could not switch. Run "ccswap list" on the server to see why.')
    stamps = {name: switched_at(name) for name in AGENTS}
    stamps[agent] = time.time()
    paths.CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=paths.CACHE, delete=False) as temporary:
        os.fchmod(temporary.fileno(), 0o600)
        json.dump(stamps, temporary)
    try:
        os.replace(temporary.name, paths.CACHE / 'account_switch.json')
    finally:
        if os.path.exists(temporary.name):
            os.unlink(temporary.name)
    if agent == 'claude':
        import claude_probe
        claude_probe._usage.update(at=0, report=None, error=None)
    else:
        import live_usage
        live_usage._cache.clear()
    return listing(run)
