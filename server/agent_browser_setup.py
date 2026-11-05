"""Safe, local agent setup and read-only checks for agents with an old browser environment."""
import copy
from collections import deque
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

import paths
import procinfo

try:
    import tomllib
except ImportError:
    tomllib = None

VARIABLE = 'PLAYWRIGHT_MCP_CDP_ENDPOINT'
CLAUDE_REASON = 'Claude settings need a manual change.'
CODEX_REASON = 'Codex settings need a manual change.'
_STALE_CACHE = {}


def claude_settings_path():
    return Path(paths.CLAUDE) / 'settings.json'


def codex_config_path():
    return Path(os.environ.get('CODEX_HOME') or paths.CODEX) / 'config.toml'


def playwright_server(server):
    """Match the stdio Playwright servers used by the project launcher."""
    return bool(server.get('command')) and any(
        'playwright' in str(value).lower() for value in [server['command'], *server.get('args', [])])


def has_browser_env(env_vars):
    return VARIABLE in env_vars or any(isinstance(value, dict) and value.get('name') == VARIABLE
                                      and value.get('source', 'local') == 'local' for value in env_vars)


def _invalid_json_constant(value):
    raise ValueError('Invalid JSON constant.')


def _claude_data(path):
    try:
        data = json.loads(path.read_bytes(), parse_constant=_invalid_json_constant)
    except FileNotFoundError:
        data = {}
    except (ValueError, UnicodeError):
        raise ValueError('Claude settings contain invalid JSON.') from None
    if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
        raise ValueError(CLAUDE_REASON)
    groups = data.get('hooks', {}).get('SessionStart', [])
    if not isinstance(groups, list) or any(not isinstance(group, dict)
            or not isinstance(group.get('hooks'), list)
            or any(not isinstance(hook, dict) for hook in group['hooks']) for group in groups):
        raise ValueError(CLAUDE_REASON)
    return data, groups


def _claude_ok(groups):
    for group in groups:
        for hook in group['hooks']:
            if hook.get('type') != 'command' or not isinstance(hook.get('command'), str):
                continue
            try:
                args = shlex.split(hook['command'])
            except ValueError:
                continue
            for index, arg in enumerate(args):
                if (index and args[index - 1] != sys.executable
                        and not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(args[index - 1]).name)):
                    continue
                if Path(arg).parts[-2:] == ('server', 'claude_hooks.py'):
                    return True
                if Path(arg).parts[-2:] == ('server', 'agent_browser_cli.py') and args[index + 1:index + 2] == ['hook']:
                    return True
    return False


def _syntax(text):
    """Yield TOML punctuation outside strings and comments, with its original text offset."""
    index = 0
    quote = None
    while index < len(text):
        char = text[index]
        if quote:
            if quote[0] == '"' and char == '\\':
                index += 2
                continue
            if text.startswith(quote, index):
                index += len(quote)
                if len(quote) == 3:
                    while index < len(text) and text[index] == quote[0]:
                        index += 1  # A multiline string may end with four or five quotes.
                quote = None
                continue
        elif char == '#':
            end = text.find('\n', index)
            index = len(text) if end < 0 else end
            continue
        elif char in "\"'":
            quote = char * 3 if text.startswith(char * 3, index) else char
            index += len(quote)
            continue
        else:
            yield index, char
        index += 1


def _statements(text):
    start, depth = 0, 0
    for index, char in _syntax(text):
        if char in '[{':
            depth += 1
        elif char in ']}':
            depth -= 1
        elif char == '\n' and depth == 0:
            yield start, index + 1, text[start:index + 1]
            start = index + 1
    if start < len(text):
        yield start, len(text), text[start:]


def _server_header(statement):
    if not statement.lstrip().startswith('['):
        return False, None
    # Let tomllib decode quoted names and dots; array tables and subtables are not editable server tables.
    try:
        parsed = tomllib.loads(statement + '\n__tether_setup__ = true\n').get('mcp_servers', {})
        if isinstance(parsed, dict) and len(parsed) == 1:
            name, value = next(iter(parsed.items()))
            if value == {'__tether_setup__': True}:
                return True, name
    except ValueError:
        pass
    return True, None


def _codex_edit(text):
    """Plan edits, then prove that their parsed effect is only the requested env additions."""
    if tomllib is None:
        raise ValueError('Codex setup requires Python 3.11 or newer.')
    try:
        old = tomllib.loads(text)
    except ValueError:
        raise ValueError('Codex settings contain invalid TOML.') from None
    try:
        servers = {name: server for name, server in old.get('mcp_servers', {}).items() if playwright_server(server)}
        if any(not isinstance(server.get('env_vars', []), list) for server in servers.values()):
            raise ValueError(CODEX_REASON)
        missing = {name: server for name, server in servers.items() if not has_browser_env(server.get('env_vars', []))}
    except (AttributeError, TypeError):
        raise ValueError(CODEX_REASON) from None
    if not servers:
        return 'none', text
    if not missing:
        return 'ok', text
    tables = {}
    current = None
    for start, end, statement in _statements(text):
        header, name = _server_header(statement)
        if header:
            current = name
            if name in missing:
                tables[name] = {'end': end, 'env': None}
        elif current in missing:
            syntax = list(_syntax(statement))
            equal = next((index for index, char in syntax if char == '='), None)
            if equal is not None:
                tables[current]['end'] = end
                key = statement[:equal].strip()
                if key in ('env_vars', '"env_vars"', "'env_vars'"):
                    tables[current]['env'] = (start, statement, equal)
    edits = []
    expected = copy.deepcopy(old)
    newline = '\r\n' if '\r\n' in text else '\n'
    for name, server in missing.items():
        table = tables.get(name)
        if table is None or not isinstance(server.get('env_vars', []), list):
            raise ValueError(CODEX_REASON)
        env = table['env']
        if 'env_vars' not in server:
            offset = table['end']
            prefix = newline if offset and text[offset - 1] != '\n' else ''
            edits.append((offset, prefix + f'env_vars = ["{VARIABLE}"]' + newline))
        elif env is not None:
            start, statement, equal = env
            if '\n' in statement.rstrip('\r\n') or '\r' in statement.rstrip('\r\n'):
                raise ValueError(CODEX_REASON)
            tokens = [(index, char) for index, char in _syntax(statement[equal + 1:]) if not char.isspace()]
            if not tokens or tokens[0][1] != '[' or tokens[-1][1] != ']':
                raise ValueError(CODEX_REASON)
            closing = start + equal + 1 + tokens[-1][0]
            suffix = ', ' if server['env_vars'] and tokens[-2][1] != ',' else (' ' if server['env_vars'] else '')
            edits.append((closing, suffix + f'"{VARIABLE}"'))
        else:
            raise ValueError(CODEX_REASON)
        expected['mcp_servers'][name]['env_vars'] = [*server.get('env_vars', []), VARIABLE]
    edited = text
    for offset, addition in sorted(edits, reverse=True):
        edited = edited[:offset] + addition + edited[offset:]
    try:
        valid = tomllib.loads(edited) == expected
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Codex settings could not be changed safely.')
    return 'missing', edited


def status(claude_path=None, codex_path=None):
    claude_path = Path(claude_path) if claude_path is not None else claude_settings_path()
    codex_path = Path(codex_path) if codex_path is not None else codex_config_path()
    result = {}
    try:
        if not claude_path.parent.exists():
            result['claude'] = 'none'
        else:
            _, groups = _claude_data(claude_path)
            result['claude'] = 'ok' if _claude_ok(groups) else 'missing'
    except (OSError, ValueError) as exc:
        result.update(claude='manual', claude_reason=str(exc) if isinstance(exc, ValueError) else CLAUDE_REASON)
    try:
        try:
            text = codex_path.read_bytes().decode('utf-8')
        except FileNotFoundError:
            result['codex'] = 'none'
        else:
            result['codex'] = _codex_edit(text)[0]
    except (OSError, ValueError) as exc:
        reason = CODEX_REASON if isinstance(exc, (OSError, UnicodeError)) else str(exc)
        result.update(codex='manual', codex_reason=reason)
    return result


def _write(path, content):
    """Back up an existing file once and atomically replace it without changing its mode."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        original = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        original, mode = None, 0o600
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
        if original is not None:
            try:
                backup_fd = os.open(str(path) + '.tether-bak', os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            except FileExistsError:
                pass
            else:
                with os.fdopen(backup_fd, 'wb') as backup:
                    os.fchmod(backup.fileno(), mode)
                    backup.write(original)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def fix_claude(path=None):
    path = Path(path) if path is not None else claude_settings_path()
    data, groups = _claude_data(path)
    if _claude_ok(groups):
        return
    command = shlex.join([sys.executable, str(Path(__file__).resolve().with_name('agent_browser_cli.py')), 'hook'])
    data.setdefault('hooks', {})['SessionStart'] = [*groups, {'hooks': [
        {'type': 'command', 'command': command, 'timeout': 10}]}]
    _write(path, (json.dumps(data, indent=2) + '\n').encode('utf-8'))


def fix_codex(path=None):
    path = Path(path) if path is not None else codex_config_path()
    try:
        text = path.read_bytes().decode('utf-8')
    except FileNotFoundError:
        return
    state, edited = _codex_edit(text)
    if state == 'missing':
        _write(path, edited.encode('utf-8'))


def _read_text(path):
    return procinfo.read_text(path)


def _read_bytes(path):
    return procinfo.read_bytes(path)


def _uid_of(path):
    return procinfo.uid_of(path)


def _pane_list(timeout):
    return subprocess.check_output([paths.TMUX, 'list-panes', '-a', '-F', '#{pane_id} #{pane_pid}'],
                                   text=True, timeout=timeout)


def _darwin_children(uid, deadline, clock):
    children, owned = {}, set()
    for pid in procinfo.pids():
        if clock() >= deadline:
            break
        try:
            if procinfo.uid(pid) != uid or clock() >= deadline:
                continue
            parent = procinfo.parent(pid)
            owned.add(pid)
            children.setdefault(parent, []).append(pid)
        except OSError:
            continue
    return children, owned


def _darwin_stale_panes(endpoint, pane_reader, clock, uid, cache):
    if not endpoint:
        return []
    uid = os.getuid() if uid is None else uid
    cache = _STALE_CACHE if cache is None else cache
    key = ('darwin', endpoint, pane_reader, clock, uid)
    now = clock()
    cached = cache.get(key)
    if cached and now < cached[0]:
        return list(cached[1])
    deadline, stale = now + 2, []
    try:
        panes = []
        for line in pane_reader(max(.01, deadline - clock())).splitlines():
            fields = line.split()
            if len(fields) == 2 and re.fullmatch(r'%[0-9]+', fields[0]) and fields[1].isdigit():
                panes.append((fields[0], int(fields[1])))
        children, owned = _darwin_children(uid, deadline, clock)
        for pane, root in panes:
            pending, visited = deque([root]), set()
            while pending and clock() < deadline:
                pid = pending.popleft()
                if pid in visited or pid not in owned:
                    continue
                visited.add(pid)
                try:
                    argv = procinfo.argv(pid)
                    if any(arg.rsplit(b'/', 1)[-1] in (b'claude', b'codex') for arg in argv[:2]):
                        if clock() >= deadline:
                            break
                        env = procinfo.environ(pid)
                        if env is not None and ('TETHER_CDP_URL=' + endpoint).encode() not in env:
                            stale.append(pane)
                        break  # The top-most agent owns the pane, even when its environment is hidden.
                except OSError:
                    continue
                pending.extend(children.get(pid, []))
    except (OSError, subprocess.SubprocessError):
        pass
    cache.clear()
    cache[key] = (clock() + 10, tuple(stale))
    return stale


def stale_panes(endpoint, *, read_text=_read_text, read_bytes=_read_bytes, list_dir=procinfo.list_dir,
                uid_of=_uid_of, pane_reader=_pane_list, clock=time.monotonic, uid=None, cache=None):
    """Read each process tree once, within two seconds; cache a result for ten seconds."""
    if procinfo.DARWIN:
        return _darwin_stale_panes(endpoint, pane_reader, clock, uid, cache)
    if not endpoint:
        return []
    uid = os.getuid() if uid is None else uid
    cache = _STALE_CACHE if cache is None else cache
    key = (endpoint, read_text, read_bytes, list_dir, uid_of, pane_reader, clock, uid)
    now = clock()
    cached = cache.get(key)
    if cached and now < cached[0]:
        return list(cached[1])
    deadline = now + 2
    stale = []
    try:
        panes = []
        for line in pane_reader(max(.01, deadline - clock())).splitlines():
            fields = line.split()
            if len(fields) == 2 and re.fullmatch(r'%[0-9]+', fields[0]) and fields[1].isdigit():
                panes.append((fields[0], int(fields[1])))
        children, owned = {}, set()
        for name in list_dir(procinfo.ROOT):
            if clock() >= deadline:
                break
            if not name.isdigit():
                continue
            folder = procinfo.ROOT + '/' + name
            try:
                if uid_of(folder) != uid or clock() >= deadline:
                    continue
                parent = int(read_text(folder + '/stat').rsplit(')', 1)[1].split()[1])
                owned.add(int(name))
                children.setdefault(parent, []).append(int(name))
            except (OSError, ValueError, IndexError):
                continue
        for pane, root in panes:
            pending, visited = deque([root]), set()
            while pending and clock() < deadline:
                pid = pending.popleft()
                if pid in visited or pid not in owned:
                    continue
                visited.add(pid)
                try:
                    argv = read_bytes(procinfo.ROOT + '/%d/cmdline' % pid).split(b'\0')
                    agent = any(arg.rsplit(b'/', 1)[-1] in (b'claude', b'codex') for arg in argv[:2])
                    if agent:
                        if clock() >= deadline:
                            break
                        env = read_bytes(procinfo.ROOT + '/%d/environ' % pid).split(b'\0')
                        if ('TETHER_CDP_URL=' + endpoint).encode() not in env:
                            stale.append(pane)
                        break  # Only the top-most match determines this pane's starting environment.
                except OSError:
                    continue
                pending.extend(children.get(pid, []))
    except (OSError, subprocess.SubprocessError):
        pass
    cache.clear()
    cache[key] = (clock() + 10, tuple(stale))
    return stale
