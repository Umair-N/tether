"""One line per pane that waits on the user, for the desktop "Needs you" list.

It reads only the status the session list already has (live_status), so it costs no extra tmux or file reads.
"""
import re

import detection_rules
from terminal_input import plan_menu

LIMIT = 160
# Box and rule characters that Claude and Codex draw around their dialogs.
FRAME = re.compile(r'^[\s│┃╭╮╰╯┌┐└┘─━═╌╍┄┅|]+|[\s│┃╭╮╰╯┌┐└┘─━═╌╍┄┅|]+$')
GENERIC = detection_rules.pattern('common', 'GENERIC')


def _lines(text):
    out = []
    for line in (text or '').splitlines():
        line = FRAME.sub('', line).strip()
        if line and not re.fullmatch(r'[-_=─━═]{3,}', line) and not GENERIC.match(line):
            out.append(re.sub(r'\s+', ' ', line))
    return out


def _clip(text):
    return text if len(text) <= LIMIT else text[:LIMIT - 1].rstrip() + '…'


def summary(status):
    """{'kind': 'approval'|'plan'|'question', 'text': one line} for a pane that waits on the user, else None.

    approval: a permission request. Its first two lines name the tool and the command ("Bash command · rm -r build").
    plan: Claude's plan chooser. The text is the plan's first heading.
    question: an open menu or prompt. The text is the question, else the last line above the input.
    """
    status = status or {}
    state = status.get('state')
    pending = status.get('approval') if isinstance(status.get('approval'), dict) else None
    attention = status.get('attention') if isinstance(status.get('attention'), dict) else None
    if state == 'Approval' and pending:
        lines = _lines(pending.get('text'))
        if plan_menu(pending):
            heading = next((line.lstrip('# ').strip() for line in lines if line.startswith('#')), None)
            return {'kind': 'plan', 'text': _clip(heading or next(iter(lines), 'Review the plan'))}
        return {'kind': 'approval', 'text': _clip(' · '.join(lines[:2]) or 'Waiting for your permission')}
    if state == 'Input needed' and attention:
        if plan_menu(attention):
            return {'kind': 'plan', 'text': _clip(attention.get('question') or 'Review the plan')}
        question = (attention.get('question') or '').strip()
        if not question:
            lines = [line for line in _lines(attention.get('text')) if not re.match(r'^[>❯›]?\s*\d+[.)]\s', line)]
            question = next((line for line in reversed(lines) if line.endswith('?')), lines[-1] if lines else '')
        return {'kind': 'question', 'text': _clip(re.sub(r'\s+', ' ', question) or 'Waiting for your answer')}
    return None
