"""What Claude Code shows under and above its input box: the suggested next prompt, right-aligned notices
("Update installed · /restart to apply", "4% until auto-compact"), the effort badge, the mode line and background agents.
Read-only: it parses one capture of the pane."""
import json
from pathlib import Path
import re
import time

from terminal_input import ANSI_SGR, dim_prompt_text

RULE = re.compile(r'^─+')
EFFORT = re.compile(r'[○◔◐◕●]\s*(low|medium|high|xhigh|max)\b', re.I)
UPDATE = re.compile(r'update (?:installed|available)|/restart\b', re.I)
GOAL = re.compile(r'/goal active(?:\s*\(([^)]*)\))?', re.I)
SPINNER = re.compile(r'^\S\s+([A-Z][^()…]*…)\s*\((\d[^)]*)\)')
RUNNING = re.compile(r'^[● ] (\S.*?) · (\d[\dhms ]*\S)$')  # Claude blinks the ● of a running tool, so half the reads show a space
CALL = re.compile(r'^[● ] ([A-Z]\w*)\((.*)$')  # "● Bash(command…": the call shape of a running tool
CALL_TIME = re.compile(r'^\s+\((\d[\dhms ]*\S)(?: · timeout [^)]*)?\)$')  # "(1m 43s · timeout 8m 20s)" under its output
DENIED = re.compile(r'\d+ actions? not run · permission denied')
HEADS_UP = re.compile(r'^✦ (.+)$')
CHOICE = re.compile(r'(\d):\s+(.+?)(?=\s{2,}\d:|$)')
PICKER_ROW = re.compile(r'^❯\s*\d+\.\s')
PICKER_KEYS = re.compile(r'Enter to (?:select|confirm)|↑/↓ to navigate', re.I)
MORE = re.compile(r'^\s*↓ (\d+) more\b')
URL = re.compile(r'https?://\S+')
# The keys each card state takes (from the Claude Code UI), for when the card is taller than the screen and its key line is cut off.
CARD_KEYS = {
    'offer': [('1', 'Learn more'), ('2', 'Knew this already'), ('0', 'Dismiss')],
    'explained': [('1', 'Understood'), ('2', 'Chat in main session'), ('0', 'Dismiss')],
    'ready': [('1', 'Helpful'), ('2', 'Not helpful'), ('3', 'Didn’t read')],
}
AGENT = re.compile(r'^\s*([●◯○])\s+(\S+)(?:\s{2,}(.*?))?(?:\s{2,}(\d[\dhms ]*\S.*))?\s*$')
CODEX_CONTEXT = re.compile(r'\bContext\s+(\d+(?:\.\d+)?)%\s+(left|used)\b|(\d+(?:\.\d+)?)%\s+context\s+(left|used)\b', re.I)
CODEX_MODEL = re.compile(r'^\s*([\w.-]+)(?:\s+(minimal|low|medium|high|xhigh|extra high|max))?\s+·', re.I)


def codex_screen(screen):
    """Only the bottom status line, never percentages quoted in conversation text."""
    for line in reversed(ANSI_SGR.sub('', screen).splitlines()):
        context, model = CODEX_CONTEXT.search(line), CODEX_MODEL.match(line)
        if not model and not (context and re.match(r'^\s*(?:Context\s+)?\d', line, re.I)):
            continue
        result = {'model': model[1]} if model else {}
        if context:
            value, direction = float(context[1] or context[3]), context[2] or context[4]
            result['context'] = {'percent': max(0, min(100, 100 - value if direction.lower() == 'left' else value))}
        if model and model[2]:
            result['effort'] = model[2].lower()
        credits = re.search(r'\bcredits(?: balance)?\s*:?\s+(\d+(?:\.\d+)?|unlimited)\b', line, re.I)
        if credits:
            result['credits'] = {'unlimited': True} if credits[1].lower() == 'unlimited' else {'balance': credits[1]}
        result['windows'] = [{'label': '5h' if match[1].lower() == '5h' else '168h',
                              'percent': 100 - float(match[2]) if (match[3] or '').lower() == 'left' else float(match[2]), 'resets_at': None}
                             for match in re.finditer(r'\b(5h|7d|weekly)\s+(\d+(?:\.\d+)?)%\s*(left|used)?', line, re.I)]
        return result
    return {}


def codex(report):
    """Expose Codex metrics through the same status/effort strip as Claude."""
    result = {key: report[key] for key in ('model', 'effort', 'credits') if report.get(key) not in (None, '', {})}
    context = report.get('context') or {}
    if context.get('percent') is not None:
        result['context'] = context
    result['windows'] = [window for window in report.get('windows', []) if window.get('percent') is not None]
    parts = [report['model']] if report.get('model') else []
    if context.get('percent') is not None:
        parts.append(f"ctx {context['percent']:.0f}%")
    result['status'] = ' | '.join(parts)
    return result


def parse(styled):
    lines = [line.rstrip() for line in styled.splitlines()]
    plain = [ANSI_SGR.sub('', line) for line in lines]
    # Claude's input box: a ❯ line right under a rule. A sent message in the chat history also starts with ❯, but has no rule above it.
    # When a dialog (a permission prompt) replaces the input box, no line qualifies, and the dialog is not read as the footer.
    prompts = [i for i, line in enumerate(plain) if line.startswith('❯') and i and RULE.match(plain[i - 1])]
    result = {'suggestion': '', 'notices': [], 'effort': '', 'mode': '', 'status': '', 'agents': [], 'update': '',
              'goal': '', 'tasks': [], 'ide': '', 'spinner': None, 'running': None, 'denied': None, 'heads_up': None}
    if not prompts:
        return result
    at = prompts[-1]
    # A picker (AskUserQuestion, /model) marks its cursor row with ❯ too. Its rows and key hints are not the footer.
    if PICKER_ROW.match(plain[at]) and any(PICKER_KEYS.search(line) for line in plain[at:]):
        return result
    text = dim_prompt_text('\n'.join(lines[at:at + 1]))
    if text and text != 'Press up to edit queued messages' and not text.startswith('Try '):
        result['suggestion'] = text
    # Right-aligned lines just above the top rule: notices and the effort badge.
    top = at - 1 if at and RULE.match(plain[at - 1]) else at
    above = []
    for line in reversed(plain[max(0, top - 4):top]):
        if not line.strip() or not line.startswith(' ' * 12):
            break
        above.insert(0, line.strip())
    for item in above:
        effort = EFFORT.search(item)
        if effort and '/effort' in item:
            result['effort'] = effort[1].lower()
        else:
            # "◎ /goal active (10h) · ✔ Update installed · /restart": a new notice starts at a symbol after " · ".
            result['notices'] += [part.strip() for chunk in re.split(r'\s{3,}', item) for part in re.split(r' · (?=[^\x00-\x7f])', chunk) if part.strip()]
    # Under the bottom rule: status line, mode line, agents.
    below = plain[at + 1:]
    while below and not RULE.match(below[0]):
        below = below[1:]
    for line in below[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        agent = AGENT.match(line)
        if agent and agent[2] != '❯':
            result['agents'].append({'name': agent[2], 'task': (agent[3] or '').strip(), 'detail': (agent[4] or '').strip(), 'main': agent[1] == '●'})
        elif stripped.startswith(('⏵', '⏸')) or 'shift+tab' in stripped or re.search(r'\bmode on\b', stripped):
            mode, *tasks = re.sub(r'\s*\(shift\+tab to cycle\)', '', stripped).split(' · ')
            # "⧉ …" is the IDE context (open file or selected lines), not background work.
            result['mode'] = mode
            result['tasks'] = [task.strip() for task in tasks if task.strip() and not task.strip().startswith('⧉')]
            result['ide'] = next((task.replace('⧉', '').strip() for task in tasks if task.strip().startswith('⧉')), '')
        elif not result['status']:
            result['status'] = stripped
        else:
            result['notices'] += [part.strip() for part in re.split(r'\s{3,}', stripped) if part.strip()]
    goal = next((GOAL.search(item) for item in result['notices'] if GOAL.search(item)), None)
    if goal:
        result['goal'] = goal[1] or 'active'
        result['notices'] = [item for item in result['notices'] if not GOAL.search(item)]
    transcript(plain[:top], result)
    for item in result['notices'] + [result['status'], result['mode']]:
        if UPDATE.search(item):
            result['update'] = item
            break
    return result


MODES = ('default', 'accept', 'plan', 'auto', 'bypass')


def mode_key(mode):
    """'⏵⏵ accept edits on' -> 'accept'; no mode line means Claude's default mode."""
    text = (mode or '').lower()
    return next((key for key in MODES[1:] if key in text), 'default')


def set_mode(read, press, target, sleep=time.sleep, tries=6):
    """Press Shift+Tab until the footer shows the target mode. read() returns the footer; press() sends one Shift+Tab.
    Waits for each press to land, so fast taps on the phone cannot skip past the mode you asked for."""
    if target not in MODES:
        raise ValueError('Unknown mode.')
    current = mode_key(read().get('mode'))
    seen = [current]
    for _ in range(tries):
        if current == target:
            return {'mode': target}
        press()
        for _ in range(12):
            sleep(.15)
            now = mode_key(read().get('mode'))
            if now != current:
                break
        current = now
        if current in seen:
            break  # went all the way round
        seen.append(current)
    if current == target:
        return {'mode': target}
    raise RuntimeError(f'{target.capitalize()} mode is not offered in this session. Modes found: {", ".join(seen)}.')


def heads_up(lines):
    """One ✦ card from Claude Code's suggestion feature, in any state:
    offer (Learn more / Knew this already / Dismiss), making ("Making your page…", no keys), ready (a page link),
    explained (sections under the headline), asked, failed ("Couldn't …" with 0: OK).
    The card must be the last thing above the input box; an older card higher up is history, not a question."""
    title, body, detail, options, more, end, in_detail = lines[0][2:].strip(), [], [], [], 0, len(lines), False
    inline = re.search(r'(?:\s+·)?\s+(?=\d:\s)', title)  # "Couldn't write that page · 0: OK"
    if inline:
        title, options = title[:inline.start()], [{'key': k, 'label': v.strip()} for k, v in CHOICE.findall(title[inline.end():])]
        lines = lines[:1]
    for offset, follow in enumerate(lines[1:], 1):
        if re.match(r'\s*\d:\s', follow):
            options = [{'key': key, 'label': label.strip()} for key, label in CHOICE.findall(follow.strip())]
            end = offset + 1
            break
        found = MORE.match(follow)
        if found:
            more, end = int(found[1]), offset + 1
            break
        if follow.startswith(' ' * 12) or (follow.strip() and not follow.startswith('  ')):
            end = offset
            break
        if not follow.strip():
            in_detail = True  # the headline ends at the first blank line; sections follow
            if detail and detail[-1]:
                detail.append('')
            continue
        if in_detail and detail and detail[-1] and follow.startswith('    '):
            detail[-1] += ' ' + follow.strip()  # a wrapped bullet
        else:
            (detail if in_detail else body).append(follow.strip())
    rest = lines[end:]
    if any(line.strip() and not line.startswith(' ' * 12) and not MORE.match(line) for line in rest):
        return None
    head = ' '.join([title] + body)
    state = ('making' if head.startswith(('Making your page', 'One moment')) else 'ready' if head.startswith('Learning page ready')
             else 'failed' if head.startswith(('Couldn', 'Not published')) else 'explained' if detail else 'offer')
    if not options and more and state in CARD_KEYS:
        options = [{'key': key, 'label': label} for key, label in CARD_KEYS[state]]
    link = URL.search(' '.join([head] + detail))
    while detail and not detail[-1]:
        detail.pop()
    if not options and state not in ('making', 'failed'):
        return None
    return {'text': head, 'state': state, 'detail': detail, 'url': link[0].rstrip('.,)') if link else None, 'more': more, 'options': options}


def transcript(plain, result):
    """The live part of the screen above the input: the spinner, a running tool call and a Heads up card."""
    lines = plain[-40:]
    skip_to = 0
    for index, line in enumerate(lines):
        spinner = SPINNER.match(line)
        if spinner:
            result['spinner'] = {'verb': spinner[1], 'detail': spinner[2]}
        if index >= skip_to:
            found = running_tool(lines, index)
            if found:
                result['running'], skip_to = found
        denied = DENIED.search(line)
        if denied:
            reason = next((more.strip()[len('Reason:'):].strip() for more in lines[index + 1:index + 3] if more.strip().startswith('Reason:')), '')
            result['denied'] = {'text': denied[0], 'reason': reason}
        if HEADS_UP.match(line):
            card = heads_up(lines[index:])
            if card:
                result['heads_up'] = card
    if not result['heads_up']:
        result['heads_up'] = clipped_card(lines)
    # A finished run leaves no spinner with an ellipsis, so a stale "Running" line is dropped with it.
    if not result['spinner']:
        result['running'] = None
        result['denied'] = None



def running_tool(lines, index):
    """A running tool call that starts at lines[index]: its title, time and command, and the index after its title.
    A narrow pane wraps the title ("● Testing the wrapper with no" / "  output · 19s"), so up to two more lines join it.
    A permission note ("⎿  1 action not run · permission denied") can come before the "⎿  $ command" line."""
    line = lines[index]
    call = CALL.match(line)
    if call:
        return running_call(lines, index, call)
    if not (line.startswith('● ') or (line.startswith('  ') and len(line) > 2 and line[2] != ' ' and (not index or not lines[index - 1].strip()))):
        return None
    head, end = line, index + 1
    while not RUNNING.match(head) and end < min(len(lines), index + 3) and lines[end].startswith('  ') and lines[end][2:3].strip() and '⎿' not in lines[end]:
        head += ' ' + lines[end].strip()
        end += 1
    running = RUNNING.match(head)
    if not running or end >= len(lines) or not lines[end].lstrip().startswith('⎿'):
        return None
    notes = [at for at in range(end, min(len(lines), end + 6)) if lines[at].lstrip().startswith('⎿')]
    at = next((at for at in notes if lines[at].split('⎿', 1)[1].strip().startswith('$')), notes[0])
    command = lines[at].split('⎿', 1)[1].strip()
    # A long command wraps onto indented lines below; join up to two of them, stopping at Claude's own hints.
    for more in lines[at + 1:at + 3]:
        if not more.startswith('    ') or not more.strip() or more.strip().startswith(('(ctrl+', '⎿', 'Reason:')):
            break
        command += ' ' + more.strip()
    return {'title': running[1], 'elapsed': running[2], 'command': command.removeprefix('$').strip()}, end


def running_call(lines, index, call):
    """The call shape: "● Bash(command" (wrapped onto indented lines, ending in ")"), "⎿  output", then a time line
    "(6s)" or "(1m 43s · timeout 8m 20s)". Only a running call has the time line."""
    command, end = call[2], index + 1
    while end < min(len(lines), index + 6) and lines[end].startswith('      ') and not command.endswith(')'):
        command += ' ' + lines[end].strip()
        end += 1
    if not command.endswith(')') or end >= len(lines) or not lines[end].lstrip().startswith('⎿'):
        return None
    for at in range(end, min(len(lines), end + 12)):
        if not lines[at].strip() or (at > end and not lines[at].startswith('    ')):
            return None
        elapsed = CALL_TIME.match(lines[at])
        if elapsed:
            return {'title': call[1], 'elapsed': elapsed[1], 'command': command[:-1].strip()}, end
    return None


UP_MORE = re.compile(r'^\s*↑ (\d+) more\b')


def clipped_card(lines):
    """An explained ✦ card taller than the screen: Claude Code scrolls it, so the ✦ headline is gone and a
    "↑ N more" line sits under the keys. Keep the visible part; card_note adds the full text from the store."""
    rows = [line for line in lines if line.strip()]
    if not rows:
        return None
    last = len(rows) - 1
    above = UP_MORE.match(rows[last])
    keys = rows[last - 1] if above else rows[last]
    choices = CHOICE.findall(keys.strip()) if re.match(r'\s*\d:\s', keys) else []
    if [label.strip() for _, label in choices] != [label for _, label in CARD_KEYS['explained']]:
        return None
    end = lines.index(keys)
    start = end
    while start and not (lines[start - 1].strip() and not lines[start - 1].startswith('  ')):
        start -= 1
    detail = []
    for follow in lines[start:end]:
        text = re.sub(r'\s+\[[-+]\]$', '', follow.strip())  # the collapse toggle at the right edge
        if not text:
            if detail and detail[-1]:
                detail.append('')
        elif follow.startswith('    ') and detail and detail[-1]:
            detail[-1] += ' ' + text  # a wrapped bullet
        else:
            detail.append(text)
    while detail and not detail[-1]:
        detail.pop()
    return {'text': 'Heads up', 'state': 'explained', 'detail': detail, 'url': None, 'more': int(above[1]) if above else 0,
            'options': [{'key': key, 'label': label.strip()} for key, label in choices], 'clipped': True}


CARD_STORE = Path.home() / '.claude' / 'plugins' / 'store'


def card_note(card, thread, store=None):
    """Add the full explanation of an explained ✦ card, from Claude Code's own store (read-only), and the note that
    "Chat in main session" would put in the input box: the screen shows only part of a long explanation."""
    if not card or card.get('state') != 'explained' or not thread:
        return card
    for path in sorted((store or CARD_STORE).glob('cc-plugin-you-should-know_*.json')):
        try:
            sessions = json.loads(path.read_text()).get('sessions') or []
        except (OSError, ValueError, AttributeError):
            continue
        entry = next((item for item in sessions if isinstance(item, dict) and item.get('id') == thread), None)
        view = (entry or {}).get('view')
        if not isinstance(view, dict) or view.get('kind') != 'explained' or not isinstance(view.get('text'), str):
            continue
        line = view.get('line') or ''
        if card.get('clipped'):
            card = dict(card, text=f"{entry.get('tag') or 'Heads up'} · {line}")
        elif line[:40] not in card['text']:
            continue  # the store moved on to another card
        quoted = '\n'.join('>' if not row else '> ' + row for row in f"{entry.get('tag') or 'Heads up'} · {line}\n\n{view['text'].strip()}".split('\n'))
        return dict(card, full=view['text'].strip(), note=f'Here is a note offered by a side agent:\n\n{quoted}\n\n')
    return card
