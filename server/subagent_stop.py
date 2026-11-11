"""Stop one Claude subagent through /tasks without disturbing the main turn."""
import re
import subprocess
import time

from model_picker import press, visible
import subagents
import terminal_input


class StopError(RuntimeError):
    """The stop failed; the message is safe to show the user."""


def hint(text):
    return ' '.join(line.strip() for line in text.splitlines()[-14:])


def dialog_open(text):
    shown = hint(text)
    return ('x to stop' in shown or 'Esc/Enter/Space to close' in shown
            or '↑/↓ to select' in shown and 'Esc to close' in shown)


def close(entry, run, sleep):
    """Esc is safe only while the tasks dialog's hint remains visible."""
    for _ in range(3):
        if not dialog_open(visible(entry, run)):
            return
        press(entry, 'Escape', run=run)
        sleep(0.35)
    if dialog_open(visible(entry, run)):
        raise StopError('The tasks dialog did not close. Check the terminal view.')


def matches(description, shown):
    return (description == shown or shown.endswith('…') and bool(shown[:-1].strip())
            and description.startswith(shown[:-1].rstrip()))


def rows(text):
    """Read selectable rows across all sections in screen order."""
    found, section = [], None
    for line in text.splitlines():
        if '↑/↓ to select' in line:
            break
        header = re.fullmatch(r'\s*(.+?) \(\d+\)\s*', line)
        if header:
            section = header[1]
            continue
        if section is None:
            continue
        match = re.fullmatch(r'\s*(❯)?\s*●\s*(.+?)\s*', line)
        if not match:
            continue
        label = re.sub(r'\s+(?:running|completed|failed|stopped|killed)(?:\s*·.*)?$', '', match[2]).rstrip()
        found.append({'label': label, 'selected': bool(match[1]), 'section': section})
    return found


def stop_selected(entry, description, kind, run, sleep, *, task=False):
    noun = 'task' if task else 'agent'
    labels = description if task else [description]

    def identified(label):
        return any(matches(value, label) for value in labels)

    press(entry, '-l', '/tasks', run=run)
    sleep(0.7)
    screen = visible(entry, run)
    if terminal_input.approval(screen) or dialog_open(screen):
        raise StopError('The terminal changed. Check the terminal view.')
    press(entry, 'Enter', run=run)  # A second Enter could act on the selected task.
    screen = ''
    for _ in range(14):
        sleep(0.3)
        screen = visible(entry, run)
        if 'x to stop' in hint(screen):
            break
    else:
        raise StopError('The tasks dialog did not open. Check the terminal view.')
    for step in range(41):
        shown = hint(screen)
        if 'x to stop' not in shown:
            raise StopError('The tasks dialog changed. Check the terminal view.')
        if 'Esc/Enter/Space to close' in shown:
            # The header is "<agent type> › <description>"; Progress lines also start with "›".
            if task:
                # Task details put their command/description on the first header line.
                header = next((line.strip() for line in screen.splitlines() if line.strip()), '')
                headers = [header, header.split(' › ', 1)[-1]]
            else:
                headers = [found[1].strip() for found in (re.fullmatch(rf'\s*{re.escape(kind)} › (.+)', line) for line in screen.splitlines()) if found]
            if (not task and len(headers) != 1) or not any(identified(header) for header in headers):
                raise StopError(f'Could not identify that {noun}. Stop it in the terminal.')
            break
        if '↑/↓ to select' not in shown:
            raise StopError('Could not read the tasks dialog. Check the terminal view.')
        agents = rows(screen)
        targets = [index for index, row in enumerate(agents)
                   if row['section'] == ('Shells' if task else 'Local agents') and identified(row['label'])]
        if len(targets) > 1:
            raise StopError(f'Two {noun}s share this name. Stop it in the terminal.')
        if not targets:
            raise StopError(f'Could not identify that {noun}. Stop it in the terminal.')
        selected = [index for index, row in enumerate(agents) if row['selected']]
        if len(selected) != 1:
            raise StopError(f'Could not see which {noun} is selected. Check the terminal view.')
        if selected[0] == targets[0]:
            break
        if step == 40:
            raise StopError(f'Could not reach that {noun}. Stop it in the terminal.')
        press(entry, 'Down' if targets[0] > selected[0] else 'Up', run=run)
        sleep(0.18)
        screen = visible(entry, run)
    press(entry, 'x', run=run)
    sleep(0.18)
    visible(entry, run)


def stop(entry, agent_id, run=subprocess.run, sleep=time.sleep):
    noun = 'agent'
    try:
        if entry.get('agent') != 'claude':
            raise StopError('Only Claude sessions support stopping subagents.')
        listed = subagents.listing(entry)
        agent = next((item for item in listed['agents'] if item['id'] == agent_id), None)
        is_task = False
        if agent is None:
            agent = next((item for item in listed.get('tasks', []) if item['id'] == agent_id), None)
            is_task = agent is not None
        if is_task:
            noun = 'task'
        elif agent is None:
            subagents._path(entry, agent_id)  # Preserve validation and unavailable errors.
        if agent is None or agent['state'] != 'running':
            raise StopError(f'That {noun} is not running.')
        description = agent.get('description')
        if is_task:
            command = agent.get('command')
            description = [value for value in (description, command.splitlines()[0] if command else None)
                           if isinstance(value, str) and value.strip()]
        if (not description if is_task else not isinstance(description, str) or not description.strip()):
            raise StopError(f'Could not identify that {noun}. Stop it in the terminal.')
        terminal_input.verify(entry, run)
        # Do not drive an unverified pane, including a pane for another agent.
        close(entry, run, sleep)
        try:
            if terminal_input.live_status(entry, run)['state'] not in ('Idle', 'Working'):
                raise StopError(f'Claude is waiting for you. Answer its prompt before stopping a {noun}.')
            if any(terminal_input.prompt_draft(entry, run)):
                raise StopError(f'There is an unsent draft in the {noun} input. Clear it, then try again.')
            stop_selected(entry, description, agent.get('type') or 'general-purpose', run, sleep, task=is_task)
        finally:
            close(entry, run, sleep)
        for attempt in range(21):
            collection = 'tasks' if is_task else 'agents'
            agent = next((item for item in subagents.listing(entry)[collection] if item['id'] == agent_id), None)
            if agent is None:
                # Earlier-chat rows leave the public listing as soon as they stop.
                confirmed = subagents._listing(entry, include_finished=True)
                agent = next((item for item in confirmed[collection] if item['id'] == agent_id), None)
            if agent is not None and agent['state'] in (('stopped', 'finished', 'failed') if is_task else ('stopped',)):
                return {'ok': True, 'state': agent['state']}
            if attempt < 20:
                sleep(0.25)
        raise StopError(f'The {noun} did not stop. Check the terminal view.')
    except StopError:
        raise
    except (ValueError, RuntimeError) as exc:
        raise StopError(str(exc)) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise StopError(f'Could not stop the {noun}. Check the terminal view.') from exc
