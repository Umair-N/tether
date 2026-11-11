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
    """Only local agents are eligible; earlier tasks and other sections are not."""
    found, active = [], False
    for line in text.splitlines():
        if re.fullmatch(r'\s*Local agents \(\d+\)\s*', line):
            active = True
            continue
        if not active:
            continue
        match = re.fullmatch(r'\s*(❯)?\s*●\s*(.+?)\s*', line)
        if not match:
            break
        label = re.sub(r'\s+(?:running|completed|failed|stopped|killed)(?:\s*·.*)?$', '', match[2]).rstrip()
        found.append({'label': label, 'selected': bool(match[1])})
    return found


def stop_selected(entry, description, kind, run, sleep):
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
            headers = [found[1].strip() for found in (re.fullmatch(rf'\s*{re.escape(kind)} › (.+)', line) for line in screen.splitlines()) if found]
            if len(headers) != 1 or not matches(description, headers[0]):
                raise StopError('Could not identify that agent. Stop it in the terminal.')
            break
        if '↑/↓ to select' not in shown:
            raise StopError('Could not read the tasks dialog. Check the terminal view.')
        agents = rows(screen)
        targets = [index for index, row in enumerate(agents) if matches(description, row['label'])]
        if len(targets) > 1:
            raise StopError('Two agents share this name. Stop it in the terminal.')
        if not targets:
            raise StopError('Could not identify that agent. Stop it in the terminal.')
        selected = [index for index, row in enumerate(agents) if row['selected']]
        if len(selected) != 1:
            raise StopError('Could not see which agent is selected. Check the terminal view.')
        if selected[0] == targets[0]:
            break
        if step == 40:
            raise StopError('Could not reach that agent. Stop it in the terminal.')
        press(entry, 'Down' if targets[0] > selected[0] else 'Up', run=run)
        sleep(0.18)
        screen = visible(entry, run)
    press(entry, 'x', run=run)
    sleep(0.18)
    visible(entry, run)


def stop(entry, agent_id, run=subprocess.run, sleep=time.sleep):
    try:
        if entry.get('agent') != 'claude':
            raise StopError('Only Claude sessions support stopping subagents.')
        subagents._path(entry, agent_id)
        agent = next((item for item in subagents.listing(entry)['agents'] if item['id'] == agent_id), None)
        if agent is None or agent['state'] != 'running':
            raise StopError('That agent is not running.')
        description = agent.get('description')
        if not isinstance(description, str) or not description.strip():
            raise StopError('Could not identify that agent. Stop it in the terminal.')
        terminal_input.verify(entry, run)
        # Do not drive an unverified pane, including a pane for another agent.
        close(entry, run, sleep)
        try:
            if terminal_input.live_status(entry, run)['state'] not in ('Idle', 'Working'):
                raise StopError('Claude is waiting for you. Answer its prompt before stopping an agent.')
            if any(terminal_input.prompt_draft(entry, run)):
                raise StopError('There is an unsent draft in the agent input. Clear it, then try again.')
            stop_selected(entry, description, agent.get('type') or 'general-purpose', run, sleep)
        finally:
            close(entry, run, sleep)
        for attempt in range(21):
            agent = next((item for item in subagents.listing(entry)['agents'] if item['id'] == agent_id), None)
            if agent is not None and agent['state'] == 'stopped':
                return {'ok': True, 'state': 'stopped'}
            if attempt < 20:
                sleep(0.25)
        raise StopError('The agent did not stop. Check the terminal view.')
    except StopError:
        raise
    except (ValueError, RuntimeError) as exc:
        raise StopError(str(exc)) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise StopError('Could not stop the agent. Check the terminal view.') from exc
