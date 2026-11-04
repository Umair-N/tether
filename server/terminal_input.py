"""Guarded input and visible permission prompts for live agent tmux panes."""
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import shlex
import sqlite3
import subprocess
import time
import uuid

from antigravity import process_start, ROOT as AGY_ROOT, CACHE as AGY_CACHE, capture_notices
from quota_notices import capture as capture_quota_notices
from codex_provider import PROFILE_NAME
import login_flow
import paths
import window_size

LAUNCHERS = {'codex': 'codex',
             'claude': 'claude',
             'antigravity': 'agy'}


# tmux allows spaces in session names. It turns . and : into _, so they are refused here instead.
SESSION_NAME = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9 _-]{0,62}[A-Za-z0-9_-])?')
NAME_HELP = 'Use letters, numbers, spaces, - or _ (up to 64), starting with a letter or number.'


def valid_name(name):
    return isinstance(name, str) and bool(SESSION_NAME.fullmatch(name))


def create_session(name, cwd, run=subprocess.run):
    if not valid_name(name):
        raise ValueError(NAME_HELP)
    result = run([*paths.tmux_new_session(), 'new-session', '-d', '-s', name, '-c', cwd, '-P', '-F', '#{pane_id}'],
                 capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError('A session with that name already exists.' if 'duplicate session' in result.stderr
                           else 'Could not create the tmux session.')
    pane = result.stdout.strip()
    if not re.fullmatch(r'%\d+', pane):
        raise RuntimeError('Session creation could not be confirmed. Refresh the tabs before retrying.')
    return pane


def resume_session(name, cwd, identifier, run=subprocess.run):
    pane = create_session(name, cwd, run=run)
    # The pty buffers input even while the new shell starts.
    run([paths.TMUX, 'send-keys', '-t', pane, '-l', 'claude --resume ' + identifier],
        check=True, capture_output=True, timeout=5)
    run([paths.TMUX, 'send-keys', '-t', pane, 'Enter'],
        check=True, capture_output=True, timeout=5)
    return pane


UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def fork_name(entry, entries):
    """The fork's name: the chat's name (agent, then pane, then tmux session) plus the next free number in that session."""
    base = ' '.join((entry.get('agent_name') or entry.get('pane_name') or entry.get('label') or 'chat').split())
    base = re.sub(r' \d+$', '', base)[:34].strip() or 'chat'
    taken = {other.get('agent_name') or other.get('pane_name') for other in entries if other.get('tmux_session') == entry.get('tmux_session')}
    number = 2
    while f'{base} {number}' in taken:
        number += 1
    return f'{base} {number}'


def fork_chat(entry, entries, cwd, run=subprocess.run):
    """Open a new window in the pane's tmux session with a copy of its chat: `claude --resume ID --fork-session` or `codex fork ID`.
    The original chat is not touched. Returns (pane, name)."""
    agent, thread = entry.get('agent'), entry.get('thread')
    if agent not in ('claude', 'codex'):
        raise ValueError('Only Claude and Codex chats can be copied to a new chat.')
    if not isinstance(thread, str) or not UUID.fullmatch(thread) or entry.get('pending_conversation'):
        raise ValueError('Send a first message in this chat, then try again.')
    name = fork_name(entry, entries)
    result = run([paths.TMUX, 'new-window', '-a', '-t', entry['id'], '-c', cwd, '-P', '-F', '#{pane_id}'],
                 capture_output=True, text=True, timeout=5)
    pane = result.stdout.strip()
    if result.returncode or not re.fullmatch(r'%\d+', pane):
        raise RuntimeError('Could not open a new window in this tmux session.')
    # A copy keeps the provider of the chat it copies (codex -p foundry), whatever new panes use now.
    profile = entry.get('codex_profile') if agent == 'codex' else None
    command = (f"claude --resume {thread} --fork-session --name {shlex.quote(name)}" if agent == 'claude'
               else f"codex fork -p {profile} {thread}" if isinstance(profile, str) and PROFILE_NAME.fullmatch(profile)
               else f"codex fork {thread}")
    run([paths.TMUX, 'set-option', '-p', '-t', pane, '@chat_name', name, ';', 'set-option', '-p', '-t', pane, '@chat_name_at', str(int(time.time() * 1000))],
        check=True, capture_output=True, timeout=5)
    # The pty buffers input even while the new shell starts.
    run([paths.TMUX, 'send-keys', '-t', pane, '-l', command], check=True, capture_output=True, timeout=5)
    run([paths.TMUX, 'send-keys', '-t', pane, 'Enter'], check=True, capture_output=True, timeout=5)
    return pane, name


# OSC sequences such as terminal links (ESC ]8;id=…;file:///… ESC \\). A styled capture keeps them; only colors are wanted.
OSC = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')


def capture(entry, run=subprocess.run, styled=False):
    text = run([paths.TMUX, 'capture-pane', *(['-e'] if styled else []), '-p', '-t', entry['id']],
               check=True, capture_output=True, text=True, timeout=5).stdout
    return OSC.sub('', text) if styled else text


def capture_screen(entry, lines=80, run=subprocess.run):
    """The last `lines` lines of the pane (scrollback included), for the read-only live screen view."""
    lines = max(20, min(int(lines), 500))
    text = run([paths.TMUX, 'capture-pane', '-p', '-t', entry['id'], '-S', '-' + str(lines)],
               check=True, capture_output=True, text=True, timeout=5).stdout
    return '\n'.join(line.rstrip() for line in text.rstrip().splitlines()[-lines:])[-200000:]


COUNTDOWN = re.compile(r'(block\s+this\s+request\s+in\s+)(?:\d+[:hms.]?\s?)+', re.I)
DIALOG_FRAME = re.compile(r'^\s*(?:[─━═╌╍┄┅\-_]{3,}|Do you want to proceed\?)\s*$')


def approval(screen):
    # Expose the native chooser; every submitted choice requires an explicit
    # user selection tied to this exact request. Never auto-accept a default.
    lines = screen.splitlines()
    questions = [i for i, line in enumerate(lines) if re.search(
        r'^\s*(?:[│┃╭╰┌└]\s*)?(?:Claude has written up a plan.*Would you like to proceed|Ready to code\?|Quick safety check|Run this command\?|Would you like to \w+|Do you want to (?:proceed|allow|approve|make this edit|create)|Do you trust|Trust this (?:folder|directory|project)|Allow access to|Allow this|(?:trust.{0,100}(?:folder|directory|project)|(?:folder|directory|project).{0,100}trust))', line, re.I)]
    if not questions:
        return None
    start = questions[-1]
    trust = bool(re.search(r'Quick safety check|trust.*(?:folder|directory|project)|(?:folder|directory|project).*trust', lines[start], re.I))
    choices = []
    choice_numbers = []
    for i, line in enumerate(lines[start + 1:], start + 1):
        match = re.match(r'^\s*([>❯›]?)\s*(\d+)\.\s+(.+)', line)
        if match:
            choices.append((i, bool(match[1]), match[3]))
            choice_numbers.append(int(match[2]))
        elif trust and (match := re.match(r'^\s*([>❯›]?)\s*((?:Yes|No|Trust|Don.t trust|Do not trust|Exit)\b[^?]*)$', line)):  # capitalised, and not a wrapped question line
            choices.append((i, bool(match[1]), match[2]))
            choice_numbers.append(len(choices))
    reject = r'(?:No\b|Reject\b|Cancel\b|Exit\b|Don.t trust\b|Do not trust\b)'
    if not choices or not any(re.match(reject, c[2], re.I) for c in choices):
        return None
    selected = next((i for i, c in enumerate(choices) if c[1]), None)
    once = next((i for i, c in enumerate(choices) if re.match(r'Yes\b', c[2], re.I)
                 and not re.search(r'always|don.t ask|remember', c[2], re.I)), None)
    # Fingerprint includes the visible tool request, independent of cursor position.
    preceding = [i for i in range(start) if re.search(r'^\s*(?:Requesting permission for:|Command|\$ )', lines[i])]
    # Claude draws its dialog under a full rule: start there, so chat history above the dialog stays out of the card.
    tops = [i for i in range(max(0, start - 15), start) if re.match(r'^\s*\u2500{10,}\s*$', lines[i])]
    begin = preceding[-1] if preceding else tops[-1] + 1 if tops else max(0, start - 15)
    full_text = '\n'.join(lines[begin:choices[-1][0] + 1]).strip()
    # The card shows the request, not the dialog frame: drop rule lines and the generic question (the card title says it).
    shown = [line for line in lines[begin:choices[0][0]] if not DIALOG_FRAME.match(line)]
    text = re.sub(r'\n{3,}', '\n\n', '\n'.join(shown)).strip()
    options = []
    numbers = []
    for index, (line, _, label) in enumerate(choices):
        number = choice_numbers[index]
        numbers.append(number)
        end = choices[index + 1][0] if index + 1 < len(choices) else line + 1
        continuation = [part.strip() for part in lines[line + 1:end] if part.strip()]
        options.append({'id': number, 'text': ' '.join([label, *continuation])})
    canonical = re.sub(r'(?m)^\s*[>❯›]?\s*(\d+)\.\s+', r'\1. ', full_text)
    if trust:
        canonical = re.sub(r'(?m)^[ \t]*[>❯›]?[ \t]*((?:Yes|No|Trust|Don.t trust|Do not trust|Exit)\b[^?\n]*)$', r'\1', full_text)
    # Claude Code counts down before it blocks a request in an unattended session ("... block this request in 0:57, ...").
    # The time changes each second, so it stays out of the id: otherwise every tap would find a "changed" approval.
    canonical = COUNTDOWN.sub(r'\1-', canonical)
    return {'id': hashlib.sha256(canonical.encode()).hexdigest(), 'text': text,
            'actionable': selected is not None, 'options': options,
            '_numbers': numbers,
            '_selected': selected, '_once': once,
            '_reject': next(i for i, c in enumerate(choices) if re.match(reject, c[2], re.I))}


def plan_menu(pending):
    """Recognize the exit-plan chooser without mistaking a command approval for a plan."""
    options = (pending or {}).get('options') or []
    return (any(re.search(r'\bkeep planning\b|tell Claude what to change', option['text'], re.I) for option in options)
            and any(re.match(r'Yes\b', option['text'], re.I) for option in options))


ATTENTION_DELAY = 3
ATTENTION_GRACE = 10
_attention_seen = {}
NUMBERED_ITEM = re.compile(r'^\s*[>❯›]?\s*\d+[.)]\s+\S')
CURSOR_ITEM = re.compile(r'^\s*[>❯›]\s*\d+[.)]\s+\S')
# Chat output that an open menu never has below its cursor row: an agent line (● Checking…), the working spinner, a tool result.
CHAT_LINE = re.compile(r'^\s*[●⏺✻✶✳✢✽·]\s|^\s*⎿')


def cursor_in_menu(tail, marker):
    """True when a numbered row with a cursor is part of an open menu. A sent message that starts with a number
    ("❯ 1. Do what is compliant") looks the same in the chat history, but the chat goes on below it: agent lines,
    the spinner, or the empty input box. Below a real cursor row there are only options, details and key hints."""
    rows = [i for i, line in enumerate(tail) if CURSOR_ITEM.match(line)]
    if not rows:
        return False
    # A long option wraps in a narrow pane, and a wrapped line can start with "·" ("· API usage billing"). It sits at
    # or right of the option text. Agent lines and the spinner start left of it.
    label = re.match(r'^\s*[>❯›]\s*\d+[.)]\s+', tail[rows[-1]]).end()
    for line in tail[rows[-1] + 1:]:
        if CHAT_LINE.match(line) and len(line) - len(line.lstrip()) < label:
            return False
        if marker and line.startswith(marker) and not NUMBERED_ITEM.match(line):
            return False  # the input box: "❯ " with or without typed text
    return True
# Claude's /usage, /status, /config and /stats share one Settings dialog. /usage grows past a short pane once its scan ends,
# and then its "Esc to cancel" footer is off screen: the tab row at its top still shows it.
SETTINGS_TABS = re.compile(r'^\s*Settings\s+Status\s+Config\s+Usage\b')
INPUT_HINT = re.compile(r'esc Close|↑/↓ Scroll|Enter to (?:select|confirm|continue)|↑/?↓ (?:to )?(?:navigate|select)|enter Select|Esc to cancel|press enter|press any key|'
                        r'\(y/n\)|\[y/n\]|\(yes/no\)', re.I)


RULE_LINE = re.compile(r'^[\s─━│┃╭╮╰╯┌┐└┘├┤┬┴┼═║-]*$')
PICKER_CURSOR = re.compile(r'^\s*[❯›]\s+(\S.*)$')
PICKER_MORE = re.compile(r'^\s*([↑↓])\s+(\d+)\s+more\s*$')
PICKER_HINT = re.compile(r'^Enter to .+[·•].*\bEsc to \S', re.I)


def cursor_picker(screen):
    """Read an unnumbered cursor picker, bounded by its header and Enter/Esc footer."""
    lines = screen.rstrip().splitlines()
    # A narrow pane can wrap the footer. Stop as soon as both key instructions appear.
    hint = None
    for index in range(len(lines) - 1, -1, -1):
        if not re.match(r'^\s*Enter to\b', lines[index], re.I):
            continue
        for end in range(index + 1, min(index + 3, len(lines)) + 1):
            text = ' '.join(line.strip() for line in lines[index:end])
            if PICKER_HINT.search(text):
                hint = (index, end, text)
                break
        if hint:
            break
    if hint is None:
        return None
    index, end, hint_text = hint
    # An old picker in chat history must not hide the current input box or reply.
    if any(re.match(r'^\s*[❯›]', line) or CHAT_LINE.match(line) for line in lines[end:]):
        return None
    cursor = next((i for i in range(index - 1, -1, -1) if PICKER_CURSOR.match(lines[i])), None)
    if cursor is None or NUMBERED_ITEM.match(lines[cursor]):
        return None
    if any(CHAT_LINE.match(line) for line in lines[cursor + 1:index]):
        return None
    # Rows align with the cursor's label. A scroll count also marks the list's start.
    column = PICKER_CURSOR.match(lines[cursor]).start(1)
    first = cursor
    for i in range(cursor - 1, -1, -1):
        line = lines[i]
        if not line.strip():
            continue
        if PICKER_MORE.match(line):
            first = i
            break
        if RULE_LINE.match(line) or len(line) - len(line.lstrip()) < column:
            break
        first = i
    # The paragraph directly above the list is the panel header. Never take the
    # screen tail: it can include the conversation above this paragraph.
    top = first - 1
    while top >= 0 and not lines[top].strip():
        top -= 1
    bottom = top + 1
    while top >= 0 and lines[top].strip() and not RULE_LINE.match(lines[top]):
        top -= 1
    header = [line.strip() for line in lines[top + 1:bottom]]
    if not header or any(CHAT_LINE.match(line) or re.match(r'^[❯›]', line) for line in header):
        return None
    items, counts = [], {'↑': 0, '↓': 0}
    for line in lines[first:index]:
        if not line.strip() or RULE_LINE.match(line):
            continue
        if match := PICKER_MORE.match(line):
            counts[match[1]] = int(match[2])
        elif NUMBERED_ITEM.match(line):
            return None  # numbered menus keep their existing parser and response
        else:
            match = PICKER_CURSOR.match(line)
            items.append({'text': match[1].strip() if match else line.strip(), 'selected': bool(match)})
    if not items or sum(item['selected'] for item in items) != 1:
        return None
    return {'title': header[0], 'subtitle': ' '.join(header[1:]), 'items': items,
            'more_above': counts['↑'], 'more_below': counts['↓'], 'hint': hint_text,
            'text': '\n'.join(lines[top + 1:end]).strip()}


def menu_lines(lines, limit=80):
    """The lines of the menu at the bottom of the screen. Long options in a narrow pane push the question above
    the last 30 lines, so start at the rule line Claude draws above the question (at most `limit` lines up)."""
    lines = lines[-limit:]
    cursor = next((i for i in range(len(lines) - 1, -1, -1) if CURSOR_ITEM.match(lines[i])), None)
    if cursor is None:
        return lines[-30:]
    # Skip the rule between options ("Type something." / "Chat about this"): the top rule has the question below it.
    after = lambda i: next((line for line in lines[i + 1:] if line.strip()), '')
    top = next((i for i in range(cursor - 1, -1, -1) if lines[i].strip() and RULE_LINE.match(lines[i]) and not NUMBERED_ITEM.match(after(i))), None)
    return lines[top + 1:] if top is not None else lines[-30:]


def menu_question(lines):
    """Split a numbered terminal menu into its question and options so a phone can show it as a chat card, not a screen dump."""
    first = next((i for i, line in enumerate(lines) if NUMBERED_ITEM.match(line)), None)
    if first is None:
        return None
    clean = lambda line: re.sub(r'^\s*[│┃]\s*|\s*[│┃]\s*$', '', line).strip()
    above, index = [], first - 1
    while index >= 0 and not clean(lines[index]):  # the paragraph right above the options, not the chat before it
        index -= 1
    while index >= 0 and clean(lines[index]) and not RULE_LINE.match(lines[index]):
        above.insert(0, clean(lines[index]))
        index -= 1
    question = re.sub(r'^Question \d+/\d+:\s*', '', ' '.join(above))
    options, ruled = [], False
    for line in lines[first:]:
        match = re.match(r'^\s*([>❯›]?)\s*(\d+)[.)]\s+(.*\S)', line)
        if match:
            text = match[3].strip()
            # Claude's own-answer row: "Type something." until the user types, then their text. It sits right above
            # the rule line before "Chat about this". With the cursor on it, digit keys type text instead of choosing.
            if options and ruled and text == 'Chat about this':
                options[-1]['free'] = True
            options.append({'id': int(match[2]), 'text': text, 'detail': '', 'selected': bool(match[1])})
            if text == 'Type something.':
                options[-1]['free'] = True
            ruled = False
        elif options and INPUT_HINT.search(line):
            break  # the key hint ends the menu; in a narrow pane it wraps, and a named input border follows
        elif options and clean(line) and RULE_LINE.match(line):
            ruled = True
        elif options and clean(line) and not INPUT_HINT.search(line):
            options[-1]['detail'] = (options[-1]['detail'] + ' ' + clean(line)).strip()
    return {'question': question, 'options': options} if len(options) >= 2 else None


def menu_choose(entry, choice, label=None, answer=None, run=subprocess.run, sleep=time.sleep):
    """Answer a numbered menu by moving the cursor, not by a digit key. A digit types text when the cursor sits on
    Claude's own-answer row, and then every later tap types more ("344"). So: read the cursor row, press Up or Down
    until it is on the chosen row (checking the screen after each press), then Enter. answer: text for the own-answer row;
    its old text is removed first."""
    def press(*keys):
        run([paths.TMUX, 'send-keys', '-t', entry['id'], *keys], check=True, capture_output=True, timeout=5)

    def look():
        menu = menu_question(menu_lines(capture(entry, run).rstrip().splitlines()))
        if not menu:
            raise RuntimeError('The question is no longer open.')
        return menu['options']
    verify(entry, run)
    options = look()
    target = next((i for i, option in enumerate(options) if option['id'] == choice), None)
    if target is None:
        raise RuntimeError('That option is gone. Reload and review the question again.')
    free = options[target].get('free', False)
    if answer is None and free:
        raise ValueError('Type your answer in the message box, then send it.')
    if answer is not None and not free:
        raise ValueError('This option takes no text.')
    if label is not None and not free and options[target]['text'] != label:
        raise RuntimeError('The question changed. Reload and review it again.')
    for _ in range(len(options) * 2 + 2):
        at = next((i for i, option in enumerate(options) if option['selected']), None)
        if at is None:
            raise RuntimeError('Could not find the cursor in the question. Open the terminal to answer it.')
        if at == target:
            break
        press('Down' if at < target else 'Up')
        sleep(.15)
        options = look()
    else:
        raise RuntimeError('Could not move to that option. Open the terminal to answer it.')
    if answer is not None:
        old = options[target]['text']
        if old != 'Type something.':
            press('End', *['BSpace'] * (len(old) + 2))
            sleep(.2)
            if look()[target]['text'] != 'Type something.':
                raise RuntimeError('Could not clear the old answer. Open the terminal to answer it.')
        buffer = 'tmux-mobile-' + uuid.uuid4().hex
        try:
            run([paths.TMUX, 'load-buffer', '-b', buffer, '-'], input=' '.join(answer.split()).encode(), check=True, capture_output=True, timeout=5)
            run([paths.TMUX, 'paste-buffer', '-p', '-d', '-b', buffer, '-t', entry['id']], check=True, capture_output=True, timeout=5)
        finally:
            run([paths.TMUX, 'delete-buffer', '-b', buffer], check=False, capture_output=True, timeout=5)
        sleep(.3)
    press('Enter')


def login_link(lines, agent=None):
    """A sign-in screen (Claude's /login, codex login): the sign-in URL, joined back from its wrapped lines, and whether
    the screen asks for a code. A wrapped URL is one link only on its first line, so a click opens a broken address."""
    # An earlier login in scrollback must never own the current input box.
    start = max((i + 1 for i, line in enumerate(lines) if re.match(r'^[❯›>]\s', line)), default=0)
    # Use the latest login link, including when history holds an earlier attempt.
    for index in range(len(lines) - 1, start - 1, -1):
        if 'https://' not in lines[index]:
            continue
        screen = '\n'.join(lines[index:])
        for name in (('claude', 'codex') if agent is None else (agent,)):
            if name not in login_flow.HOSTS:
                continue
            parsed = login_flow.parse_screen(name, screen)
            url = parsed['url']
            if url and (urlsplit(url).hostname == 'auth.openai.com' or '/oauth/' in url):
                code = name == 'claude' and bool(re.search(r'paste\s+code|authentication code|enter (?:the )?code', ' '.join(screen.split()), re.I))
                found = {'url': url, 'code': code}
                if parsed['user_code']:
                    found['user_code'] = parsed['user_code']
                if login_flow.CODE_ERROR.search(screen):
                    found['message'] = 'Sign-in code failed. Check the code and try again.'
                return found
    return None


def login_capture(entry, screen, run=subprocess.run):
    """A resized pane can push the start of its URL into history. Only a live login field permits recovery."""
    if entry['agent'] not in login_flow.HOSTS or not re.search(r'Pastecodehereifprompted>|one-timecode', re.sub(r'\s+', '', screen), re.I):
        return screen
    return run([paths.TMUX, 'capture-pane', '-p', '-J', '-S', '-200', '-t', entry['id']],
               check=True, capture_output=True, text=True, timeout=5).stdout


_login_progress = {}


def login_progress(entry, screen, now=None):
    """Short-lived native login feedback. Store no URL, code, transcript, or credential."""
    key = (entry['id'], entry.get('owner_pid'), entry.get('process_start'), entry.get('thread'))
    link = login_link(screen.splitlines(), entry['agent'])
    if not link and key not in _login_progress:
        return None
    now = time.monotonic() if now is None else now
    for old_key, value in list(_login_progress.items()):
        if value['expires'] <= now:
            _login_progress.pop(old_key, None)
    fingerprint = hashlib.sha256(link['url'].encode()).hexdigest() if link else None
    state = _login_progress.get(key)
    if link and (not state or state['fingerprint'] != fingerprint):
        if len(_login_progress) >= 64:
            _login_progress.pop(next(iter(_login_progress)))
        state = {'fingerprint': fingerprint, 'phase': 'waiting', 'message': '', 'expires': now + login_flow.LIMIT}
        _login_progress[key] = state
    if not state:
        return None
    # Only inspect the live end of the screen, not old chat messages above a login.
    tail = '\n'.join(screen.rstrip().splitlines()[-15:])
    phase = 'done' if re.search(r'(?im)^\s*(?:[✓✔]\s*)?(?:Login successful|Successfully logged in|Logged in successfully|Authentication successful|Signed in successfully)\b', tail) else 'failed' if login_flow.CODE_ERROR.search(tail) else None
    if phase:
        if state['phase'] != phase:
            state['expires'] = now + 30
        state.update(phase=phase, message='Signed in.' if phase == 'done' else 'Sign-in failed. Try again.')
    elif not link and state['phase'] == 'waiting':
        state.update(phase='unknown', message='Sign-in screen closed. Check the account in Plan usage.')
    return {key: state[key] for key in ('phase', 'message')} if state['phase'] != 'waiting' else None


def attention(entry, screen, working, now=None, login_screen=None):
    """Catch-all for any screen that waits on the user but is not a recognized approval.

    Returns the visible screen once the same situation has lasted ATTENTION_DELAY seconds, so redraws and
    transient states never flash a panel.
    """
    now = time.monotonic() if now is None else now
    lines = screen.rstrip().splitlines()
    tail = lines[-30:]
    marker = {'claude': '❯', 'codex': '›', 'antigravity': '>'}.get(entry['agent'])
    reason = None
    picker = None
    link = login_link(login_screen.splitlines() if login_screen is not None else lines, entry['agent'])
    numbered = [line for line in tail if NUMBERED_ITEM.match(line)]
    tabs = next((index for index, line in enumerate(lines) if SETTINGS_TABS.match(line)), None) if entry['agent'] == 'claude' else None
    if link:
        reason = 'prompt'
    elif tabs is not None:
        # Claude's Settings dialog (/usage, /status ...) only shows facts. Show it from its tab row, without the chat above it.
        reason, tail = 'panel', lines[tabs:]
    elif len(numbered) >= 2 and cursor_in_menu(tail, marker):
        reason = 'menu'
    elif entry['agent'] == 'claude' and (picker := cursor_picker(screen)):
        reason, tail = 'picker', picker['text'].splitlines()
    elif any(INPUT_HINT.search(line) for line in lines[-8:]):
        reason = 'prompt'
    elif marker and not working and lines and not any(line.startswith(marker) for line in lines[-15:]):
        reason = 'no-input-box'
    key = entry['id']
    seen = _attention_seen.get(key)
    if not reason:
        # A redraw can drop the panel for one capture. Keep a shown panel's record a little while so it returns at once.
        if seen and not (seen['shown'] and now - seen['last'] < ATTENTION_GRACE):
            _attention_seen.pop(key, None)
        return None
    # The cursor moves while the user answers; it must not restart the delay.
    text = '\n'.join(tail).strip('\n')
    fingerprint = hashlib.sha256((reason + re.sub(r'[>❯›]|\s+', '', text)).encode()).hexdigest()
    if not seen or seen['reason'] != reason or (not seen['shown'] and seen['fingerprint'] != fingerprint):
        _attention_seen[key] = {'fingerprint': fingerprint, 'first': now, 'last': now, 'reason': reason, 'shown': False, 'id': fingerprint}
        return None
    if not seen['shown'] and now - seen['first'] < ATTENTION_DELAY:
        return None
    # Once shown, a panel that redraws (a /usage panel fills in, a clock ticks) stays up and keeps its id.
    # A menu takes the new id, because a new question needs a fresh card.
    seen.update(fingerprint=fingerprint, last=now, shown=True, id=fingerprint if reason in ('menu', 'picker') else seen['id'])
    found = {'id': seen['id'], 'text': text, 'reason': reason}
    if reason == 'menu':
        found.update(menu_question(menu_lines(lines)) or {})
    elif reason == 'picker':
        found.update(picker)
    elif link:
        found['login'] = link
    return found


_busy = {}  # transcript path -> ((size, mtime), result)


def transcript_busy(path, window=262144):
    """True when the last lifecycle event in a Codex rollout file is a started turn."""
    try:
        info = os.stat(path)
        if path in _busy and _busy[path][0] == (info.st_size, info.st_mtime_ns):
            return _busy[path][1]
        with open(path, 'rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - window))
            tail = stream.read().decode(errors='ignore')
    except (OSError, TypeError):
        return False
    last = None
    for line in tail.splitlines():
        for kind in ('task_started', 'task_complete', 'turn_aborted'):
            if '"type":"' + kind + '"' in line.replace(' ', ''):
                last = kind
    if len(_busy) > 64:
        _busy.clear()
    _busy[path] = ((info.st_size, info.st_mtime_ns), last == 'task_started')
    return last == 'task_started'


def reflow_prompt(text):
    """Antigravity breaks its permission request at the pane width; join each paragraph back into one line and drop the
    rule lines and the lone 'Command' heading."""
    paragraphs, current = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or RULE_LINE.match(stripped):
            if current:
                paragraphs.append(' '.join(current))
                current = []
        elif stripped != 'Command':
            current.append(stripped)
    if current:
        paragraphs.append(' '.join(current))
    return '\n'.join(paragraphs)


# Seconds a "waiting" status must hold. Longer than ATTENTION_DELAY, so a menu the screen parser knows wins,
# and a prompt just answered does not flash a card.
REGISTRY_SETTLE = ATTENTION_DELAY + 1


def waiting_approval(registry, now=None):
    """True when Claude Code's own session file says it waits on a permission: "permission prompt" in 2.1.x."""
    waiting_for = registry.get('waitingFor')
    if registry.get('status') != 'waiting' or not isinstance(waiting_for, str):
        return False
    if waiting_for not in ('permission prompt', 'sandbox request', 'worker request') and not waiting_for.startswith('approve'):
        return False
    changed = registry.get('statusUpdatedAt')
    now = time.time() if now is None else now
    return isinstance(changed, (int, float)) and now - changed / 1000 >= REGISTRY_SETTLE


def live_status(entry, run=subprocess.run):
    if not entry.get('owner_pid') or not entry.get('writable'):
        return {'state': 'Unavailable', 'approval': None}
    screen = capture(entry, run)
    login_screen = login_capture(entry, screen, run)
    status = _live_status(entry, run, screen, login_screen=login_screen)
    progress = login_progress(entry, login_screen)
    if progress:
        status['login_result'] = progress
    if entry['agent'] == 'claude':
        import footer  # footer imports this module
        # Background work from Claude's footer line ("1 shell", "2 local agents"), so the session list can show it.
        status['tasks'] = footer.parse(screen)['tasks']
    return status


def _live_status(entry, run, screen, login_screen=None):
    if entry['agent'] == 'antigravity':
        capture_notices(entry, screen)
    else:
        capture_quota_notices(entry, screen)
    pending = approval(screen)
    if pending:
        if entry['agent'] == 'antigravity':
            pending['text'] = reflow_prompt(pending['text'])
        return {'state': 'Approval', 'approval': {k: v for k, v in pending.items() if not k.startswith('_')}}
    working = bool(re.search(r'Working \(|esc to interrupt|(?m:^\s*[✻✶✽✢·*●]\s+[A-Z][a-z]+(?:ing|ed)?…)', screen, re.I))
    if entry['agent'] == 'codex':
        working = working or transcript_busy(entry.get('transcript'))
    registry = {}
    if entry['agent'] == 'claude' and entry.get('registry'):  # a Claude with no conversation yet has no registry
        try:
            registry = json.loads(Path(entry['registry']).read_text())
            working = working or registry.get('status') == 'busy'
        except (OSError, ValueError):
            pass
    elif entry['agent'] == 'antigravity':
        try:
            metrics = json.loads((AGY_CACHE / (entry['thread'] + '.json')).read_text())
            working = working or metrics.get('agent_state') in ('thinking', 'working', 'tool_use', 'running')
            if metrics.get('tool_confirmation_pending'):
                return {'state': 'Approval', 'approval': {'id': None, 'text': screen.strip(), 'actionable': False}}
        except (OSError, ValueError):
            pass
    needed = attention(entry, screen, working, login_screen=login_screen)
    if needed and needed['reason'] == 'panel':  # a read-only panel waits on no one: keep the real state, show the panel
        return {'state': 'Working' if working else 'Idle', 'approval': None, 'attention': needed}
    if needed:
        return {'state': 'Input needed', 'approval': None, 'attention': needed}
    if waiting_approval(registry) and (_attention_seen.get(entry['id']) or {}).get('reason') != 'menu':
        # The screen parser missed this prompt. Show it read-only: the option keys are not known.
        reason = {'permission prompt': 'Claude is waiting for your permission.', 'sandbox request': 'Claude is waiting for sandbox access.',
                  'worker request': 'A worker agent is waiting for your permission.'}.get(registry['waitingFor'], registry['waitingFor'])
        text = reason + '\n\n' + '\n'.join(screen.rstrip().splitlines()[-20:])
        return {'state': 'Approval', 'approval': {'id': None, 'text': text.strip(), 'actionable': False}}
    status = {'state': 'Working' if working else 'Idle', 'approval': None}
    if entry['agent'] == 'claude' and not working and (reply := suggestion(entry, run)):
        status['suggestion'] = reply
    return status


def verify(entry, run=subprocess.run):
    pid = entry['owner_pid']
    process = Path('/proc') / str(pid)
    valid_comm = {'claude': ('claude',), 'codex': ('codex',), 'antigravity': ('agy', 'exe')}[entry['agent']]
    process_comm = process.joinpath('comm').read_text().strip()
    if process_start(pid) != entry['process_start'] or process_comm not in valid_comm:
        raise RuntimeError('The agent restarted. Reload its tab before sending.')
    result = run([paths.TMUX, 'display-message', '-p', '-t', entry['id'],
                  '#{pane_pid}\t#{pane_current_command}\t#{pane_in_mode}'],
                 check=True, capture_output=True, text=True, timeout=5)
    root, command, mode = result.stdout.strip().split('\t')
    if mode != '0' or command not in {'claude': ('claude',), 'codex': ('node', 'codex'), 'antigravity': ('agy', 'exe')}[entry['agent']]:
        raise RuntimeError('Return to the agent prompt in Termius before sending.')
    ancestor = pid
    while ancestor != int(root) and ancestor > 1:
        ancestor = int((Path('/proc') / str(ancestor) / 'stat').read_text().rsplit(')', 1)[1].split()[1])
    if ancestor != int(root):
        raise RuntimeError('The agent no longer belongs to this tmux pane.')
    if entry['agent'] == 'claude':
        if not entry.get('pending_conversation'):
            registry = json.loads(Path(entry['registry']).read_text())
            if registry.get('sessionId') != entry['thread'] or registry.get('pid') != pid:
                raise RuntimeError('The Claude conversation changed. Reload its tab.')
    elif entry['agent'] == 'antigravity':
        if not entry.get('pending_conversation'):
            lock = AGY_ROOT / 'presence' / (entry['thread'] + '.lock')
            if lock.exists():
                if not any(p.resolve() == lock for p in process.joinpath('fd').iterdir()):
                    raise RuntimeError('The Antigravity conversation changed. Reload its tab.')
            else:
                cache_file = AGY_CACHE / (entry['thread'] + '.json')
                if cache_file.is_file():
                    try:
                        cached = json.loads(cache_file.read_text())
                        if cached.get('conversation_id') != entry['thread']:
                            raise RuntimeError('The Antigravity conversation changed. Reload its tab.')
                    except (OSError, ValueError):
                        pass
    elif entry['agent'] == 'codex':
        if not entry.get('pending_conversation'):  # a new session has no conversation until its first message
            from agents import codex_database, codex_thread
            db = codex_database()
            try:
                row = codex_thread(db, pid, entry['process_start'], entry.get('thread_cwd') or entry['cwd'])
            finally:
                db.close()
            if not row or row[0] != entry['thread']:
                raise RuntimeError('The Codex conversation changed. Reload its tab.')


class DraftError(RuntimeError):
    """The agent input box holds text, so sending would mix it with the new message."""


class PaneTooSmall(RuntimeError):
    """The pane is too short to show the agent input box."""


# Claude needs two rules, the prompt and a status line, plus room for wrapped hints
# and spacing. Eight rows is a diagnostic threshold. A visible marker always wins.
MIN_INPUT_ROWS = 8
SIZE_FIXING = 'Tether is fixing the terminal size. Send again in a moment.'


def small_pane_reason(entry, run=subprocess.run):
    """Explain a hidden input box using read-only tmux queries; None if size is not the cause or queries fail."""
    try:
        result = run([paths.TMUX, 'display-message', '-p', '-t', entry['id'],
                      '#{pane_width}\t#{pane_height}\t#{window_width}\t#{window_height}\t'
                      '#{session_name}\t#{window_name}\t#{window-size}'],
                     check=True, capture_output=True, text=True, timeout=5)
        if result.returncode:
            return None
        pw, ph, ww, wh, session, window, sizing = result.stdout.rstrip('\n').split('\t')
        pw, ph, ww, wh = map(int, (pw, ph, ww, wh))
        if min(pw, ph, ww, wh) <= 0 or not session or not sizing or ph >= MIN_INPUT_ROWS:
            return None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None
    return SIZE_FIXING


def prompt_draft(entry, run=subprocess.run, *, _size_retry=True):
    screen = capture(entry, run)
    if approval(screen):
        raise RuntimeError('Respond to the pending approval before sending a message.')
    if entry['agent'] == 'claude' and (picker := cursor_picker(screen)):
        raise RuntimeError(f'Claude has the {picker["title"]} picker open. Choose an item or press Esc in the card.')
    marker = {'claude': '❯', 'codex': '›', 'antigravity': '>'}[entry['agent']]
    lines = screen.splitlines()
    prompts = [i for i, line in enumerate(lines) if line.startswith(marker)]
    if not prompts:
        if not _size_retry:
            raise PaneTooSmall(SIZE_FIXING)
        reason = small_pane_reason(entry, run)
        if reason:
            if _size_retry:
                window_size.apply(run)
                time.sleep(.1)  # Let the agent redraw its input after SIGWINCH.
                return prompt_draft(entry, run, _size_retry=False)
            raise PaneTooSmall(reason)
        name = {'claude': 'Claude', 'codex': 'Codex', 'antigravity': 'Antigravity'}[entry['agent']]
        raise RuntimeError(f'Cannot find the {name} input box on screen. '
                           'Open the pane in Terminals to see what it shows.')
    start = prompts[-1]
    if re.match(r'\s*\d+[.)]\s', lines[start][1:]) and menu_question(lines[start:]):
        # Codex marks the chosen row of a menu ("› 1. Review hooks") the same way as its input line.
        name = {'claude': 'Claude', 'codex': 'Codex', 'antigravity': 'Antigravity'}[entry['agent']]
        raise RuntimeError(f'{name} is asking you to pick an option. Answer it above, then send again.')
    draft = [lines[start][1:].strip()]
    for line in lines[start + 1:]:
        if line.startswith('─') or (entry['agent'] == 'codex' and not line.strip()):
            break
        draft.append(line.strip())
    if entry['agent'] == 'codex' and draft == ['Ask Codex to do anything']:
        draft = []
    if entry['agent'] == 'claude' and draft == ['Press up to edit queued messages']:
        draft = []
    # Claude renders placeholders and prompt suggestions ("Try ...", "push it") dimmed; typed text is not dim.
    if any(draft) and entry['agent'] == 'claude':
        if dim_prompt_text(capture(entry, run, styled=True)) == draft[0] and len(draft) == 1:
            draft = []
    if len(draft) == 1 and entry['agent'] == 'antigravity' and AGY_MODE_HINT.search(draft[0]):
        # Antigravity fills the empty box with a mode hint ("Auto mode: ... (shift+tab to cycle)") in its hint colour,
        # and the mode changes it; typed text is never drawn in that colour.
        if hint_prompt_text(capture(entry, run, styled=True), '>') == draft[0]:
            draft = []
    return draft


ANSI_SGR = re.compile(r'\x1b\[[0-9;]*m')
AGY_MODE_HINT = re.compile(r'\bmode\b.*(?:\(shift\+tab to cycle\)|\.\.\.|…)\s*$', re.I)  # a narrow pane cuts the hint short


def hint_prompt_text(styled, marker):
    """Text of the last input line when all of it is in Antigravity's hint colour (256-colour 109), else ''."""
    candidates = [line for line in styled.splitlines() if ANSI_SGR.sub('', line).startswith(marker)]
    if not candidates:
        return ''
    line = candidates[-1]
    hint = False
    start = line.index(marker) + 1
    for part in re.split(r'(\x1b\[[0-9;]*m)', line[start:]):
        if ANSI_SGR.fullmatch(part):
            codes = [int(code or 0) for code in part[2:-1].split(';') if code.isdigit() or code == '']
            if codes[:3] == [38, 5, 109]:
                hint = True
            elif 0 in codes or 39 in codes or codes[:2] == [38, 5] or codes[:1] == [38]:
                hint = False
        elif part.strip() and not hint:
            return ''
    return ANSI_SGR.sub('', line)[1:].strip()


def dim_prompt_text(styled):
    """Text of Claude's input line when every visible character is dimmed (a placeholder or suggested reply)."""
    candidates = [line for line in styled.splitlines() if ANSI_SGR.sub('', line).startswith('❯')]
    if not candidates:
        return ''
    line = candidates[-1]
    text = ANSI_SGR.sub('', line)[1:].strip()
    dim = False
    for part in re.split(r'(\x1b\[[0-9;]*m)', line[line.index('❯') + 1:]):
        if ANSI_SGR.fullmatch(part):
            for code in [int(code or 0) for code in part[2:-1].split(';')]:
                if code in (0, 22):
                    dim = False
                elif code == 2:
                    dim = True
        elif part.strip() and not dim:
            return ''
    return text


def suggestion(entry, run=subprocess.run):
    """A reply Claude has prepared for the user, shown dimmed in the input box."""
    text = dim_prompt_text(capture(entry, run, styled=True))
    return '' if text == 'Press up to edit queued messages' or text.startswith('Try ') else text


AGY_PANEL = re.compile(r'esc Close|↑/↓ Scroll|\(\d+–\d+ of \d+ lines\)|general\s+commands\s+shortcuts', re.I)


def close_agy_panel(entry, run=subprocess.run):
    """Antigravity's /usage, /help ... open full-screen panels that swallow typing: close one (Esc) before sending anything."""
    if entry.get('agent') != 'antigravity':
        return
    for _ in range(3):
        if not AGY_PANEL.search('\n'.join(capture(entry, run).splitlines()[-40:])):
            return
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Escape'], check=True, capture_output=True, timeout=5)
        time.sleep(.5)
    if AGY_PANEL.search('\n'.join(capture(entry, run).splitlines()[-40:])):
        raise RuntimeError('An Antigravity panel is open. Close it in Termius, then send again.')


PANEL_CLOSE = re.compile(r'Esc to (?:cancel|close|exit|go back)|esc (?:Close|to close)', re.I)


def close_info_panel(entry, run=subprocess.run):
    """A read-only panel such as /usage, /status or /cost covers Claude's input box. Sending a message means the person
    is done with it, so press Esc once. A menu, picker or approval asks something, so it is never closed here."""
    if entry.get('agent') != 'claude':
        return False
    screen = capture(entry, run)
    lines = screen.rstrip().splitlines()
    panel = any(PANEL_CLOSE.search(line) for line in lines[-8:]) or any(SETTINGS_TABS.match(line) for line in lines)
    if approval(screen) or cursor_picker(screen) or not panel or any(CURSOR_ITEM.match(line) for line in lines[-30:]):
        return False
    run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Escape'], check=True, capture_output=True, timeout=5)
    time.sleep(.4)
    return True


_escape_seen = {}


def drop_double_escape(entry, run=subprocess.run, now=None):
    """Suppress repeated Esc requests only at Claude's visible, empty input box."""
    if entry.get('agent') != 'claude':
        return False
    now = time.monotonic() if now is None else now
    identity = (entry['id'], entry.get('thread'), entry.get('owner_pid'), entry.get('process_start'))
    previous = _escape_seen.get(identity)
    _escape_seen[identity] = now
    if previous is None or not 0 <= now - previous < 1.0:
        return False
    try:
        return not any(prompt_draft(entry, run))
    except RuntimeError:
        return False  # a picker, approval or hidden input box must still receive Esc


def empty_prompt(entry, run=subprocess.run):
    close_agy_panel(entry, run)
    try:
        draft = prompt_draft(entry, run)
    except PaneTooSmall:
        raise
    except RuntimeError as exc:
        if 'input box' not in str(exc) or not close_info_panel(entry, run):
            raise
        draft = prompt_draft(entry, run)
    if any(draft):
        raise DraftError('There is an unsent draft in the agent input. Clear it, then send again.')


def login_code_draft(screen):
    prompts = list(re.finditer(r'Paste\s+code\s+here\s+if\s+prompted\s*>', screen, re.I))
    return re.split(r'\bEsc\s+to\s+cancel\b', screen[prompts[-1].end():], maxsplit=1, flags=re.I)[0].strip() if prompts else None


def type_text(entry, text, enter=False, run=subprocess.run, login_url=None):
    if not isinstance(text, str) or not 0 < len(text) <= 500 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError('Enter up to 500 characters of text.')
    verify(entry, run)
    if login_url is not None:
        screen = capture(entry, run)
        if login_code_draft(screen):
            raise DraftError('The sign-in field has text. Check the terminal before sending.')
        screen = login_capture(entry, screen, run)
        lines = screen.splitlines()
        link = login_link(lines, entry['agent'])
        if entry['agent'] != 'claude' or not link or not link['code'] or link['url'] != login_url:
            raise RuntimeError('The sign-in prompt changed. Reload and try again.')
        if login_code_draft(screen) != '':
            raise DraftError('The sign-in field has text or is not visible. Check the terminal before sending.')
    run([paths.TMUX, 'send-keys', '-t', entry['id'], '-l', '--', text], check=True, capture_output=True, timeout=5)
    if enter:
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)


def card_key(entry, key, run=subprocess.run, sleep=time.sleep):
    """Press one key of a ✦ card. Claude takes a card's hotkeys only while the card holds the keyboard. It moves the focus
    there by itself only when Claude is idle and the input box is empty; otherwise the digit is typed into the box, which then
    blocks every later send. So: refuse when the box has text. If the digit lands as text, remove it, focus the card with
    Ctrl+X Tab (Claude's abovePrompt:focus key) and press it once more. If it lands as text again, remove it and give up."""
    verify(entry, run)
    draft = [line for line in prompt_draft(entry, run) if line]
    if draft:
        raise DraftError(f'Claude’s input box has text (“{" ".join(draft)[:40]}”). Clear it, then tap again.')
    def press(*keys):
        run([paths.TMUX, 'send-keys', '-t', entry['id'], *keys], check=True, capture_output=True, timeout=5)

    def typed():
        return [line for line in prompt_draft(entry, run) if line] == [key]
    press(key)
    sleep(.3)
    if not typed():
        return
    press('BSpace')
    press('C-x', 'Tab')
    sleep(.2)
    press(key)
    sleep(.3)
    if typed():
        press('BSpace')
        raise RuntimeError('Claude did not take the key, so the card is not active now. Open the terminal to answer it.')


def clear_draft(entry, run=subprocess.run):
    verify(entry, run)
    if not any(prompt_draft(entry, run)):
        return
    keys = lambda *k: run([paths.TMUX, 'send-keys', '-t', entry['id'], *k], check=True, capture_output=True, timeout=5)
    keys('C-e')
    # Ctrl+U clears to the start of the line; Backspace joins the previous line of a multi-line draft.
    for _ in range(40):
        keys('C-u')
        time.sleep(.05)
        if not any(prompt_draft(entry, run)):
            return
        keys('BSpace')
        if not any(prompt_draft(entry, run)):
            return
    raise DraftError('Could not clear the draft. Clear it in Termius.')


QUEUE_HINT = 'Press up to edit queued messages'


def unqueue(entry, run=subprocess.run, sleep=time.sleep, *, _size_retry=True):
    """Take back what waits in Claude's queue: Up pulls every queued message into its input (Claude's own "edit queued"),
    then the input is cleared. Returns the text so the page can put it back in its own message box."""
    if entry.get('agent') != 'claude':
        raise ValueError('Only Claude queues can be cancelled from here.')
    verify(entry, run)
    lines = capture(entry, run).splitlines()
    prompts = [line for line in lines if line.startswith('❯')]
    if not prompts:
        if not _size_retry:
            raise PaneTooSmall(SIZE_FIXING)
        reason = small_pane_reason(entry, run)
        if reason:
            if _size_retry:
                window_size.apply(run)
                sleep(.1)
                return unqueue(entry, run, sleep, _size_retry=False)
            raise PaneTooSmall(reason)
    if not prompts or QUEUE_HINT not in prompts[-1]:
        raise RuntimeError('Nothing is queued. Claude may already have taken the message.')
    run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Up'], check=True, capture_output=True, timeout=5)
    for _ in range(10):
        sleep(.15)
        draft = [line for line in prompt_draft(entry, run) if line]
        if draft:
            break
    else:
        raise RuntimeError('Claude did not hand back the queued message. Check the terminal.')
    text = '\n'.join(draft)
    clear_draft(entry, run)
    return text


def confirm_delivery(entry, sent, run):
    """Antigravity sometimes drops a message right after it starts or refreshes, with an empty box and no error. Its screen
    then never shows the text, so a message missing from the screen after a moment is reported instead of silently lost."""
    if entry.get('agent') != 'antigravity' or sent.startswith('/') or len(sent) < 3:
        return
    for _ in range(8):
        time.sleep(.5)
        screen = capture_screen(entry, 200, run)
        # Taken if the text is on screen (the pane can cut or rewrap it, so only its start is compared), or if the agent is
        # visibly busy: a message sent while it works may be queued out of sight.
        if sent[:15] in ''.join(screen.split()) or re.search(r'esc to cancel|Loading\.\.\.|Working', '\n'.join(screen.splitlines()[-8:])):
            return
    raise RuntimeError('Antigravity did not take the message. Send it again.')


def leave_scrollback(entry, run=subprocess.run):
    """With tmux mouse mode on, a wheel scroll in the terminal puts the pane in copy mode, and every send was then refused.
    Sending a message means the person is done reading back, so leave copy mode (or view mode) first. Other modes stay."""
    mode = run([paths.TMUX, 'display-message', '-p', '-t', entry['id'], '#{pane_in_mode}\t#{pane_mode}'],
               check=True, capture_output=True, text=True, timeout=5).stdout.strip()
    if mode in ('1\tcopy-mode', '1\tview-mode'):
        run([paths.TMUX, 'send-keys', '-t', entry['id'], '-X', 'cancel'], check=True, capture_output=True, timeout=5)


def submit(entry, message, run=subprocess.run):
    if any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in message):
        raise RuntimeError('The message contains terminal control characters.')
    leave_scrollback(entry, run)
    verify(entry, run)
    empty_prompt(entry, run)
    buffer = 'tmux-mobile-' + uuid.uuid4().hex
    try:
        run([paths.TMUX, 'load-buffer', '-b', buffer, '-'], input=message.encode(), check=True, capture_output=True, timeout=5)
        verify(entry, run)
        empty_prompt(entry, run)
        run([paths.TMUX, 'paste-buffer', '-p', '-d', '-b', buffer, '-t', entry['id']], check=True, capture_output=True, timeout=5)
        verify(entry, run)
        if approval(capture(entry, run)):
            raise RuntimeError('An approval appeared during delivery. Check Termius before retrying.')
        # The agent can swallow an Enter that arrives right behind a bracketed paste, leaving the
        # message sitting in the input box; confirm the box emptied and press Enter again if not.
        sent = ''.join(message.split())
        for _ in range(4):
            time.sleep(.2)
            run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)
            time.sleep(.3)
            try:
                draft = prompt_draft(entry, run)
            except PaneTooSmall:
                raise
            except RuntimeError as exc:
                if 'approval' in str(exc):
                    raise
                return  # no input box on screen any more: a menu or picker took the Enter
            if not any(draft):
                confirm_delivery(entry, sent, run)
                return
            # Agents mark the highlighted row of a picker (/model ...) like the input line. If the box no longer holds the
            # text we sent, a menu opened: pressing Enter again would choose an item for the user.
            if '\n' not in message.strip() and not ''.join(' '.join(draft).split()).startswith(sent[:30]):
                return
        raise DraftError('The message reached the agent input but was not submitted. Clear it, then send again.')
    finally:
        run([paths.TMUX, 'delete-buffer', '-b', buffer], check=False, capture_output=True, timeout=5)


AGENT_TITLES = {'claude': 'Claude', 'codex': 'Codex'}


def rename_agent(entry, name, run=subprocess.run):
    """Send `/rename NAME` so the agent's own session name matches the Tether name. Claude and Codex both take the name inline.
    Only when the agent is idle with an empty input box. Returns '' when sent, else why it was skipped."""
    title = AGENT_TITLES.get(entry.get('agent'))
    if not title or not entry.get('writable'):
        return ''
    try:
        if live_status(entry, run)['state'] != 'Idle':
            return f'{title} is busy, so its own name did not change. Rename again when it is idle.'
        submit(entry, '/rename ' + ' '.join(name.split()), run)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        return f'{title}\'s own name did not change: {exc}'
    return ''


def respond_approval(entry, identifier, action=None, run=subprocess.run, choice=None, feedback=None):
    if choice is None and action not in ('approve', 'reject'):
        raise ValueError('Invalid approval action.')
    verify(entry, run)
    pending = approval(capture(entry, run))
    if not pending or pending['id'] != identifier or not pending['actionable']:
        raise RuntimeError('The approval changed. Reload and review it again.')
    if choice is not None:
        if type(choice) is not int or choice not in pending['_numbers']:
            raise ValueError('Choose one of the displayed approval options.')
        target = pending['_numbers'].index(choice)
    else:
        target = pending['_once'] if action == 'approve' else pending['_reject']
    if target is None:
        raise RuntimeError('That approval choice is unavailable. Review it again.')
    if feedback is not None:
        if (entry.get('agent') != 'claude' or not plan_menu(pending)
                or not re.match(r'No,?\s+keep planning\b', pending['options'][target]['text'], re.I)):
            raise ValueError('Feedback requires the displayed Keep planning option.')
        if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 10000 or any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in feedback):
            raise ValueError('Enter feedback without terminal control characters (up to 10000 characters).')
    delta = target - pending['_selected']
    expected = pending['_selected']
    for _ in range(abs(delta)):
        verify(entry, run)
        current = approval(capture(entry, run))
        if not current or current['id'] != identifier:
            raise RuntimeError('The approval changed. Review it again.')
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Down' if delta > 0 else 'Up'], check=True, capture_output=True, timeout=5)
        expected += 1 if delta > 0 else -1
        deadline = time.monotonic() + .5
        while True:
            current = approval(capture(entry, run))
            if not current or current['id'] != identifier:
                raise RuntimeError('The approval changed. Review it again.')
            if current['_selected'] == expected:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Could not confirm the approval selection. Check Termius.')
            time.sleep(.03)
    verify(entry, run)
    current = approval(capture(entry, run))
    if not current or current['id'] != identifier or current['_selected'] != target:
        raise RuntimeError('Could not confirm the selected approval choice. Review it in Termius.')
    run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)
    if feedback is not None:
        deadline = time.monotonic() + 2
        while True:
            verify(entry, run)
            screen = capture(entry, run)
            if not approval(screen) and not menu_question(screen.splitlines()[-30:]):
                try:
                    empty_prompt(entry, run)
                    break
                except DraftError:
                    raise
                except RuntimeError:
                    pass
            if time.monotonic() >= deadline:
                raise RuntimeError('Keep planning was selected, but feedback was not sent. Check the terminal input and send your feedback again.')
            time.sleep(.05)
        submit(entry, feedback.strip(), run=run)


def shell_prompt(entry, run=subprocess.run):
    if entry.get('agent') != 'shell':
        raise RuntimeError('An agent is already running in this pane.')
    if process_start(entry['pane_pid']) != entry['pane_start']:
        raise RuntimeError('This tmux pane changed. Reload its tab.')
    process = Path('/proc') / str(entry['pane_pid'])
    fields = process.joinpath('stat').read_text().rsplit(')', 1)[1].split()
    if process.joinpath('comm').read_text().strip() not in ('bash', 'zsh', 'fish') or fields[2] != fields[5]:
        raise RuntimeError('Wait for the foreground program to finish before starting an agent.')
    state = run([paths.TMUX, 'display-message', '-p', '-t', entry['id'],
                 '#{pane_pid}\t#{pane_current_command}\t#{pane_in_mode}\t#{cursor_y}\t#{cursor_x}'],
                check=True, capture_output=True, text=True, timeout=5).stdout.strip().split('\t')
    if len(state) != 5 or state[0] != str(entry['pane_pid']) or state[1] not in ('bash', 'zsh', 'fish') or state[2] != '0':
        raise RuntimeError('This pane is not at an idle shell prompt.')
    lines = capture(entry, run).splitlines()
    row, column = int(state[3]), int(state[4])
    if row >= len(lines):
        # A prompt that fills the last row puts the cursor on a new, empty row that capture-pane drops.
        lines += [''] * (row + 1 - len(lines))
    if lines[row][column:].strip():
        return lines[row].rstrip()  # text after the cursor: a draft, which the caller refuses
    before = lines[row][:column].rstrip()
    if not before and row > 0:
        return lines[row - 1].rstrip()  # the prompt filled the row above and the cursor wrapped
    return before


def start_agent(entry, agent, run=subprocess.run, profile=None):
    """Type the agent's launch command at the pane's idle shell prompt. profile: a Codex profile (`codex -p foundry`); None = the plan."""
    command = LAUNCHERS.get(agent)
    if not command:
        raise ValueError('Choose a supported agent.')
    if profile is not None:
        if agent != 'codex' or not PROFILE_NAME.fullmatch(profile):
            raise ValueError('Choose a supported Codex provider.')
        command += ' -p ' + profile
    prompt = shell_prompt(entry, run)
    if not re.search(r'[$#>%❯➜]$', prompt):
        raise RuntimeError('There is terminal input or an unrecognized shell prompt. Clear it before starting an agent.')
    buffer = 'tmux-mobile-launch-' + uuid.uuid4().hex
    try:
        run([paths.TMUX, 'load-buffer', '-b', buffer, '-'], input=command.encode(), check=True, capture_output=True, timeout=5)
        if shell_prompt(entry, run) != prompt:
            raise RuntimeError('The shell input changed. Try again after clearing its draft.')
        run([paths.TMUX, 'paste-buffer', '-p', '-d', '-b', buffer, '-t', entry['id']], check=True, capture_output=True, timeout=5)
        deadline = time.monotonic() + 2
        while True:
            current = shell_prompt(entry, run)
            tail = current[len(prompt):].strip() if current.startswith(prompt) else current.strip()
            # zsh-autosuggestions can draw grey text after the pasted command (e.g. "claude --resume"); Enter runs only what was typed.
            if current == prompt + ' ' + command or current == prompt + command or tail == command or current.rstrip().endswith(command) or tail.startswith(command + ' '):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Could not confirm the launch command. Check the terminal before retrying.')
            time.sleep(.05)
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)
    finally:
        run([paths.TMUX, 'delete-buffer', '-b', buffer], check=False, capture_output=True, timeout=5)


def shell_snapshot(entry, run=subprocess.run):
    # -J joins lines the pane wrapped, so a long URL stays one line that the phone can open or copy.
    text = run([paths.TMUX, 'capture-pane', '-p', '-J', '-S', '-1000', '-t', entry['id']],
               check=True, capture_output=True, text=True, timeout=5).stdout.rstrip()
    return {'messages': [{'id': 'terminal', 'role': 'assistant', 'kind': 'terminal', 'text': text}],
            'thread': None, 'agent': 'shell', 'writable': True, 'session': entry['id'], 'usage': {}, 'busy': False}


def foreground_program(entry):
    """True when a program (gcloud auth, a y/n script) runs in front of the pane's shell and reads the keyboard."""
    if entry.get('agent') != 'shell' or process_start(entry['pane_pid']) != entry['pane_start']:
        return False
    try:
        fields = (Path('/proc') / str(entry['pane_pid']) / 'stat').read_text().rsplit(')', 1)[1].split()
    except OSError:
        return False
    return fields[2] != fields[5]  # the shell's process group is not the terminal's foreground group


def answer_program(entry, message, run=subprocess.run):
    """Type one line into the running program, then Enter: a verification code, a password prompt answer, "y"."""
    if '\n' in message.strip():
        raise RuntimeError('A program is running in this pane. Send one line at a time.')
    state = run([paths.TMUX, 'display-message', '-p', '-t', entry['id'], '#{pane_pid}\t#{pane_in_mode}'],
                check=True, capture_output=True, text=True, timeout=5).stdout.strip().split('\t')
    if state != [str(entry['pane_pid']), '0']:
        raise RuntimeError('The terminal is scrolled or changed. Check it, then send again.')
    run([paths.TMUX, 'send-keys', '-t', entry['id'], '-l', message.strip()], check=True, capture_output=True, timeout=5)
    run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)


def shell_submit(entry, message, run=subprocess.run):
    if any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in message):
        raise RuntimeError('The command contains terminal control characters.')
    if foreground_program(entry):
        return answer_program(entry, message, run)
    prompt = shell_prompt(entry, run)
    if not re.search(r'[$#>%❯➜]$', prompt):
        raise RuntimeError('Clear the existing terminal draft before sending a command.')
    buffer = 'tmux-mobile-shell-' + uuid.uuid4().hex
    try:
        run([paths.TMUX, 'load-buffer', '-b', buffer, '-'], input=message.encode(), check=True, capture_output=True, timeout=5)
        if shell_prompt(entry, run) != prompt:
            raise RuntimeError('The shell input changed. Try again after clearing its draft.')
        run([paths.TMUX, 'paste-buffer', '-p', '-d', '-b', buffer, '-t', entry['id']], check=True, capture_output=True, timeout=5)
        time.sleep(.1)
        shell_prompt(entry, run)  # Recheck the idle foreground shell before Enter.
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)
    finally:
        run([paths.TMUX, 'delete-buffer', '-b', buffer], check=False, capture_output=True, timeout=5)
