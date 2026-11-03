"""The /goal and background tasks of one Claude conversation, with bounded output and a process-group stop.

The transcript only grows, so each file is read once and then from where the last read stopped."""
import json
import os
from pathlib import Path
import re
import signal
import stat
import threading
import time

import history

ENDED = re.compile(r'<status>(completed|failed|killed|stopped|error)</status>|Monitor (?:expired|ended|stopped)|\[Monitor (?:expired|ended|stopped)')
TASK_ID = re.compile(r'<task-id>([^<]+)</task-id>')
STARTED = re.compile(r'running in background with ID: (\w+)|Monitor started \(task (\w+)')
OUTPUT = re.compile(r'Output is being written to: (\S+?\.output)')
TAIL_BYTES = 65536
ANSI = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")
EXITED = re.compile(r'\[exited with code -?\d+\]\s*$')
_states = {}
_lock = threading.Lock()


def _iso(stamp):
    try:
        return time.mktime(time.strptime(stamp[:19], '%Y-%m-%dT%H:%M:%S')) - time.timezone if stamp else None
    except ValueError:
        return None


def _consume(state, record):
    if isinstance(record.get('cwd'), str) and isinstance(record.get('sessionId'), str) and re.fullmatch(r'[\w-]+', record['sessionId']):
        state['output_dir'] = Path(f'/tmp/claude-{os.getuid()}') / record['cwd'].replace('/', '-') / record['sessionId'] / 'tasks'
    kind = record.get('type')
    message = record.get('message') if isinstance(record.get('message'), dict) else {}
    content = message.get('content')
    if kind == 'attachment':
        attachment = record.get('attachment') or {}
        if attachment.get('type') == 'goal_status':
            goal = state['goal'] if state['goal'] and state['goal']['condition'] == attachment.get('condition') else {'condition': attachment.get('condition'), 'set_at': _iso(record.get('timestamp'))}
            goal['met'] = bool(attachment.get('met'))
            if attachment.get('reason'):  # the "sentinel" rows only repeat the condition; real checks carry a reason
                goal.update(reason=attachment['reason'], checked_at=_iso(record.get('timestamp')))
            state['goal'] = goal
        return
    if kind == 'assistant' and isinstance(content, list):
        for part in content:
            if not isinstance(part, dict) or part.get('type') != 'tool_use':
                continue
            data = part.get('input') or {}
            if part.get('name') == 'Monitor' or (part.get('name') == 'Bash' and data.get('run_in_background')):
                state['pending'][part['id']] = {'kind': 'monitor' if part['name'] == 'Monitor' else 'shell',
                                                'title': data.get('description') or '', 'command': data.get('command') or '',
                                                'started': _iso(record.get('timestamp')), 'timeout': data.get('timeout_ms') or data.get('timeoutMs')}
            elif part.get('name') in ('TaskStop', 'KillShell'):
                state['ended'].add(data.get('task_id') or data.get('shell_id') or '')
        return
    if kind != 'user':
        return
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get('type') == 'tool_result' and part.get('tool_use_id') in state['pending']:
                task = state['pending'].pop(part['tool_use_id'])
                result = record.get('toolUseResult') if isinstance(record.get('toolUseResult'), dict) else {}
                body = part.get('content') if isinstance(part.get('content'), str) else json.dumps(part.get('content'))
                started = STARTED.search(body or '')
                identifier = result.get('backgroundTaskId') or result.get('taskId') or (started and (started[1] or started[2]))
                if identifier:
                    output = OUTPUT.search(body or '')
                    task.update(id=identifier, output=output[1] if output else str(state['output_dir'] / (identifier + '.output')) if state.get('output_dir') and re.fullmatch(r'[\w-]+', identifier) else None)
                    if result.get('timeoutMs'):
                        task['timeout'] = result['timeoutMs']
                    state['tasks'][identifier] = task


def _tail(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - TAIL_BYTES))
            lines = stream.read(TAIL_BYTES).decode('utf-8', 'replace').splitlines()
        return '\n'.join(ANSI.sub('', '\n'.join(lines[1:] if size > TAIL_BYTES else lines)).splitlines()[-40:])
    except OSError:
        return None


def read(path, now=None):
    """{'goal': {...} | None, 'tasks': [...]} for one transcript file."""
    path = Path(path)
    with _lock:
        state = _states.get(path)
        stat = path.stat()
        if not state or state['inode'] != stat.st_ino or stat.st_size < state['offset']:
            state = _states[path] = {'inode': stat.st_ino, 'offset': 0, 'goal': None, 'tasks': {}, 'pending': {}, 'ended': set()}
        with open(path, 'rb') as stream:
            stream.seek(state['offset'])
            chunk = stream.read()
        end = chunk.rfind(b'\n') + 1  # leave a half-written last line for the next read
        for line in chunk[:end].splitlines():
            if b'<task-notification>' in line:  # sent as queue-operation, attachment and user records
                for block in line.decode('utf-8', 'replace').split('<task-notification>')[1:]:
                    found = TASK_ID.search(block)
                    if found and ENDED.search(block):
                        state['ended'].add(found[1])
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                _consume(state, record)
        state['offset'] += end
        now = now or time.time()
        tasks = []
        for identifier, task in state['tasks'].items():
            output = _tail(task['output']) if task.get('output') else None
            ended = identifier in state['ended'] or (task['timeout'] and task['started'] and now > task['started'] + task['timeout'] / 1000) or (output and EXITED.search(output))
            tasks.append({key: task.get(key) for key in ('kind', 'title', 'command', 'started')} | {'id': identifier, 'output': output, 'state': 'ended' if ended else 'running'})
        return {'goal': dict(state['goal']) if state['goal'] else None, 'tasks': tasks}


def for_thread(thread):
    if not thread:
        return {'goal': None, 'tasks': []}
    found = next(history.transcript_files(thread), None)
    return read(found[0]) if found else {'goal': None, 'tasks': []}


def _task(thread, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r'[\w-]+', identifier):
        raise ValueError('Invalid task id.')
    found = next(history.transcript_files(thread), None) if thread else None
    if not found:
        raise ValueError('No conversation found.')
    path = Path(found[0])
    read(path)
    with _lock:
        task = _states[path]['tasks'].get(identifier)
        if not task:
            raise ValueError('Task does not belong to this conversation.')
        return path, dict(task)


def shell_output(thread, identifier):
    _, task = _task(thread, identifier)
    return {'output': _tail(task['output']) if task.get('output') else None}


def _processes():
    result = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            try:
                args = (entry / 'cmdline').read_bytes().split(b'\0')
            except PermissionError:
                args = []
            result[int(entry.name)] = {'uid': entry.stat().st_uid, 'group': int(fields[2]), 'start': fields[19],
                                       'parent': int(fields[1]), 'state': fields[0], 'name': (entry / 'comm').read_text().strip(), 'args': [os.fsdecode(arg) for arg in args if arg]}
        except (OSError, ValueError, IndexError):
            continue
    return result


def _groups(output):
    matches = set()
    for pid, proc in _processes().items():
        if proc['uid'] != os.getuid():
            continue
        for fd in (1, 2):
            try:
                if os.readlink(f'/proc/{pid}/fd/{fd}') == output:
                    matches.add(proc['group'])
            except OSError:
                pass
    if not matches:
        raise ValueError('No process writes to this task output.')
    return matches


def _safe_group(group, processes):
    members = {pid: proc for pid, proc in processes.items() if proc['group'] == group}
    if not members:
        raise ValueError('The process group has ended.')
    protected = {1}
    parent = os.getpid()
    while parent not in protected:
        protected.add(parent)
        if parent not in processes:
            break
        parent = processes[parent]['parent']
    for pid, proc in members.items():
        names = {Path(arg).name for arg in proc['args'][:2]} | {proc.get('name', '')}
        if pid in protected or group <= 1 or proc['uid'] != os.getuid() or names & {'claude', 'claude.exe', 'tmux', 'server.py'}:
            raise ValueError('Refusing to stop a protected process group.')
    return members


def stop_task(thread, identifier):
    path, task = _task(thread, identifier)
    if task['kind'] != 'shell' or not task.get('output'):
        raise ValueError('Only shells with an output file can be stopped.')
    groups = _groups(task['output'])
    processes = _processes()
    identities = {group: _safe_group(group, processes) for group in groups}
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        current = _processes()
        if not any(pid in current and current[pid]['start'] == proc['start'] and current[pid]['state'] != 'Z' for members in identities.values() for pid, proc in members.items()):
            break
        time.sleep(.1)
    current = _processes()
    for group, members in identities.items():
        if any(pid in current and current[pid]['start'] == proc['start'] and current[pid]['state'] != 'Z' for pid, proc in members.items()):
            _safe_group(group, current)  # recheck the whole group before escalation
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
    with _lock:
        _states[path]['ended'].add(identifier)
    return {'accepted': True}
