"""Read-only, pane-scoped Codex run details from live process fds and bounded session windows."""
import copy
import json
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import codex_level
import procinfo
from agents import descendants, iso_ms, tidy_user_text

PATH = '/mobile/api/codex-runs'
HEAD = 64 * 1024
TAIL = 256 * 1024
CACHE_SECONDS = 2
ENDED_SECONDS = 10 * 60
_panes = {}
_parsed = OrderedDict()
_lock = threading.RLock()
_COMMAND = re.compile(r'\bcmd\s*:\s*("(?:[^"\\]|\\.)*")')
# Summaries must not expose common inline credentials or authorization headers.
_SECRET = re.compile(
    r'''(?ix)(\b(?:[\w-]*(?:token|password|secret|api[_-]?key)|authorization)\b["']?\s*[:=]\s*)'''
    r'''(?:"[^"\n]*"|'[^'\n]*'|(?:(?:Bearer|Basic)\s+)?[^\s,;]+)''')
_BEARER = re.compile(r'(?i)\b(Bearer\s+)\S+')


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(part['text'] for part in value
                         if isinstance(part, dict) and isinstance(part.get('text'), str))
    return ''


def _short(text, limit, one_line=False):
    text = _BEARER.sub(r'\1[redacted]', _SECRET.sub(r'\1[redacted]', _text(text)))
    return (' '.join(text.split()) if one_line else text.strip())[:limit]


def _prompt(payload):
    text = tidy_user_text(_text(payload.get('content'))).strip()
    if text.startswith(('# AGENTS.md instructions', '<environment_context>', '<environment-context>')):
        return ''
    return _short(text, 400)


def _tool_text(payload):
    name = _text(payload.get('name')) or 'tool'
    leaf = name.rsplit('__', 1)[-1].rsplit('.', 1)[-1]
    raw = payload.get('arguments', payload.get('input', ''))
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        data = raw
    if leaf == 'apply_patch':
        patch = data.get('patch', data.get('input', '')) if isinstance(data, dict) else _text(data)
        names = re.findall(r'^\*\*\* (?:Update|Add|Delete) File: (.+)$|^\*\*\* Move to: (.+)$',
                           _text(patch), re.M)
        return 'Edit ' + ', '.join(a or b for a, b in names) if names else 'Edit files'
    summary = ''
    if isinstance(data, dict):
        command = data.get('cmd') or data.get('command')
        if isinstance(command, list):
            command = ' '.join(part for part in command if isinstance(part, str))
        if isinstance(command, str) and command.strip():
            summary = command.strip().splitlines()[0]
        else:
            summary = next((_text(data.get(key)) for key in
                            ('file_path', 'path', 'pattern', 'query', 'url', 'description') if data.get(key)), '')
    elif leaf == 'exec':
        # The exec tool wraps shell calls in JavaScript. Do not expose the script or its outputs.
        match = _COMMAND.search(_text(data))
        if match:
            try:
                summary = json.loads(match[1]).strip().split('\n')[0]
            except ValueError:
                summary = 'Script'
        else:
            summary = 'Script'
    elif isinstance(data, str) and data.strip():
        summary = data.strip().splitlines()[0]
    return name + (' ' + summary if summary else '')


def _records(data):
    for line in data.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(record, dict) and isinstance(record.get('payload'), dict):
            yield record


def _parse(path, force=False):
    """Read at most the head and tail; cache only compact fields, never raw transcript content."""
    try:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        cached = _parsed.get(str(path))
        if not force and cached and cached[0] == key:
            _parsed.move_to_end(str(path))
            return cached[1]
        with path.open('rb') as stream:
            head = stream.read(HEAD)
            offset = max(0, stat.st_size - TAIL)
            stream.seek(max(0, offset - 1))
            boundary = stream.read(1) if offset else b'\n'
            tail = stream.read(TAIL)
        if stat.st_size > HEAD:
            head = head.rsplit(b'\n', 1)[0]  # discard the cut head record
        if offset and boundary != b'\n':
            tail = tail.partition(b'\n')[2]  # discard the cut tail record
    except (OSError, ValueError):
        return None
    first = list(_records(head))
    meta = next((r['payload'] for r in first if r.get('type') == 'session_meta'), {})
    source = meta.get('source')
    if (not meta or isinstance(source, dict) and 'subagent' in source
            or meta.get('thread_source') == 'guardian_review'
            or not (source == 'exec' or meta.get('originator') == 'codex_exec')):
        value = None
    else:
        value = {'id': _text(meta.get('id')), 'started': iso_ms(meta.get('timestamp')),
                 'cwd': _text(meta.get('cwd')), 'prompt': '', 'steps': [],
                 'last_activity': 0, 'final': None, 'complete': False}
        for record in first:
            value['last_activity'] = max(value['last_activity'], iso_ms(record.get('timestamp')))
            payload = record['payload']
            if (not value['prompt'] and record.get('type') == 'response_item'
                    and payload.get('type') == 'message' and payload.get('role') == 'user'):
                value['prompt'] = _prompt(payload)
        for record in _records(tail):
            at = iso_ms(record.get('timestamp'))
            value['last_activity'] = max(value['last_activity'], at)
            payload = record['payload']
            kind, text = None, ''
            typ = payload.get('type')
            if record.get('type') == 'response_item':
                if typ == 'message' and payload.get('role') == 'user' and not value['prompt']:
                    value['prompt'] = _prompt(payload)
                elif typ in ('function_call', 'custom_tool_call'):
                    kind, text = 'tool', _tool_text(payload)
                elif typ == 'message' and payload.get('role') == 'assistant':
                    kind, text = 'message', _text(payload.get('content'))
                elif typ == 'reasoning':
                    kind, text = 'thinking', _text(payload.get('summary')) or 'Thinking'
            elif record.get('type') == 'event_msg':
                if typ == 'task_complete':
                    value['complete'] = True
                    value['final'] = _short(payload.get('last_agent_message'), 400) or None
                elif typ == 'agent_message':
                    kind, text = 'message', _text(payload.get('message'))
                elif typ == 'exec_command_begin':
                    kind, text = 'tool', _tool_text({'name': 'exec_command', 'arguments': payload})
            text = _short(text, 160, one_line=True)
            if kind and text:
                step = {'at': at, 'kind': kind, 'text': text}
                # Some versions emit both response_item and event_msg for one assistant message.
                if (kind != 'message' or not value['steps'] or
                        any(value['steps'][-1][key] != step[key] for key in ('kind', 'text'))):
                    value['steps'].append(step)
                    value['steps'] = value['steps'][-6:]
    _parsed[str(path)] = (key, value)
    _parsed.move_to_end(str(path))
    while len(_parsed) > 128:
        _parsed.popitem(last=False)
    return value


def _roots(pane_pid, procs):
    """Use codex_level.running's ancestor and count-once rule."""
    for pid, (ppid, argv) in procs.items():
        if not codex_level.is_run(argv):
            continue
        parent, owned, nested, seen = ppid, pid == pane_pid, False, set()
        while parent > 1 and parent in procs and parent not in seen:
            seen.add(parent)
            if codex_level.is_run(procs[parent][1]):
                nested = True
                break
            owned = owned or parent == pane_pid
            parent = procs[parent][0]
        if owned and not nested:
            yield pid


def _argv_prompt(argv):
    """The task text from `codex-lane ... "<prompt>"` or `codex exec ... "<prompt>"`: the last argument. In a long session
    file the first user message can sit past the head window, so the command line is the fallback."""
    last = argv[-1] if len(argv) > 1 else ''
    return '' if last.startswith('-') or last == 'exec' or not last.strip() else _short(last, 400)


def _session(pid, procs, proc, readlink):
    for child in sorted(descendants(pid, procs)):
        try:
            fds = sorted(procinfo.fd_names(child, proc))
        except OSError:
            continue
        for fd in fds:
            try:
                path = Path(procinfo.fd_target(child, fd, proc, readlink=readlink))
            except (OSError, ValueError):
                continue
            if path.name.startswith('rollout-') and path.suffix == '.jsonl':
                parsed = _parse(path)
                if parsed is not None:
                    return path, parsed
    return None, None


def _process_start(pid, proc, now):
    try:
        ticks = procinfo.start_ticks(pid, proc)
        return ticks, procinfo.start_ms(ticks, proc, now=now)
    except (OSError, ValueError, IndexError):
        return None, int(now * 1000)


def _fresh(pane_pid, now):
    pane = _panes.get(pane_pid)
    return pane if pane and 0 <= now - pane['at'] < CACHE_SECONDS else None


def _update(run, parsed):
    if parsed is not None:
        run.update({key: val for key, val in parsed.items()
                    if key != 'complete' and (val or key not in ('id', 'started', 'last_activity'))})


def runs_for_pane(pane_pid, procs, now=None, readlink=os.readlink, proc=Path('/proc')):
    """Newest runs first, including up to three runs that ended within ten minutes. now is epoch seconds."""
    now = time.time() if now is None else now
    proc = Path(proc)
    with _lock:
        cached = _fresh(pane_pid, now)
        if cached:
            return copy.deepcopy(cached['answer'])
        for key in list(_panes):
            if now - _panes[key]['at'] >= ENDED_SECONDS:
                del _panes[key]
        previous = _panes.get(pane_pid, {'live': {}, 'ended': []})
        live = {}
        for pid in _roots(pane_pid, procs):
            ticks, started = _process_start(pid, proc, now)
            old = previous['live'].get(pid)
            if old and old['ticks'] != ticks:
                old = None
            path, parsed = _session(pid, procs, proc, readlink)
            if parsed is None and old and old['path']:
                path = old['path']
                parsed = _parse(path)
            run = copy.deepcopy(old['run']) if old else {
                'id': f'pid-{pid}', 'pid': pid, 'started': started, 'cwd': '', 'prompt': '',
                'steps': [], 'last_activity': started, 'final': None}
            _update(run, parsed)
            if not run['cwd']:
                try:
                    run['cwd'] = procinfo.cwd(pid, proc, readlink=readlink)
                except OSError:
                    pass
            run['pid'] = pid
            if not run['prompt']:
                run['prompt'] = _argv_prompt(procs[pid][1])
            run['state'] = ('starting' if path is None else
                            'stalled' if now * 1000 - run['last_activity'] > 300_000 else 'running')
            live[pid] = {'run': run, 'path': path, 'ticks': ticks}
        live_ids = {item['run']['id'] for item in live.values()}
        ended = [item for item in previous['ended']
                 if now - item['at'] < ENDED_SECONDS and item['run']['id'] not in live_ids]
        for pid, old in previous['live'].items():
            if old['run']['id'] in live_ids:
                continue
            parsed = _parse(old['path'], force=True) if old['path'] else None
            run = copy.deepcopy(old['run'])
            _update(run, parsed)
            run['state'] = 'done' if parsed and parsed['complete'] else 'failed'
            ended.append({'at': now, 'run': run})
        ended = sorted(ended, key=lambda item: (item['at'], item['run']['started']), reverse=True)[:3]
        answer = sorted([item['run'] for item in live.values()] + [item['run'] for item in ended],
                        key=lambda run: (run['started'], run['pid']), reverse=True)
        _panes[pane_pid] = {'at': now, 'live': live, 'ended': ended, 'answer': answer}
        return copy.deepcopy(answer)


def handles(path):
    return path.split('?', 1)[0] == PATH


def get(handler, choose):
    config = choose(parse_qs(urlsplit(handler.path).query).get('session', [None])[0])
    runs = []
    if config.get('pane_pid') is not None:
        pid = int(config['pane_pid'])
        with _lock:
            now = time.time()
            cached = _fresh(pid, now)
            runs = (copy.deepcopy(cached['answer']) if cached else
                    runs_for_pane(pid, codex_level.processes(), now=now))
    handler.respond(200, {'pane': config['id'],
                          'running': sum(run['state'] in ('starting', 'running', 'stalled') for run in runs),
                          'runs': runs})
