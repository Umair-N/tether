"""What the chat box can suggest: slash commands and skills for "/", files for "@", and the MCP servers and plugins in reach.
Read-only. Everything comes from the agents' own config folders; nothing here runs an agent."""
import json
import os
from pathlib import Path
import re
import subprocess
import time
try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

import claude_probe
import paths

HOME = Path.home()
# Every documented Claude Code command, with aliases. Made by update_claude_commands.py from the official reference.
COMMANDS_FILE = Path(__file__).with_name('claude_commands.json')


def claude_builtins():
    try:
        commands = json.loads(COMMANDS_FILE.read_text())['commands']
    except (OSError, ValueError, KeyError):
        return []
    return [{'name': item['name'], 'description': short(item['description']), 'aliases': item.get('aliases') or [], 'kind': item.get('kind', 'builtin')}
            for item in commands]


CODEX_BUILTINS = [
    ('new', 'Start a new conversation'), ('compact', 'Summarize the conversation to free context'),
    ('status', 'Show model, limits and token use'), ('model', 'Choose the model and effort'),
    ('approvals', 'Choose what Codex may do without asking'), ('review', 'Review the current changes'),
    ('diff', 'Show the git diff'), ('mention', 'Mention a file'), ('init', 'Write an AGENTS.md for this project'),
    ('mcp', 'List MCP tools'), ('resume', 'Resume an earlier conversation'), ('undo', 'Undo the last turn'),
]
FRONT = re.compile(r'^---\s*\n(.*?)\n---', re.S)


def front_matter(path):
    """name and description from a SKILL.md or command file; tolerant of missing or odd front matter."""
    try:
        text = path.read_text(errors='ignore')[:4000]
    except OSError:
        return {}
    match = FRONT.match(text)
    result = {}
    for line in (match[1] if match else '').splitlines():
        key, _, value = line.partition(':')
        if key.strip() in ('name', 'description') and value.strip() and value.strip() not in ('>', '|', '>-', '|-'):
            result[key.strip()] = value.strip().strip('"\'')
    return result


def short(text, limit=140):
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def skills_in(folder, prefix=''):
    items = []
    for skill in sorted(folder.glob('*/SKILL.md')) if folder.is_dir() else []:
        meta = front_matter(skill)
        items.append({'name': prefix + (meta.get('name') or skill.parent.name), 'description': short(meta.get('description')), 'kind': 'skill'})
    return items


def commands_in(folder, prefix=''):
    items = []
    for command in sorted(folder.glob('*.md')) if folder.is_dir() else []:
        items.append({'name': prefix + command.stem, 'description': short(front_matter(command).get('description')), 'kind': 'command'})
    return items


def transcript_skills(transcript):
    """Skills the CLI announced to the model in this conversation ("skill_listing" entries): bundled, claude.ai and plugin skills.
    The file only grows, so each call reads just the new bytes."""
    if not transcript:
        return {}
    try:
        size = os.path.getsize(transcript)
    except OSError:
        return {}
    offset, found = _listings.get(transcript, (0, {}))
    if size < offset:
        offset, found = 0, {}
    if size > offset:
        with open(transcript, 'rb') as handle:
            handle.seek(offset)
            data = handle.read(size - offset)
        end = data.rfind(b'\n') + 1  # leave a half-written last line for the next call
        for line in data[:end].splitlines():
            if b'"skill_listing"' not in line:
                continue
            try:
                entry = json.loads(line).get('attachment') or {}
            except ValueError:
                continue
            if entry.get('type') != 'skill_listing':
                continue
            if entry.get('isInitial'):
                found = {}
            texts = dict(LISTING_LINE.findall(entry.get('content') or ''))
            for name in entry.get('names') or texts:
                found[name] = short(texts.get(name, found.get(name, '')))
        _listings[transcript] = (offset + end, found)
    return found


LISTING_LINE = re.compile(r'^- ([^:\s][^:\n]*(?::[^:\s][^:\n]*)?): ?(.*)$', re.M)
_listings = {}  # transcript -> (bytes read, {name: description})


def plugin_folder(key):
    """The newest cached copy of an enabled plugin ("name@marketplace")."""
    name, _, market = key.partition('@')
    versions = [path for path in (paths.CLAUDE / 'plugins' / 'cache' / market / name).glob('*') if path.is_dir()]
    return max(versions, key=lambda path: path.stat().st_mtime) if versions else None


def json_file(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def plugin_name(folder, key):
    """The prefix the CLI uses: the plugin's own manifest name, which can differ from the settings key (atlassian-mcp -> atlassian)."""
    return json_file(folder / '.claude-plugin' / 'plugin.json').get('name') or key.partition('@')[0]


def synced_plugins():
    """Plugins synced from claude.ai for the signed-in account: plugins/synced/<organization>_<account>/<plugin>/."""
    account = json_file(HOME / '.claude.json').get('oauthAccount') or {}
    if not account.get('organizationUuid') or not account.get('accountUuid'):
        return []
    root = paths.CLAUDE / 'plugins' / 'synced' / ('%s_%s' % (account['organizationUuid'], account['accountUuid']))
    return sorted(folder for folder in root.glob('*') if (folder / '.claude-plugin' / 'plugin.json').is_file())


# Commands that only open a panel in the terminal. Claude's SDK list leaves them out; Tether shows its own sheet for some.
TERMINAL = 'Opens in the terminal. '


def merged_commands(probe):
    """Claude's own list (exact for this folder), plus the documented terminal-only commands it leaves out, with aliases."""
    docs = {item['name']: item for item in claude_builtins()}
    items, seen = [], set()
    for command in probe['commands']:
        doc = docs.get(command['name'])
        text = re.sub(r'\s*\((user|project|bundled|plugin[^)]*|managed)\)$', '', command['description'])
        items.append({'name': command['name'], 'description': short(text or (doc or {}).get('description')),
                      'aliases': (doc or {}).get('aliases', []), 'hint': command.get('hint', ''),
                      'kind': (doc or {}).get('kind', 'skill') if doc else 'skill'})
        seen.add(command['name'])
    for name, doc in docs.items():
        if name not in seen and doc['kind'] == 'builtin':
            items.append(dict(doc, description=short(TERMINAL + doc['description'])))
    return items


def claude_catalog(cwd, transcript=None, probe=True):
    """probe=False skips asking Claude (the disk-only list)."""
    base = paths.CLAUDE  # ~/.claude
    found = claude_probe.commands(cwd) if probe else None
    if found and found['commands']:
        result = claude_catalog(cwd, transcript, probe=False)
        result['commands'] = merged_commands(found)
        # A pane started under another account or before a change keeps the skills it loaded then.
        seen = {item['name'] for item in result['commands']}
        result['commands'] += [{'name': name, 'description': text, 'kind': 'skill'} for name, text in transcript_skills(transcript).items() if name not in seen]
        result['account'] = found['account']
        return result
    items = claude_builtins()
    items += commands_in(base / 'commands') + skills_in(base / 'skills')
    project = Path(cwd) if cwd else None
    if project:
        items += commands_in(project / '.claude' / 'commands') + skills_in(project / '.claude' / 'skills')
    settings = json_file(base / 'settings.json')
    plugins, mcp = [], []
    for key, enabled in (settings.get('enabledPlugins') or {}).items():
        folder = plugin_folder(key) if enabled else None
        name = plugin_name(folder, key) if folder else key.partition('@')[0]
        plugins.append({'name': name, 'source': key.partition('@')[2], 'enabled': bool(enabled)})
        if folder:
            items += commands_in(folder / 'commands', name + ':') + skills_in(folder / 'skills', name + ':')
            servers = json_file(folder / '.mcp.json')
            mcp += [{'name': server, 'source': 'plugin ' + name} for server in servers.get('mcpServers', servers)]
    for folder in synced_plugins():
        name = plugin_name(folder, folder.name)
        items += commands_in(folder / 'commands', name + ':') + skills_in(folder / 'skills', name + ':')
    seen = {item['name'] for item in items}
    items += [{'name': name, 'description': text, 'kind': 'skill'} for name, text in transcript_skills(transcript).items() if name not in seen]
    config = json_file(HOME / '.claude.json')
    mcp += [{'name': server, 'source': 'user'} for server in config.get('mcpServers') or {}]
    if project:
        mcp += [{'name': server, 'source': 'project'} for server in ((config.get('projects') or {}).get(str(project)) or {}).get('mcpServers') or {}]
        mcp += [{'name': server, 'source': '.mcp.json'} for server in json_file(project / '.mcp.json').get('mcpServers') or {}]
    return {'commands': items, 'plugins': plugins, 'mcp': mcp,
            'note': 'Connectors added on claude.ai do not show here. Run /mcp to see every server.'}


def codex_catalog(cwd):
    items = [{'name': name, 'description': text, 'kind': 'builtin'} for name, text in CODEX_BUILTINS]
    items += commands_in(paths.CODEX / 'prompts', 'prompts:')
    skills = skills_in(paths.CODEX / 'skills') + (skills_in(Path(cwd) / '.codex' / 'skills') if cwd else [])
    items += [dict(skill, name='$' + skill['name']) for skill in skills]  # Codex calls a skill with $name
    mcp = []
    if tomllib:
        try:
            config = tomllib.loads((paths.CODEX / 'config.toml').read_text())
        except (OSError, ValueError):
            config = {}
        mcp = [{'name': server, 'source': 'config.toml'} for server in config.get('mcp_servers') or {}]
    return {'commands': items, 'plugins': [], 'mcp': mcp, 'note': ''}


def catalog(agent, cwd, transcript=None):
    if agent == 'claude':
        return claude_catalog(cwd, transcript)
    if agent == 'codex':
        return codex_catalog(cwd)
    return {'commands': [], 'plugins': [], 'mcp': [], 'note': ''}


SKIP = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build', '.next', '.cache', 'target'}
_files = {}  # folder -> (time, list)


def list_files(cwd, cap=20000):
    cached = _files.get(cwd)
    if cached and time.time() - cached[0] < 30:
        return cached[1]
    try:
        result = subprocess.run(['git', '-C', cwd, 'ls-files', '--cached', '--others', '--exclude-standard'],
                                capture_output=True, text=True, timeout=4)
        files = result.stdout.splitlines()[:cap] if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        files = None
    if files is None:
        files = []
        for root, folders, names in os.walk(cwd):
            folders[:] = [name for name in folders if name not in SKIP and not name.startswith('.')]
            if root[len(cwd):].count(os.sep) >= 4:
                folders[:] = []
            files += [os.path.relpath(os.path.join(root, name), cwd) for name in names]
            if len(files) >= cap:
                break
    folders = sorted({str(Path(name).parent) + '/' for name in files if '/' in name})
    entries = folders + files
    _files[cwd] = (time.time(), entries)
    return entries


def find_files(cwd, query, limit=30):
    """Paths under the pane's folder that contain the query, best match first: name start, then name, then path."""
    if not cwd or not os.path.isdir(cwd):
        return []
    query = query.lower().lstrip('@')
    scored = []
    for entry in list_files(cwd):
        lower = entry.lower()
        if query and query not in lower:
            continue
        name = lower.rstrip('/').rsplit('/', 1)[-1]
        rank = 0 if name.startswith(query) else 1 if query in name else 2
        scored.append((rank, entry.count('/'), len(entry), entry))
    return [entry for *_, entry in sorted(scored)[:limit]]
