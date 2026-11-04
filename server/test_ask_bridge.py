"""Bridge tests use fake pane identities and an isolated, ephemeral HTTP server."""
import contextlib
import copy
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

import ask_bridge as bridge
import auth
import claude_hooks
import server
import terminal_input
import agents
from test_tts import handler


QUESTIONS = [
    {'question': 'Which library?', 'header': 'Library', 'multiSelect': False,
     'options': [{'label': 'React', 'description': 'Components', 'preview': '<p>Example</p>'},
                 {'label': 'Vue', 'description': 'Framework'}]},
    {'question': 'Which features?', 'header': 'Features', 'multiSelect': True,
     'options': [{'label': 'Search', 'description': ''}, {'label': 'Export', 'description': ''}]},
    {'question': 'Which name?', 'header': 'Name', 'options': [{'label': 'A'}, {'label': 'B'}]},
]
ANSWERS = {'Which library?': 'React', 'Which features?': ['Search', 'Export'], 'Which name?': 'My own name'}
ENTRY = dict(id='%9', thread='session-1', agent='claude', writable=True, owner_pid=123, process_start='start-1')
DATA = dict(hook_event_name='PreToolUse', tool_name='AskUserQuestion', tool_use_id='tool-1',
            session_id='session-1', tool_input={'questions': QUESTIONS, 'metadata': {'source': 'test'}})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.store = bridge.Pending(lambda: self.now)
        for name, value in [('hook_process', 'hook-start'), ('process_alive', True)]:
            p = patch.object(bridge, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.registration = dict(session='%9', session_id='session-1', nonce='nonce-12345678901234567890',
                                 tool_use_id='tool-1', tool_input=copy.deepcopy(DATA['tool_input']), pid=456)
        self.ask = self.store.register(ENTRY, self.registration)
        self.answer = dict(session='%9', **self.ask, answers=ANSWERS, requestId='tap-1')

    def test_one_answer_dedupes_before_and_after_delivery(self):
        self.assertEqual(self.store.answer(ENTRY, self.answer), {'status': 'submitted'})
        self.assertEqual(self.store.answer(ENTRY, self.answer), {'status': 'submitted'})
        out = self.store.poll(ENTRY, self.ask)
        self.assertEqual(out['answers'], dict(ANSWERS, **{'Which features?': 'Search, Export'}))
        self.assertEqual(self.store.answer(ENTRY, self.answer), {'status': 'submitted'})
        with self.assertRaises(bridge.Conflict):
            self.store.answer(ENTRY, dict(self.answer, requestId='tap-2'))
        with self.assertRaises(bridge.Conflict):
            self.store.answer(ENTRY, dict(self.answer, answers=dict(ANSWERS, **{'Which name?': 'changed'})))
        with self.assertRaises(bridge.Conflict):
            self.store.poll(ENTRY, self.ask)
        self.store.retire(ENTRY, self.ask)
        item = self.store.records[self.ask['id']]
        self.assertNotIn('questions', item)
        self.assertNotIn('answers', item)

    def test_attention_is_complete_and_does_not_mutate_payload(self):
        shown = self.store.attention(ENTRY)
        self.assertEqual(set(shown), {'id', 'nonce', 'questions', 'expires'})
        self.assertEqual(shown['questions'], QUESTIONS)
        shown['questions'][0]['question'] = 'changed'
        self.assertEqual(self.store.attention(ENTRY)['questions'], QUESTIONS)
        self.assertIsNone(self.store.attention(dict(ENTRY, thread='other')))

    def test_expiry_disconnect_and_reused_pane_reject_answers(self):
        for changed in [dict(ENTRY, thread='new'), dict(ENTRY, owner_pid=124), dict(ENTRY, process_start='new')]:
            with self.assertRaises(bridge.Conflict):
                self.store.answer(changed, self.answer)
        for bad in [dict(self.answer, nonce='other'), dict(self.answer, id='unknown')]:
            with self.assertRaises(bridge.Conflict):
                self.store.answer(ENTRY, bad)
        self.now += bridge.LEASE_SECONDS
        with self.assertRaises(bridge.Conflict):
            self.store.answer(ENTRY, self.answer)
        self.assertEqual(self.store.records, {})

    def test_absolute_expiry_even_with_heartbeat(self):
        self.now += bridge.WAIT_SECONDS - 1
        self.store.records[self.ask['id']]['lease'] = self.now + 5
        self.store.poll(ENTRY, self.ask)
        self.now += 1
        with self.assertRaises(bridge.Conflict):
            self.store.answer(ENTRY, self.answer)

    def test_dead_hook_retires_prompt(self):
        with patch.object(bridge, 'process_alive', return_value=False):
            self.assertIsNone(self.store.attention(ENTRY))
            with self.assertRaises(bridge.Conflict):
                self.store.answer(ENTRY, self.answer)

    def test_handoff_supplies_no_answer(self):
        self.assertEqual(self.store.answer(ENTRY, dict(self.answer, answers=None)), {'status': 'handoff'})
        self.assertIsNone(self.store.attention(ENTRY))
        with self.assertRaises(bridge.Conflict):
            self.store.poll(ENTRY, self.ask)
        with self.assertRaises(bridge.Conflict):
            self.store.answer(ENTRY, self.answer)

    def test_bad_answers_do_not_consume_the_nonce(self):
        missing = dict(self.answer)
        missing.pop('answers')
        with self.assertRaises(ValueError):
            self.store.answer(ENTRY, missing)
        for answers in [{}, [], {'other': 'React'}, dict(ANSWERS, **{'Which name?': ''}),
                        dict(ANSWERS, **{'Which library?': ['React']}),
                        dict(ANSWERS, **{'Which features?': ['Unknown']}),
                        dict(ANSWERS, **{'Which features?': ['Search', 'Search']}),
                        dict(ANSWERS, **{'Which name?': 'x' * bridge.MAX_ANSWER})]:
            with self.subTest(answers=type(answers).__name__), self.assertRaises(ValueError):
                self.store.answer(ENTRY, dict(self.answer, answers=answers))
        self.assertIsNotNone(self.store.attention(ENTRY))
        self.store.answer(ENTRY, self.answer)

    def test_bounds_and_duplicate_registration(self):
        with self.assertRaises(bridge.Conflict):
            self.store.register(ENTRY, self.registration)
        with patch.object(bridge, 'MAX_PENDING', 1), self.assertRaises(bridge.Conflict):
            self.store.register(dict(ENTRY, id='%10'), dict(self.registration, nonce='another-12345678901234567890'))
        for tool in [{'questions': []}, {'questions': QUESTIONS * 2}, {'questions': [{'kind': 'text'}]},
                     {'questions': QUESTIONS, 'metadata': 'x' * bridge.MAX_PAYLOAD},
                     {'questions': [QUESTIONS[0], QUESTIONS[0]]}]:
            with self.assertRaises(ValueError):
                bridge.questions_input(tool)

    def test_live_status_and_footer_expose_wait_without_busy_spinner(self):
        with patch.object(bridge, 'enabled', return_value=True), patch.object(bridge, 'pending', self.store), \
                patch.object(terminal_input, 'capture', return_value=''), \
                patch.object(terminal_input, 'login_capture', return_value=''), \
                patch.object(terminal_input, 'login_progress', return_value=None), \
                patch.object(terminal_input, '_live_status', return_value={'state': 'Working', 'approval': None}):
            status = terminal_input.live_status(ENTRY)
            self.assertEqual(status['state'], 'Input needed')
            self.assertEqual(status['attention']['askBridge']['questions'], QUESTIONS)
            with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
                footer = claude_hooks.overlay(ENTRY, {'spinner': {'verb': 'Working'}, 'running': {}})
            self.assertIsNone(footer['spinner'])
        with patch.object(bridge, 'enabled', return_value=False), patch.object(bridge, 'pending', self.store):
            self.assertEqual(bridge.overlay(ENTRY, {'state': 'Idle'}), {'state': 'Idle'})
            self.assertEqual(self.store.records, {})

    def test_general_status_cache_does_not_retain_question_payloads(self):
        status = {'state': 'Input needed', 'attention': {'askBridge': self.ask}}
        with patch.object(server, 'STATUS_CACHE', {}), patch.object(server, 'live_status', return_value=status):
            self.assertEqual(server.shared_status(ENTRY), status)
            self.assertNotIn(ENTRY['id'], server.STATUS_CACHE)


class HookTests(unittest.TestCase):
    def test_bridge_verifies_owner_registry_and_socket_without_keyboard_focus(self):
        entry = dict(ENTRY, registry='/isolated/registry.json')
        registry = {'pid': 123, 'sessionId': 'session-1'}
        def read(path, *args, **kwargs):
            if str(path).endswith('/comm'):
                return 'claude\n'
            if str(path).endswith('/stat'):
                return '123 (claude) S 100 0'
            return json.dumps(registry)
        with patch.object(agents, 'process_start', return_value='start-1'), \
                patch.object(Path, 'read_text', read), \
                patch.object(bridge.subprocess, 'run', return_value=Mock(stdout='100\t/private/socket\n')) as run:
            self.assertEqual(bridge.verify_entry(entry), '/private/socket')
            self.assertEqual(run.call_args.args[0][-1], '#{pane_pid}\t#{socket_path}')
            registry['sessionId'] = 'other'
            with self.assertRaises(bridge.Conflict):
                bridge.verify_entry(entry)
            registry['sessionId'] = 'session-1'
            with self.assertRaises(bridge.Conflict):
                bridge.verify_entry(dict(entry, process_start='old'))
            with self.assertRaises(bridge.Conflict):
                bridge.verify_entry(dict(entry, pending_conversation=True))

    def test_register_wait_answer_preserves_original_input(self):
        calls = []
        def request(body):
            calls.append(body)
            if body['action'] == 'register':
                return {'id': 'ask-1'}
            if body['action'] == 'poll':
                return {'state': 'pending'} if len(calls) == 2 else {'state': 'answered', 'answers': ANSWERS}
            return {'state': 'retired'}
        wait = Mock()
        original = copy.deepcopy(DATA)
        with patch.dict(os.environ, TMUX_PANE='%9'):
            result = bridge.hook(DATA, request, wait=wait)
        self.assertEqual(DATA, original)
        self.assertEqual([c['action'] for c in calls], ['register', 'poll', 'poll', 'retire'])
        self.assertEqual(calls[0]['tool_use_id'], 'tool-1')
        self.assertEqual(calls[0]['session_id'], 'session-1')
        self.assertEqual(calls[0]['session'], '%9')
        self.assertGreaterEqual(len(calls[0]['nonce']), 20)
        wait.assert_called_once_with(.5)
        output = result['hookSpecificOutput']
        self.assertEqual(output['permissionDecision'], 'allow')
        self.assertEqual(output['updatedInput'], dict(DATA['tool_input'], answers=dict(ANSWERS, **{'Which features?': 'Search, Export'})))

    def test_timeout_retires_before_empty_stdout_exit_zero(self):
        clock = Mock(side_effect=[0, 0, bridge.WAIT_SECONDS])
        request = Mock(side_effect=[{'id': 'ask'}, {'state': 'pending'}, {'state': 'retired'}])
        self.assertIsNone(bridge.hook(DATA, request, wait=Mock(), clock=clock))
        self.assertEqual(request.call_args.args[0]['action'], 'retire')
        with patch.object(bridge, 'hook', return_value=None), patch('sys.stdin', io.StringIO(json.dumps(DATA))), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertIsNone(bridge.main())
        self.assertEqual(stdout.getvalue(), '')

    def test_failures_never_emit_allow_or_answers(self):
        for request in [Mock(side_effect=ConnectionRefusedError),
                        Mock(side_effect=[{'id': 'ask'}, KeyboardInterrupt, {}]),
                        Mock(side_effect=[{'id': 'ask'}, TimeoutError, {}]),
                        Mock(side_effect=[{'id': 'ask'}, {'state': 'retired'}, {}]),
                        Mock(side_effect=[{'id': 'ask'}, {'state': 'answered', 'answers': {}}, {}])]:
            self.assertIsNone(bridge.hook(DATA, request, wait=Mock()))
        request = Mock()
        self.assertIsNone(bridge.hook(dict(DATA, agent_id='child'), request))
        request.assert_not_called()

    def test_server_down_subprocess_falls_through_fast_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = home / 'config'
            config.mkdir()
            (config / 'token').write_text('isolated-test-token')
            state = home / 'install.json'
            # A closed kernel-assigned port, never a production port.
            import socket
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            state.write_text(json.dumps({'options': {'ask_bridge': True, 'port': port}}))
            env = dict(os.environ, HOME=folder, CLAUDE_CONFIG_DIR=str(home / 'claude'), CODEX_HOME=str(home / 'codex'),
                       MOBILE_TMUX_HOME=folder, MOBILE_TMUX_CONFIG=str(config), TETHER_INSTALL_STATE=str(state))
            env.pop('MOBILE_TMUX_PORT', None)
            env.pop('TMUX', None)  # the hook reads the client socket; keep it out of these tests
            before = {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()}
            start = time.monotonic()
            result = subprocess.run([sys.executable, str(Path(bridge.__file__))], input=json.dumps(DATA),
                                    capture_output=True, text=True, env=env, timeout=5)
            self.assertLess(time.monotonic() - start, 3)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
            self.assertEqual(before, {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()})


class RouteTests(unittest.TestCase):
    setUp = StoreTests.setUp
    # Reuse the isolated record for the real server handler, never a terminal.
    def post(self, body, path=bridge.ANSWER_PATH, **headers):
        value = handler(path, body)
        value.client_address = ('127.0.0.1', 1234)
        value.headers.update(headers)
        with patch.object(bridge, 'enabled', return_value=True), patch.object(bridge, 'pending', self.store), \
                patch.object(server, 'choose', return_value=ENTRY), patch.object(bridge, 'verify_entry', return_value='') as verify:
            value.do_POST()
        return value.send_response.call_args.args[0], json.loads(value.wfile.getvalue()), verify

    def test_route_second_answer_409_duplicate_200(self):
        self.assertEqual(self.post(self.answer)[0], 200)
        self.assertEqual(self.post(self.answer)[0], 200)
        self.assertEqual(self.post(dict(self.answer, requestId='tap-2'))[0], 409)
        self.assertEqual(self.post(dict(self.answer, answers={}))[0], 409)

    def test_auth_origin_and_write_header_precede_bridge(self):
        value = handler(bridge.ANSWER_PATH, self.answer)
        value.deny.return_value = True
        with patch.object(bridge, 'serve') as serve:
            value.do_POST()
        serve.assert_not_called()
        for headers in [{'Origin': 'https://evil.test'}, {'X-Codex-Mobile': '0'}]:
            status, _, verify = self.post(self.answer, **headers)
            self.assertEqual(status, 403)
            verify.assert_not_called()
        self.assertEqual(self.post(dict(self.ask, session='%9', action='poll'), bridge.HOOK_PATH)[0], 403)

    def test_expired_and_stale_identity_are_409(self):
        self.now += bridge.WAIT_SECONDS
        self.assertEqual(self.post(self.answer)[0], 409)
        with patch.object(bridge, 'verify_entry', side_effect=RuntimeError('stale')):
            value = handler(bridge.ANSWER_PATH, self.answer)
            with patch.object(bridge, 'enabled', return_value=True), patch.object(server, 'choose', return_value=ENTRY):
                value.do_POST()
            self.assertEqual(value.send_response.call_args.args[0], 409)

    def test_internal_route_requires_token_and_matching_socket(self):
        body = dict(self.registration, action='register', socket='wrong-socket')
        value = handler(bridge.HOOK_PATH, body)
        value.client_address = ('127.0.0.1', 1)
        value.headers['X-Tether-Hook'] = 'credential'
        with patch.object(bridge, 'enabled', return_value=True), patch.object(bridge, 'pending', self.store), \
                patch.object(server, 'choose', return_value=ENTRY), \
                patch.object(bridge, 'verify_entry', return_value='/private/socket'), \
                patch.object(auth, 'session_value', return_value='credential'):
            value.do_POST()
        self.assertEqual(value.send_response.call_args.args[0], 409)

    def test_bad_nonce_types_return_400(self):
        self.assertEqual(self.post(dict(self.answer, id=[]))[0], 400)
        self.assertEqual(self.post(dict(self.answer, nonce={}))[0], 400)


class HttpRoundtripTests(unittest.TestCase):
    def test_real_hook_process_registers_and_receives_route_answer(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            token = home / 'token'
            token.write_text('isolated-test-token')
            state = home / 'install.json'
            store = bridge.Pending()
            entry = dict(ENTRY, owner_pid=os.getpid(), process_start='mocked-owner')
            class QuietHandler(server.Handler):
                def log_message(self, *_):
                    pass
            httpd = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
            worker = threading.Thread(target=httpd.serve_forever)
            state.write_text(json.dumps({'options': {'ask_bridge': True, 'port': httpd.server_port}}))
            env = dict(os.environ, HOME=folder, CLAUDE_CONFIG_DIR=str(home / 'claude'), CODEX_HOME=str(home / 'codex'),
                       MOBILE_TMUX_HOME=folder, MOBILE_TMUX_CONFIG=folder, TETHER_INSTALL_STATE=str(state), TMUX_PANE='%9')
            env.pop('MOBILE_TMUX_PORT', None)
            env.pop('TMUX', None)  # verify_entry is mocked to the empty socket
            process = None
            with patch.object(bridge, 'pending', store), patch.object(bridge, 'enabled', return_value=True), \
                    patch.object(server, 'choose', return_value=entry), patch.object(bridge, 'verify_entry', return_value=''), \
                    patch.object(auth, 'TOKEN_FILE', token), patch.object(auth, 'MODE', 'token'):
                worker.start()
                try:
                    process = subprocess.Popen([sys.executable, str(Path(bridge.__file__))], stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                    process.stdin.write(json.dumps(DATA))
                    process.stdin.close()
                    process.stdin = None
                    deadline = time.monotonic() + 5
                    ask = None
                    while time.monotonic() < deadline:
                        ask = store.attention(entry)
                        if ask:
                            break
                        time.sleep(.02)
                    self.assertIsNotNone(ask)
                    connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
                    try:
                        url = f'http://127.0.0.1:{httpd.server_port}'
                        connection.request('POST', bridge.ANSWER_PATH, json.dumps(dict(session='%9', id=ask['id'], nonce=ask['nonce'],
                            answers=ANSWERS, requestId='http-tap')), headers={'Origin': url, 'X-Codex-Mobile': '1',
                            'Cookie': auth.COOKIE + '=' + auth.session_value()})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.loads(response.read()), {'status': 'submitted'})
                    finally:
                        connection.close()
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual((process.returncode, stderr), (0, ''))
                    self.assertEqual(json.loads(stdout)['hookSpecificOutput']['updatedInput']['answers']['Which features?'], 'Search, Export')
                    self.assertIsNone(store.attention(entry))
                    self.assertEqual(set(p.name for p in home.iterdir()), {'token', 'install.json'})
                finally:
                    if process and process.poll() is None:
                        process.kill()
                        process.communicate(timeout=3)
                    httpd.shutdown()
                    httpd.server_close()
                    worker.join(timeout=3)
                    self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
