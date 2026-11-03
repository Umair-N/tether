"""Quiet, fail-open Claude hook capture and read-only footer overlay."""
import json
import os
from pathlib import Path
import re
import sys
import time


EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
          'PostToolUseFailure', 'PermissionRequest', 'PermissionDenied',
          'Notification', 'Stop', 'StopFailure', 'SubagentStart', 'SubagentStop',
          'SessionEnd', 'PostModelSwitch')
MODES = {'default': '', 'plan': '⏸ plan mode on', 'acceptEdits': '⏵⏵ accept edits on',
         'auto': '⏵⏵ auto mode on', 'dontAsk': 'dontAsk',
         'bypassPermissions': '⏵⏵ bypass permissions on'}
# Slow thinking/replies can take many minutes; bound missed turn events at 30m.
TURN_MAX_AGE = 1800


def valid_session(session):
    return isinstance(session, str) and re.fullmatch(r'[a-zA-Z0-9-]+', session)


def short(value):
    return ' '.join(value.split())[:160] if isinstance(value, str) else ''


def update(state, data, now):
    event, agent = data['hook_event_name'], data.get('agent_id')
    # Subagent tool context must not replace the main session's mode or effort.
    if not agent and now >= state.get('settings_at', 0):
        if data.get('permission_mode') in MODES:
            state['mode'] = MODES[data['permission_mode']]
        effort = data.get('effort')
        if isinstance(effort, dict) and effort.get('level') in ('low', 'medium', 'high', 'xhigh', 'max'):
            state['effort'] = effort['level']
        state['settings_at'] = now
    if event == 'Notification' and data.get('notification_type') not in ('permission_prompt', 'idle_prompt'):
        return
    # Settings changes cannot revive an old turn.
    if event == 'PostModelSwitch':
        return
    # Removing a unique agent id is independent of main-turn ordering.
    if event == 'SubagentStop':
        state.setdefault('agents', {}).pop(agent, None)
        return
    if now < state.get('reported_at', 0):
        return
    agents = state.setdefault('agents', {})
    if event == 'SubagentStart' and isinstance(agent, str):
        agents[agent] = {'name': short(data.get('agent_type')) or agent, 'started': now}
    elif agent:
        return  # subagent completions must not end the parent's turn
    else:
        state['reported_at'] = now
    if agent or event in ('SubagentStart', 'SubagentStop'):
        return
    if event == 'SessionStart':
        state.update(ended=False, busy=False, waiting=False, tools={}, agents={})
    elif event == 'SessionEnd':
        state.update(ended=True, busy=False, waiting=False, tools={}, agents={})
    elif event == 'UserPromptSubmit':
        state.update(ended=False, busy=True, waiting=False, started=now, tools={})
    elif event in ('Stop', 'StopFailure') or (event == 'Notification' and data.get('notification_type') == 'idle_prompt'):
        state.update(busy=False, waiting=False, tools={})
    elif event == 'PermissionRequest' or (event == 'Notification' and data.get('notification_type') == 'permission_prompt'):
        state['waiting'] = True
    elif event == 'PreToolUse':
        state.update(busy=True, waiting=False)
        state.setdefault('started', now)
        tool = data.get('tool_input')
        tool = tool if isinstance(tool, dict) else {}
        state.setdefault('tools', {})[short(data.get('tool_use_id')) or short(data.get('tool_name'))] = {
            'title': short(data.get('tool_name')),
            'command': next((short(tool.get(key)) for key in ('command', 'description', 'file_path', 'pattern', 'query') if short(tool.get(key))), ''),
            'started': now}
    elif event in ('PostToolUse', 'PostToolUseFailure', 'PermissionDenied'):
        state['waiting'] = False
        tools = state.setdefault('tools', {})
        if event == 'PermissionDenied' and not data.get('tool_use_id'):
            for key in list(tools):
                if tools[key]['title'] == data.get('tool_name'):
                    tools.pop(key)
        else:
            tools.pop(short(data.get('tool_use_id')) or short(data.get('tool_name')), None)


def capture(data, now=None):
    if not isinstance(data, dict) or not valid_session(data.get('session_id')) or data.get('hook_event_name') not in EVENTS:
        return
    import fcntl  # only the writer needs a process lock
    now = time.time() if now is None else now
    folder = Path(os.environ.get('MOBILE_TMUX_CACHE') or Path.home() / '.cache/mobile-tmux') / 'hooks'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    session = data['session_id']
    target = folder / (session + '.json')
    # Atomic rename protects readers; a short lock also protects concurrent writers.
    with os.fdopen(os.open(folder / (session + '.lock'), os.O_CREAT | os.O_WRONLY, 0o600), 'w') as lock:
        deadline = time.monotonic() + .1
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return
                time.sleep(.005)
        try:
            state = json.loads(target.read_text())
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict) or state.get('session_id') != session:
            state = {'session_id': session}
        update(state, data, now)
        temporary = folder / (session + '.' + str(os.getpid()) + '.tmp')
        try:
            with os.fdopen(os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(state, stream)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def transcript_newer(path, reported):
    """A newer prompt or completed response invalidates a missed lifecycle event.

    Streaming/tool records are normal during a turn. Inspect only a bounded tail;
    transcript writes can lag hooks, so allow one second for the same event.
    """
    from datetime import datetime
    with open(path, 'rb') as stream:
        stream.seek(0, 2)
        start = max(0, stream.tell() - 65536)
        stream.seek(start)
        if start:
            stream.readline()
        for line in stream:
            try:
                record = json.loads(line)
                if record.get('isSidechain') or record.get('isMeta'):
                    continue
                message = record.get('message') or {}
                content = message.get('content')
                prompt = record.get('type') == 'user' and (isinstance(content, str) or
                         isinstance(content, list) and any(part.get('type') == 'text' for part in content if isinstance(part, dict)))
                done = record.get('type') == 'assistant' and message.get('stop_reason') in ('end_turn', 'stop_sequence')
                if (prompt or done) and datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp() > reported + 1:
                    return True
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
    return False


def elapsed(started, now):
    seconds = max(0, int(now - started))
    return f'{seconds // 60}m {seconds % 60}s' if seconds >= 60 else f'{seconds}s'


def overlay(config, result, now=None, screen_read_at=None):
    """Keep the existing footer schema and screen fields outside hook coverage."""
    if config.get('agent') != 'claude' or config.get('ambiguous') or not valid_session(config.get('thread')):
        return result
    import paths
    now = time.time() if now is None else now
    try:
        state = json.loads((paths.CACHE / 'hooks' / (config['thread'] + '.json')).read_text())
        reported = state['reported_at']
        age = now - reported
        if state['session_id'] != config['thread'] or state.get('ended') or age < 0:
            return result
        merged = dict(result)
        for key in ('mode', 'effort'):
            if key in state:
                merged[key] = state[key]
        # Shift+Tab has no hook. Only a later, explicitly parsed mode line wins;
        # an empty mode may mean a dialog or a clipped footer, not default mode.
        if ('mode' in state and result.get('mode') and screen_read_at is not None
                and screen_read_at > max(reported, state.get('settings_at', reported))):
            from footer import mode_key
            if mode_key(result['mode']) != mode_key(state['mode']):
                merged['mode'] = result['mode']
        tools = state.get('tools', {})
        lifecycle_fresh = bool(tools) or age <= TURN_MAX_AGE
        if lifecycle_fresh and config.get('transcript') and transcript_newer(config['transcript'], reported):
            lifecycle_fresh = False
        if lifecycle_fresh:
            merged['spinner'] = None
            merged['running'] = None
            if state.get('busy') and not state.get('waiting'):
                merged['spinner'] = {'verb': 'Working…', 'detail': elapsed(state.get('started', reported), now)}
                if tools:
                    tool = max(tools.values(), key=lambda item: item['started'])
                    merged['running'] = {key: tool[key] for key in ('title', 'command')}
                    merged['running']['elapsed'] = elapsed(tool['started'], now)
        # Screen rows include the main agent and richer task details. They lack
        # hook IDs, so even fresh empty hooks cannot safely remove those rows.
        merged['agents'] = list(result.get('agents', []))
        screen_names = [row['name'] for row in merged['agents'] if not row['main']]
        for agent in state.get('agents', {}).values():
            if agent['name'] in screen_names:
                screen_names.remove(agent['name'])
            else:
                merged['agents'].append({'name': agent['name'], 'task': '', 'detail': elapsed(agent['started'], now), 'main': False})
        return merged
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return result


if __name__ == '__main__':
    try:
        occurred = time.time()
        capture(json.load(sys.stdin), occurred)
    except Exception:
        pass  # hooks must never print context or fail Claude Code
