"""Message many panes at once: the one broker behind the Agents page "Message sessions" box and `tether broadcast`.

Every pane goes through the chat composer's own guarded send (server.send_text -> terminal_input.submit), so a broadcast
keeps the composer's checks: the agent and conversation are still the same, the input box holds no draft, and no approval
or picker is on screen. The state is read again right before each pane's send. A pane that waits on an approval, a
question or a picker is skipped and never typed into. A busy agent takes typed input while it works; Tether keeps no queue
of its own, so that result is "sent while busy", not "queued".

Claude master: one Claude pane gets one message with the text and the target list, and delivers it with its own peer
messaging. It gets no approval authority; targets that wait on the user are skipped here first, and it is not its own target.
"""
import subprocess

from terminal_input import DraftError

MAX_TEXT = 20000
MAX_PANES = 100
AGENTS = ('claude', 'codex', 'antigravity')
NAMES = {'claude': 'Claude', 'codex': 'Codex', 'antigravity': 'Antigravity'}
WAITING = {'Approval': 'Waits on an approval.', 'Input needed': 'Waits on a question or a picker.',
           'Unavailable': 'The agent is not available.'}


def title(entry):
    return entry.get('pane_name') or entry.get('agent_name') or entry.get('label') or entry['id']


def targets_of(panes):
    """[(pane id, thread or None, thread given)] from ['%1', ...] or [{'pane': '%1', 'thread': ...}, ...], first copy only."""
    if not isinstance(panes, list) or len(panes) > MAX_PANES:
        raise ValueError(f'Choose up to {MAX_PANES} panes.')
    seen, found = set(), []
    for item in panes:
        pane = item.get('pane') if isinstance(item, dict) else item
        if not isinstance(pane, str) or not pane.startswith('%') or not pane[1:].isdigit():
            raise ValueError('Choose panes by their tmux pane id, such as %12.')
        if pane in seen:
            continue
        seen.add(pane)
        found.append((pane, item.get('thread') if isinstance(item, dict) else None, isinstance(item, dict) and 'thread' in item))
    return found


def row(pane, entry, result, reason=''):
    out = {'pane': pane, 'name': title(entry) if entry else pane, 'agent': entry.get('agent') if entry else None, 'result': result}
    if reason:
        out['reason'] = reason
    return out


def check(pane, thread, bound, entry, status):
    """Why this pane must not get the text now ('' = it may), and its fresh state."""
    if not entry:
        return 'The pane has ended.', None
    if entry.get('agent') not in AGENTS:
        return 'Not an agent pane.', None
    if not entry.get('writable') or entry.get('ambiguous'):
        return 'Messaging is unavailable for this pane.', None
    if bound and thread != entry.get('thread'):
        return 'The pane changed conversations. Reload and choose again.', None
    try:
        state = status(entry)['state']
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return 'Could not read the pane state.', None
    return WAITING.get(state, ''), state


def send_one(pane, entry, text, status, send, thread=None, bound=False):
    """Re-check one pane, then send through the composer path. Returns its result row."""
    reason, state = check(pane, thread, bound, entry, status)
    if reason:
        return row(pane, entry, 'skipped', reason)
    try:
        delivery = send(entry, text, state)
    except DraftError:
        return row(pane, entry, 'skipped', 'Its input box has an unsent draft.')
    except RuntimeError as exc:
        message = str(exc)
        # submit() refuses these before it pastes anything, so nothing was typed.
        if message.startswith('Respond to the pending approval'):
            return row(pane, entry, 'skipped', WAITING['Approval'])
        if 'pick an option' in message:
            return row(pane, entry, 'skipped', WAITING['Input needed'])
        return row(pane, entry, 'failed', message)
    except (OSError, subprocess.SubprocessError):
        return row(pane, entry, 'failed', 'Could not confirm delivery. Check the terminal.')
    return row(pane, entry, 'sent_busy' if delivery == 'queued' else 'sent')


def master_text(text, targets, cli):
    lines = ['[Tether broadcast] Deliver the message below to each target pane with your own peer messaging.',
             'Do not answer approvals, questions or pickers for them. Do not send it to yourself.',
             f'If peer messaging cannot reach a pane (a Codex pane, for example), run: {cli} broadcast --panes <pane id> "<message>"',
             'That command skips a pane that waits on approval. When done, tell me which panes got it.',
             '', 'Targets:']
    for entry, state in targets:
        lines.append(f"- {entry['id']} · {title(entry)} · {NAMES.get(entry.get('agent'), entry.get('agent'))} · "
                     f"tmux session {entry.get('tmux_session') or '?'} · {entry.get('cwd') or ''} · {state}".rstrip(' ·'))
    return '\n'.join(lines + ['', 'Message:', text])


def deliver(text, panes, *, entries, status, send, via='direct', master=None, exclude=None, cli='tether'):
    """Send `text` to `panes` (None: every agent pane that is idle now). entries: () -> discovered panes; status: entry ->
    live status; send: (entry, text, state) -> 'queued' | 'submitted', raising like terminal_input.submit.
    Returns {'via', 'results': [{'pane', 'name', 'agent', 'result', 'reason'?}], 'master'?}.
    result: sent | sent_busy | skipped | failed | handed_off (Claude master only)."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError(f'Enter a message of up to {MAX_TEXT} characters.')
    if any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in text):
        raise ValueError('The message contains terminal control characters.')
    if via not in ('direct', 'claude'):
        raise ValueError('Send via Direct or a Claude master.')
    if exclude is not None and not (isinstance(exclude, str) and exclude.startswith('%')):
        raise ValueError('Exclude a pane by its tmux pane id, such as %12.')
    skip = {exclude} if exclude else set()
    found = {entry['id']: entry for entry in entries()}
    if panes is None:  # the page's default: the agent panes that are idle now
        targets = [(pane, None, False) for pane, entry in found.items()
                   if pane not in skip and pane != master and check(pane, None, False, entry, status) == ('', 'Idle')]
    else:
        targets = targets_of(panes)
    if via == 'direct':
        results = [send_one(pane, found.get(pane), text, status, send, thread, bound) if pane not in skip
                   else row(pane, found.get(pane), 'skipped', 'This is the sending pane.')
                   for pane, thread, bound in targets]
        return {'via': 'direct', 'results': results}
    boss = found.get(master) if isinstance(master, str) else None
    if not boss or boss.get('agent') != 'claude' or not boss.get('writable'):
        raise ValueError('Choose a live Claude pane as the master.')
    results, ready = [], []
    for pane, thread, bound in targets:
        if pane == master or pane in skip:
            results.append(row(pane, found.get(pane), 'skipped', 'This is the master pane.' if pane == master else 'This is the sending pane.'))
            continue
        reason, state = check(pane, thread, bound, found.get(pane), status)
        if reason:
            results.append(row(pane, found.get(pane), 'skipped', reason))
        else:
            ready.append((found[pane], state))
            results.append(row(pane, found[pane], 'handed_off'))
    if not ready:
        return {'via': 'claude', 'results': results, 'master': row(master, boss, 'skipped', 'No target can take the message.')}
    head = send_one(master, boss, master_text(text, ready, cli), status, send)
    if head['result'] not in ('sent', 'sent_busy'):
        results = [dict(item, result='skipped', reason='The master did not get the message.') if item['result'] == 'handed_off' else item
                   for item in results]
    return {'via': 'claude', 'results': results, 'master': head}
