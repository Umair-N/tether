"""CLI-owned sign-in, isolated from the user's tmux server. No transcript leaves this module."""
import os
import re
import shlex
import subprocess
import threading
import time
import unicodedata
import uuid
from urllib.parse import urlsplit

import accounts
import paths

HOSTS = {'claude': {'claude.ai', 'claude.com', 'platform.claude.com', 'anthropic.com', 'console.anthropic.com'},
         'codex': {'auth.openai.com', 'chatgpt.com', 'openai.com'}}
ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')
LIMIT = 600
URL_PART = re.compile(r"^[A-Za-z0-9%._~:/?#\[\]@!$&'()*+,;=-]+$")
CODE_ERROR = re.compile(r'invalid (?:authentication |authorization |sign.in )?code|code (?:is )?(?:invalid|expired)|authentication failed|sign.in failed', re.I)


def agent_name(agent):
    if not isinstance(agent, str) or agent not in HOSTS:
        raise ValueError('Select Claude or Codex.')
    return agent


def plain(value, maximum, label):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(unicodedata.category(c).startswith('C') for c in value):
        raise ValueError(f'{label} must contain 1–{maximum} characters and no control characters.')
    return value


def command(agent, method=None, email=None):
    agent_name(agent)
    if agent == 'codex':
        if method is not None or email is not None:
            raise ValueError('Codex uses device sign-in without a method or email.')
        return ['codex', 'login', '--device-auth']
    method = 'claudeai' if method is None else method
    if method not in ('claudeai', 'console', 'sso'):
        raise ValueError('Select Claude subscription, Anthropic Console, or SSO.')
    result = ['claude', 'auth', 'login', '--' + method]
    if email is not None and email != '':
        result += ['--email', plain(email, 320, 'Email')]
    return result


def parse_screen(agent, screen):
    agent_name(agent)
    screen = ANSI.sub('', screen)
    url = None
    lines = screen.splitlines()
    for index, line in enumerate(lines):
        match = re.search(r'https://[^\s<>\x00-\x1f]+', line)
        if not match:
            continue
        candidate = match.group()
        # Some CLI versions print separate lines, not terminal soft wraps (-J).
        for more in lines[index + 1:]:
            if not URL_PART.fullmatch(more.strip()):
                break
            candidate += more.strip()
        candidate = candidate.rstrip(').,;\'"')
        try:
            parts = urlsplit(candidate)
            if parts.hostname in HOSTS[agent] and not parts.username and not parts.password and parts.port in (None, 443):
                url = candidate
                break
        except ValueError:
            pass
    # Device codes are displayed alone or after a label; never return arbitrary terminal text.
    # Codex 0.165 prints "2. Enter this one-time code" and the code on the same or the next line.
    code = re.search(r'(?i)one-time\s+code\b.{0,160}?\b([A-Z0-9]{3,10}(?:-[A-Z0-9]{3,10}){1,3})\b', ' '.join(screen.split())) if agent == 'codex' else None
    return {'url': url, 'user_code': code.group(1) if code else None}


def empty(phase='idle', message=''):
    return {'phase': phase, 'url': None, 'user_code': None, 'message': message, 'account': None}


class LoginRunner:
    def __init__(self):
        self.socket = 'tether-login-' + uuid.uuid4().hex
        self.flows = {}
        self.lock = threading.RLock()

    def tmux(self, *args, missing_ok=False):
        env = dict(os.environ)
        env.pop('TMUX', None)
        try:
            result = subprocess.run([paths.TMUX, '-L', self.socket, '-f', '/dev/null', *args],
                                    capture_output=True, text=True, timeout=5, env=env)
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError('Could not reach the login session.') from None
        if result.returncode:
            if missing_ok and re.search(r"can't find session|no server running|No such file or directory", result.stderr or ''):
                return ''
            raise RuntimeError('Could not reach the login session.')
        return result.stdout.strip()

    def start(self, agent, method=None, email=None):
        argv = command(agent, method, email)
        with self.lock:
            old = self.flows.get(agent)
            if old and old['session']:
                raise ValueError('A sign-in is already active. Cancel it before starting again.')
            # Set remain-on-exit before exec, so an early CLI failure still has a reliable exit status.
            setup = shlex.join([paths.TMUX, '-L', self.socket, 'set-option', '-p', 'remain-on-exit', 'on'])
            script = setup + ' && exec ' + shlex.join(['env', 'BROWSER=true', *argv])
            session = self.tmux('new-session', '-d', '-P', '-F', '#{session_id}', '-s', 'tether-login-' + agent,
                                '-x', '300', '-y', '60', script)
            if not re.fullmatch(r'\$\d+', session):
                raise RuntimeError('Could not create the login session.')
            flow = {'session': session, 'deadline': time.monotonic() + LIMIT, 'state': empty('waiting_browser', 'Starting sign-in…'), 'submitted': False}
            self.flows[agent] = flow
            threading.Thread(target=self.watch, args=(agent, flow), daemon=True, name='login-' + agent).start()
            return dict(flow['state'])

    def finish(self, flow, phase, message, account=None):
        if flow['session']:
            self.tmux('kill-session', '-t', flow['session'], missing_ok=True)
            flow['session'] = None
        flow['state'] = dict(empty(phase, message), account=account)

    def update(self, agent, flow):
        if not flow['session']:
            return
        if time.monotonic() >= flow['deadline']:
            self.finish(flow, 'failed', 'Sign-in timed out after 10 minutes. Try again.')
            return
        target = flow['session'] + ':0.0'
        dead = self.tmux('display-message', '-p', '-t', target, '#{pane_dead}:#{pane_dead_status}')
        if dead.startswith('1:'):
            success = dead == '1:0'
            self.finish(flow, 'done' if success else 'failed', 'Signed in.' if success else 'Sign-in failed. Try again.',
                        getattr(accounts, agent)() if success else None)
            return
        screen = self.tmux('capture-pane', '-p', '-J', '-S', '-200', '-t', target)
        failed_code = bool(CODE_ERROR.search(screen))
        if flow['submitted'] and failed_code:
            self.finish(flow, 'failed', 'Sign-in code failed. Start sign-in again.')
            return
        if not flow['submitted'] or failed_code:
            parsed = parse_screen(agent, screen)
            flow['state'].update({key: value or flow['state'][key] for key, value in parsed.items()})
            ready = flow['state']['url']
            flow['state'].update(phase='waiting_code' if agent == 'claude' and ready else 'waiting_browser',
                                 message='Sign-in code failed. Check the code and try again.' if failed_code else
                                 ('Open the sign-in link, then paste the code here.' if agent == 'claude' else 'Open the sign-in link and enter the device code.') if ready else 'Waiting for the CLI sign-in link…')

    def watch(self, agent, flow):
        while True:
            with self.lock:
                if not flow['session']:
                    return
                try:
                    self.update(agent, flow)
                except RuntimeError:
                    # Keep ownership until cleanup succeeds; never expose subprocess output.
                    try:
                        self.finish(flow, 'failed', 'The login session stopped. Try again.')
                    except RuntimeError:
                        flow['state'] = empty('failed', 'Could not close the login session. Cancel to retry.')
                if not flow['session'] or time.monotonic() >= flow['deadline'] + 5:
                    return
            time.sleep(.5)

    def state(self, agent):
        agent_name(agent)
        with self.lock:
            return dict(self.flows[agent]['state']) if agent in self.flows else empty()

    def code(self, agent, code):
        agent_name(agent)
        code = plain(code, 2000, 'Code')
        with self.lock:
            flow = self.flows.get(agent)
            if agent != 'claude' or not flow or not flow['session']:
                raise ValueError('No Claude sign-in is waiting for a code.')
            self.update(agent, flow)
            if flow['state']['phase'] != 'waiting_code':
                raise ValueError('The login session is not waiting for a code.')
            target = flow['session'] + ':0.0'
            self.tmux('send-keys', '-t', target, '-l', '--', code)
            self.tmux('send-keys', '-t', target, 'Enter')
            flow['submitted'] = True
            flow['state'].update(phase='waiting_browser', message='Checking the sign-in code…')
            return dict(flow['state'])

    def cancel(self, agent):
        agent_name(agent)
        with self.lock:
            flow = self.flows.get(agent)
            if flow:
                self.finish(flow, 'idle', 'Sign-in cancelled.')
                return dict(flow['state'])
            return empty()


runner = LoginRunner()
