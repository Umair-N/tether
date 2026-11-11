"""Read Claude subagents only from the selected conversation's transcript folder."""
from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime, timezone
import json
from html import unescape
from pathlib import Path
import re
import threading
import time

import tool_details

TAIL_BYTES = 256 * 1024
CACHE_LIMIT = 200
_cache = OrderedDict()
_lock = threading.Lock()
_UNAVAILABLE = 'That agent is no longer available.'


def _cached(path, reader, *, incremental=False):
    info = path.stat()
    key = str(path)
    stamp = (info.st_mtime_ns, info.st_size)
    with _lock:
        previous = _cache.get(key)
        if previous is None or previous[0] != stamp:
            value = reader(path, previous) if incremental else reader(path)
            _cache[key] = (stamp, value)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_LIMIT:
            _cache.popitem(last=False)
        return _cache[key][1], info


def _records(lines):
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if isinstance(record, dict) and isinstance(record.get('type'), str):
            if record['type'] in ('user', 'assistant') and not isinstance(record.get('message'), dict):
                continue
            yield record


def _tokens(message):
    usage = message.get('usage')
    if not isinstance(usage, dict):
        return None
    counts = [usage.get(key, 0) for key in (
        'input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')]
    return sum(counts) if all(isinstance(value, int) and not isinstance(value, bool) for value in counts) else None


def _main(path, previous=None):
    result = {'model': None, 'context': None, 'title': None, '_notifications': {}}
    with path.open('rb') as stream:
        size = stream.seek(0, 2)
        offset = max(0, size - TAIL_BYTES)
        stream.seek(offset)
        tail = stream.read(TAIL_BYTES)
        if offset:
            tail = tail.partition(b'\n')[2]
        for record in _records(tail.splitlines()):
            message = record.get('message')
            if record.get('type') == 'assistant' and isinstance(message, dict) and isinstance(message.get('usage'), dict):
                result.update(model=message.get('model'), context=_tokens(message))
        # Titles and notifications can precede the tail. Scan once, then resume at
        # the last complete line when the transcript grows (including split writes).
        start = 0
        if previous is not None and size > previous[0][1]:
            old = previous[1]
            start = old['_offset']
            result.update(title=old['title'], _notifications=dict(old['_notifications']))
        stream.seek(start)
        while True:
            result['_offset'] = stream.tell()
            line = stream.readline()
            if not line:
                break
            for record in _records([line]):
                field = {'custom-title': 'customTitle', 'agent-name': 'agentName'}.get(record['type'])
                title = record.get(field) if field else None
                if isinstance(title, str) and title.strip():
                    result['title'] = title.strip()
                if record['type'] == 'user':
                    _notifications(_text(record['message'].get('content')), result['_notifications'])
            if not line.endswith(b'\n'):
                break
    return result


def _notifications(text, found):
    # Some producers put a JSON string inside the already decoded content.
    for _ in range(2):
        try:
            decoded = json.loads(text)
        except (ValueError, RecursionError):
            break
        if not isinstance(decoded, str):
            break
        text = decoded
    for match in re.finditer(r'<task-notification>(.*?)</task-notification>', text, re.S):
        body = match[1]
        identifier = re.search(r'<task-id>(.*?)</task-id>', body, re.S)
        status = re.search(r'<status>(completed|failed|killed)</status>', body)
        summary = re.search(r'<summary>(.*?)</summary>', body, re.S)
        if identifier and status:
            found[identifier[1].strip()] = (status[1], unescape(summary[1]).strip()[:240] if summary else None)


def _meta(path):
    data = json.loads(path.read_bytes())
    return data if isinstance(data, dict) else {}


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(part['text'] for part in content if isinstance(part, dict)
                         and part.get('type') == 'text' and isinstance(part.get('text'), str))
    return ''


def _parse(path):
    data = {'model': None, 'tokens': None, 'started': None, 'ended': None,
            'tools': 0, 'prompt': None, 'report': None, 'steps_cut': False, 'tool_ids': set()}
    steps, calls = deque(), {}
    seen = False
    with path.open('rb') as stream:
        for record in _records(stream):
            if not seen:
                data['started'] = record.get('timestamp')
                seen = True
            data['ended'] = record.get('timestamp')
            message = record.get('message')
            if not isinstance(message, dict):
                continue
            content = message.get('content')
            role = record.get('type')
            if role == 'user' and data['prompt'] is None:
                data['prompt'] = tool_details.bounded(_text(content), max_bytes=16384)[0]
            if role == 'assistant':
                data['model'] = message.get('model')
                if isinstance(message.get('usage'), dict):
                    data['tokens'] = _tokens(message)
            if isinstance(content, str) and role == 'assistant':
                content = [{'type': 'text', 'text': content}]
            if not isinstance(content, list) or role not in ('assistant', 'user'):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                step = None
                if role == 'assistant' and part.get('type') == 'text' and isinstance(part.get('text'), str):
                    data['report'] = tool_details.bounded(part['text'], max_bytes=16384)[0]
                    step = {'kind': 'text', 'text': tool_details.bounded(part['text'], max_bytes=4000)[0]}
                elif role == 'assistant' and part.get('type') == 'tool_use':
                    data['tools'] += 1
                    if isinstance(part.get('id'), str):
                        data['tool_ids'].add(part['id'])
                    summary = tool_details.summary(part)
                    step = dict(kind='tool', **{key: summary[key] for key in ('id', 'name', 'title', 'is_error', 'status')})
                    calls[step['id']] = step
                elif part.get('type') == 'tool_result':
                    identifier = part.get('tool_use_id')
                    if isinstance(identifier, str) and identifier in calls:
                        calls[identifier].update(status='complete', is_error=tool_details.failed(part))
                if step is not None:
                    steps.append(step)
                    if len(steps) > 400:
                        dropped = steps.popleft()
                        if dropped.get('kind') == 'tool' and calls.get(dropped['id']) is dropped:
                            del calls[dropped['id']]
                        data['steps_cut'] = True
    if not seen:
        raise ValueError(_UNAVAILABLE)
    return dict(data, prompt=data['prompt'] or '', steps=list(steps))


def _folder(config):
    if config.get('agent') != 'claude' or not config.get('transcript'):
        return None
    return (Path(config['transcript']).with_suffix('') / 'subagents').resolve()


def _path(config, agent_id):
    if not isinstance(agent_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', agent_id):
        raise ValueError(_UNAVAILABLE)
    try:
        folder = _folder(config)
        if folder is not None:
            path = (folder / f'agent-{agent_id}.jsonl').resolve()
            if path.is_relative_to(folder) and path.is_file():
                return path
    except (OSError, RuntimeError, ValueError):
        pass
    raise ValueError(_UNAVAILABLE)


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _entry(config, agent_id, now, notifications):
    path = _path(config, agent_id)
    data, info = _cached(path, _parse)
    meta = {}
    meta_path = _folder(config) / f'agent-{agent_id}.meta.json'
    try:
        if meta_path.resolve().is_relative_to(_folder(config)):
            meta, _ = _cached(meta_path, _meta)
    except (OSError, ValueError, RecursionError, RuntimeError):
        pass
    status, summary = notifications.get(agent_id, (None, None))
    if meta.get('stoppedByUser') or status == 'killed':
        state = 'stopped'
    elif status == 'failed':
        state = 'failed'
    elif meta.get('finished') or status == 'completed':
        state = 'finished'
    else:
        state = 'running' if now - info.st_mtime <= 15 * 60 else 'stopped'
    start = _stamp(data['started'])
    end = now if state == 'running' else _stamp(data['ended'])
    entry = {'id': agent_id, 'description': meta.get('description'), 'type': meta.get('agentType'),
             'model': data['model'] or meta.get('model') or meta.get('pickedEffortModel'),
             'effort': meta.get('effort'), 'background': meta.get('requestShape') == 'background',
             'state': state, 'started': data['started'], 'ended': None if state == 'running' else data['ended'],
             'seconds': int(end - start) if start is not None and end is not None else None,
             'tokens': data['tokens'], 'tools': data['tools'], 'depth': meta.get('spawnDepth', 1),
             'parent': None, 'summary': summary if state in ('failed', 'stopped') else None}
    return entry, data, meta


def _listing(config, *, limit=True):
    result = {'main': {'model': None, 'context': None, 'title': None}, 'agents': []}
    try:
        folder = _folder(config)
        if folder is None or not folder.is_dir():
            return result
        files = list(folder.glob('agent-*.jsonl'))
    except (OSError, ValueError, RuntimeError):
        return result
    notifications = {}
    try:
        main, _ = _cached(Path(config['transcript']), _main, incremental=True)
        result['main'] = {key: main[key] for key in ('model', 'context', 'title')}
        notifications = main['_notifications']
    except (OSError, ValueError):
        pass
    now = time.time()
    owners, spawned = {}, {}
    for path in files:
        try:
            entry, data, meta = _entry(config, path.stem[len('agent-'):], now, notifications)
        except (OSError, ValueError, UnicodeError, RecursionError):
            continue
        result['agents'].append(entry)
        for tool_id in data['tool_ids']:
            owners[tool_id] = entry['id']
        if isinstance(meta.get('toolUseId'), str):
            spawned[entry['id']] = meta['toolUseId']
    for entry in result['agents']:
        parent = owners.get(spawned.get(entry['id']))
        entry['parent'] = parent if parent != entry['id'] else None
    result['agents'].sort(key=lambda entry: (_stamp(entry['started']) is not None, _stamp(entry['started']) or 0))
    if limit:
        result['agents'] = result['agents'][-40:]
    return result


def listing(config):
    return _listing(config)


def detail(config, agent_id):
    try:
        path = _path(config, agent_id)
        entry = next((entry for entry in _listing(config, limit=False)['agents'] if entry['id'] == agent_id), None)
        if entry is None:
            raise ValueError(_UNAVAILABLE)
        data, _ = _cached(path, _parse)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(_UNAVAILABLE) from exc
    entry.update(prompt=data['prompt'], steps=deepcopy(data['steps']),
                 report=data['report'] if entry['state'] == 'finished' else None)
    if data['steps_cut']:
        entry['steps_cut'] = True
    return entry


def tool(config, agent_id, call_id):
    path = _path(config, agent_id)
    try:
        call, result = tool_details.find(dict(config, transcript=path), call_id, allow_sidechain=True)
    except OSError as exc:
        raise ValueError(_UNAVAILABLE) from exc
    return dict(tool_details.build(call, result), images=len(tool_details.images(result)))
