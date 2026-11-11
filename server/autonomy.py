#!/usr/bin/env python3
"""User-set autonomy level and reviewer choices.

  paths.CONFIG/autonomy-level               global level, 0-5. Missing or bad: 2.
  paths.TETHER/projects/<id>/autonomy-level  {"override": bool, "level": int}.
  paths.CONFIG/reviewers.json                critical, normal and light reviewer choices.

`autonomy.py hook` supplies UserPromptSubmit context. `autonomy.py show` prints it for cwd.
"""
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import codex_level  # noqa: E402
import delegate  # noqa: E402
import paths  # noqa: E402
from codex_level import hook_project  # noqa: E402

LEVELS = (
    ('Check-in', 'Confirms the plan first, stops at each milestone, asks at each unclear choice.'),
    ('Guided', 'Confirms the plan, then works. Asks at real forks.'),
    ('Standard', 'Asks when a decision is material and has no sensible default.'),
    ('Trusted', 'Uses sensible defaults. Asks its questions together at the end of the turn.'),
    ('Autopilot', 'Does not ask about normal decisions. Lists its assumptions. Asks only for new authority.'),
    ('Full auto', 'Runs the task alone, opens its own tabs and reviewers, works the backlog. Asks only when blocked; '
     'urgent asks reach the user at once.'),
)
RULES = (
    'Write the plan and wait for the user to approve it before you start. Stop at each milestone with a short status '
    'and wait for the user. Ask about each unclear choice that changes the result. Do not ask about trivial details.',
    'Write the plan and wait for the user to approve it, then work without stopping. Ask only at real forks, where '
    'the choice changes the result.',
    'Ask only when a decision is material and you cannot resolve it from the request, the code or a sensible default. '
    'Otherwise continue.',
    'Use sensible defaults and continue. Do not stop mid-task to ask. Ask your open questions together at the end of '
    'the turn, each with the default you used.',
    'Do not ask about normal decisions. Choose, list each assumption in the final reply, and continue until the work '
    'is done and verified. Ask only when you need new authority or a decision that only the user can make.',
    'Work alone until the task is done and verified. Do not ask about normal decisions; list your assumptions in the '
    'final reply. Split large work into new tabs and start reviewers with agent_tab_cli.py. When the task is done, '
    'start the next open backlog item of this project, if there is one. Ask only when you are blocked: the task cannot '
    'continue without the user, or a critical bug needs a decision.',
)
SAFETY = ('At every level, ask before you delete data, force-push, run a destructive migration, spend money, make a '
          'public release, or send a message to another person.')
URGENT = ('When you must ask, end the reply with "Needs you: yes". If the question blocks the work, put a line '
          '"Urgent: <the question in one sentence>" just before it.')
DECISIONS = ('Before you ask the user, read {path}. If a past decision answers your question, use it and say so.')
LAST = 'Do not change the level. The user sets it in Tether.'
PROJECT_ID = codex_level.PROJECT_ID
SLUG = codex_level.SLUG
TIERS = ('critical', 'normal', 'light')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
CLAUDE_REVIEW_MODELS = tuple(model for model, _ in delegate.CLAUDE_MODELS)  # what a new tab accepts
TIER_DEFAULT = {'agent': 'auto', 'model': 'auto', 'effort': 'auto'}


class LevelError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ---- the files ----

def global_path():
    return paths.CONFIG / 'autonomy-level'


def project_path(pid):
    if not isinstance(pid, str) or not PROJECT_ID.fullmatch(pid):
        raise LevelError(400, 'Choose a project.')
    return paths.TETHER / 'projects' / pid / 'autonomy-level'


def _valid(level):
    return isinstance(level, int) and not isinstance(level, bool) and 0 <= level < len(LEVELS)


def read_global():
    try:
        text = global_path().read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return 2
    return int(text) if re.fullmatch(r'[0-5]', text) else 2


def read_project(pid):
    """{'override': bool, 'level': int}, or None when the project has no usable file."""
    try:
        data = json.loads(project_path(pid).read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError, LevelError):
        return None
    if not isinstance(data, dict) or not _valid(data.get('level')) or not isinstance(data.get('override'), bool):
        return None
    return {'override': data['override'], 'level': data['level']}


def _write(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        temp.write_text(text, encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def set_global(level):
    if not _valid(level):
        raise LevelError(400, f'Choose a level from 0 to {len(LEVELS) - 1}.')
    _write(global_path(), f'{level}\n')


def set_project(pid, override, level):
    if not isinstance(override, bool) or not _valid(level):
        raise LevelError(400, f'Choose a level from 0 to {len(LEVELS) - 1}.')
    _write(project_path(pid), json.dumps({'override': override, 'level': level}) + '\n')


def resolve(pid=None):
    """An enabled project override beats the global level, including a lower level."""
    base = read_global()
    project = read_project(pid) if pid else None
    if project and project['override']:
        return {'level': project['level'], 'source': 'project', 'global': base, 'project': project}
    return {'level': base, 'source': 'global', 'global': base, 'project': project}


def rules(resolved):
    level = resolved['level']
    where = 'for this project' if resolved['source'] == 'project' else 'for all projects'
    head = f'Autonomy level {level} of {len(LEVELS) - 1} ({LEVELS[level][0]}), set by the user {where}.'
    lines = [head, RULES[level], SAFETY]
    if level >= 4:
        lines.append(URGENT)
    decisions = paths.CONFIG / 'decisions.md'
    if level >= 3 and decisions.exists():
        lines.append(DECISIONS.format(path=decisions.resolve()))
    lines.append(LAST)
    return '\n'.join(lines)


# ---- reviewer choice ----

def reviewers_path():
    return paths.CONFIG / 'reviewers.json'


def _valid_reviewers(reviewers):
    if not isinstance(reviewers, dict) or set(reviewers) != set(TIERS):
        return False
    available = None
    for tier in reviewers.values():
        if not isinstance(tier, dict) or set(tier) != set(TIER_DEFAULT):
            return False
        agent, model, effort = tier['agent'], tier['model'], tier['effort']
        if (agent not in ('auto', 'claude', 'codex', 'none') or effort not in ('auto', *EFFORTS)
                or not isinstance(model, str) or not (model == 'auto' or SLUG.fullmatch(model))):
            return False
        if agent == 'none' and (model != 'auto' or effort != 'auto'):
            return False
        if agent == 'auto' and model != 'auto':
            return False
        if agent == 'claude' and model != 'auto' and model not in CLAUDE_REVIEW_MODELS:
            return False
        if agent == 'codex' and model != 'auto':
            if available is None:
                available = [row['slug'] for row in codex_level.models()]
            if available and model not in available:
                return False
    return True


def read_reviewers():
    try:
        reviewers = json.loads(reviewers_path().read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError):
        return {tier: dict(TIER_DEFAULT) for tier in TIERS}
    return reviewers if _valid_reviewers(reviewers) else {tier: dict(TIER_DEFAULT) for tier in TIERS}


def set_reviewers(reviewers):
    if not _valid_reviewers(reviewers):
        raise LevelError(400, 'Choose a reviewer, a model and an effort for each tier.')
    _write(reviewers_path(), json.dumps(reviewers) + '\n')


def reviewer_rules(reviewers):
    lines = [
        'Choose a reviewer by the risk of the task:',
        '- Critical: security, auth, payments, data loss, migrations, deploys, public releases, concurrency, or a '
        'change of more than about 300 lines. Start a reviewer tab from the other agent (Claude reviews Codex work, '
        'Codex reviews Claude work) with the strongest model and high effort.',
        '- Normal: features and bug fixes. Start a reviewer tab from the other agent with its default model and '
        'medium effort.',
        '- Light: docs, copy, styles and small fixes. Do not start a reviewer tab. Review the change yourself.',
        f'Start a reviewer with `python3 {HERE / "agent_tab_cli.py"} start --reviews-me --agent AGENT '
        '[--model MODEL] [--effort EFFORT]`.',
    ]
    for name in TIERS:
        tier = reviewers[name]
        if tier == TIER_DEFAULT:
            continue
        if tier['agent'] == 'none':
            lines.append(f'The user set the {name.title()} reviewer: none. Do not start a reviewer for {name} tasks.')
        elif tier['agent'] == 'auto':
            lines.append(f'The user set the {name.title()} reviewer effort: {tier["effort"]}.')
        else:
            parts = ['Claude' if tier['agent'] == 'claude' else 'Codex']
            parts += [f'{key} {tier[key]}' for key in ('model', 'effort') if tier[key] != 'auto']
            lines.append(f'The user set the {name.title()} reviewer: {", ".join(parts)}. This overrides the default.')
    return '\n'.join(lines)


# ---- the hook ----

def hook_output(data, env=os.environ, tmux=codex_level._tmux):
    """The hook's stdout for one UserPromptSubmit input, as a JSON string."""
    cwd = data.get('cwd') if isinstance(data, dict) else None
    try:
        pid = hook_project(cwd, env, tmux)
    except (OSError, subprocess.SubprocessError):
        pid = None
    text = rules(resolve(pid)) + '\n\n' + reviewer_rules(read_reviewers())
    return json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': text}})


def hook():
    """Fail open: a hook must never stop a prompt."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        out = hook_output(data)
    except Exception:  # noqa: BLE001 - a hook must never stop a prompt
        return
    print(out)


# ---- HTTP (/mobile/api/autonomy). server.py checks sign-in, Origin and X-Codex-Mobile first. ----

PATH = '/mobile/api/autonomy'


def handles(path):
    return path.split('?', 1)[0] == PATH


def state(pid=None):
    resolved = resolve(pid)
    models = [{'slug': row['slug'], 'name': row['name'],
               'efforts': [effort for effort in row['efforts'] if effort in EFFORTS]} for row in codex_level.models()]
    return {'levels': [{'level': index, 'name': name, 'summary': summary} for index, (name, summary) in enumerate(LEVELS)],
            'global': resolved['global'], 'level': resolved['level'], 'source': resolved['source'],
            'project': resolved['project'], 'reviewers': read_reviewers(), 'tiers': list(TIERS),
            'models': {'claude': [{'slug': slug, 'name': name} for slug, name in delegate.CLAUDE_MODELS], 'codex': models}, 'efforts': list(EFFORTS)}


def get(handler):
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(handler.path).query)
    pid = query.get('project', [None])[0]
    try:
        if pid is not None:
            project_path(pid)
        handler.respond(200, state(pid))
    except LevelError as exc:
        handler.respond(exc.status, {'error': str(exc)})


def post(handler):
    """Set the global level, a project override, or the full reviewer choices."""
    try:
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= 4096:
            raise LevelError(413, 'Invalid request.')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise LevelError(400, 'Invalid request.')
        pid = body.get('project')
        if 'reviewers' in body:
            set_reviewers(body['reviewers'])
        elif pid is None:
            set_global(body.get('level'))
        else:
            set_project(pid, body.get('override'), body.get('level'))
        handler.respond(200, state(pid))
    except LevelError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, UnicodeError):
        handler.respond(400, {'error': 'Invalid request.'})
    except OSError:
        handler.respond(500, {'error': 'Tether could not save the autonomy settings.'})


if __name__ == '__main__':
    if sys.argv[1:] == ['hook']:
        hook()
    elif sys.argv[1:] == ['show']:
        print(rules(resolve(hook_project(os.getcwd()))) + '\n\n' + reviewer_rules(read_reviewers()))
    else:
        sys.exit(__doc__)
