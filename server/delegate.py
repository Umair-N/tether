"""Validate and launch a task in a new window beside its source chat."""
import os
import re
import shlex
import subprocess
import time
import unicodedata
from uuid import uuid4

import codex_provider
import paths
import review_links
from terminal_input import PROFILE_NAME, UUID, new_window_after


def validate(body, entries):
    pane = body.get('session')
    if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
        raise ValueError('Select a chat tab for the new task.')
    entry = next((item for item in entries if item['id'] == pane), None)
    if entry is None:
        raise ValueError('That session has ended. Select another tab.')
    if body.get('thread') != entry.get('thread'):
        raise ValueError('This tab changed conversations. Reload it and try again.')
    if body.get('agent') not in ('claude', 'codex'):
        raise ValueError('Choose Claude or Codex for the new task.')
    message = body.get('message')
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 20000:
        raise ValueError('Enter a task with 1 to 20000 characters.')
    if any(unicodedata.category(char) == 'Cc' and char not in '\n\t' for char in message):
        raise ValueError('The task contains terminal control characters.')
    for option in ('context', 'review'):
        if not isinstance(body.get(option, False), bool):
            raise ValueError('Context and review must be true or false.')
    supported = entry.get('agent') in ('claude', 'codex')
    thread = entry.get('thread')
    if body.get('context') and (not supported or not isinstance(thread, str)
                                or not UUID.fullmatch(thread) or entry.get('pending_conversation')):
        raise ValueError('Context needs a Claude or Codex chat with a saved conversation. Send a first message, then try again.')
    if body.get('review') and (not supported or not entry.get('writable')):
        raise ValueError('Review needs a writable Claude or Codex chat.')
    return entry


def task_name(message):
    name = ' '.join(message.strip().split('\n', 1)[0].split())
    if len(name) <= 32:
        return name
    prefix = name[:32]
    if name[32] != ' ' and ' ' in prefix:
        prefix = prefix.rsplit(' ', 1)[0]
    return prefix.rstrip() + '…'


BRIEF_TURNS = 6       # recent user and assistant messages in a context brief
BRIEF_EACH = 600      # characters kept from each message
BRIEF_TOTAL = 3000    # characters for the whole brief: the new chat gets a short start, not the whole chat


def brief(messages):
    """The last few user and assistant texts, each cut short, oldest first. Tool output is left out."""
    picked, total = [], 0
    for message in reversed(messages or []):
        text = message.get('text') if isinstance(message, dict) else None
        role = message.get('role') if isinstance(message, dict) else None
        if role not in ('user', 'assistant') or not isinstance(text, str) or not text.strip():
            continue
        text = ' '.join(text.split())
        if len(text) > BRIEF_EACH:
            text = text[:BRIEF_EACH].rstrip() + '…'
        line = ('User: ' if role == 'user' else 'Agent: ') + text
        if total + len(line) > BRIEF_TOTAL:
            break
        picked.append(line)
        total += len(line)
        if len(picked) >= BRIEF_TURNS:
            break
    return '\n\n'.join(reversed(picked))


def prompt_text(entry, message, agent, *, context=False, review=False, recent=''):
    text = message.strip()
    if context:
        source = 'Claude' if entry['agent'] == 'claude' else 'Codex'
        head = f'Context: this task comes from another {source} chat in this project.'
        if recent:
            head += f' Its recent turns, cut short:\n\n{recent}\n\n'
        else:
            head += ' '
        head += f"Its full transcript (JSONL) is at {entry['transcript']}. Read it only if the task needs more.\n\nTask:\n"
        text = head + text
    if review:
        text += '\n\nWhen you finish, stop and wait: a reviewer in another tab reads your work and may send follow-ups.'
    return text


def write_prompt(text):
    directory = paths.CACHE / 'delegate'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    cutoff = time.time() - 7 * 24 * 60 * 60
    for old in directory.iterdir():
        try:
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink()
        except FileNotFoundError:  # another request may have pruned it
            pass
    path = directory / f'{uuid4().hex}.md'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(text)
    return path


def _check_profile(profile):
    if profile is not None and (not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile)):
        raise ValueError('Choose a supported Codex provider.')


def launch_command(entry, agent, path, name, *, context=False, profile=None):
    if agent == 'claude':
        command = 'claude'
    else:
        _check_profile(profile)
        command = 'codex' + (f' -p {shlex.quote(profile)}' if profile is not None else '')
    return command + f' "$(cat {shlex.quote(str(path))})"'


def start(entry, body, folder, run=subprocess.run, recent=lambda: []):
    """recent: returns the source chat's messages, read only when context is on."""
    # Call after validate, under the server's send lock. Only the new pane receives input.
    agent = body['agent']
    context, review = body.get('context', False), body.get('review', False)
    profile = codex_provider.launch_profile() if agent == 'codex' else None
    _check_profile(profile)
    name = task_name(body['message'])
    pane = new_window_after(entry['id'], run, cwd=folder)
    run([paths.TMUX, 'set-option', '-p', '-t', pane, '@chat_name', name, ';',
         'set-option', '-p', '-t', pane, '@chat_name_at', str(int(time.time() * 1000))],
        check=True, capture_output=True, timeout=5)
    path = write_prompt(prompt_text(entry, body['message'], agent, context=context, review=review, recent=brief(recent()) if context else ''))
    command = launch_command(entry, agent, path, name, context=context, profile=profile)
    # The pty buffers input even while the new shell starts.
    run([paths.TMUX, 'send-keys', '-t', pane, '-l', command], check=True, capture_output=True, timeout=5)
    run([paths.TMUX, 'send-keys', '-t', pane, 'Enter'], check=True, capture_output=True, timeout=5)
    if review:
        review_links.add(worker=pane, reviewer=entry['id'], reviewer_thread=entry.get('thread'), name=name, agent=agent)
    return pane, name
