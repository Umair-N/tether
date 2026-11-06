"""Agent commands use isolated review stores and mocked launchers and HTTP."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

import agent_tab_cli as cli
import agent_tabs
import auth
import delegate
import review_links
import server

THREAD = '0f8fad5b-d9cb-469f-a165-70867728950e'


class AgentTabsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(review_links, 'FILE', self.root / 'links.json'))
        self.entries = [dict(id='%12', agent='codex', thread=THREAD, writable=True,
                             transcript='/project/source.jsonl', pane_name='Source', label='Other name'),
                        dict(id='%13', agent='claude', thread=THREAD, writable=True, label='Reviewer'),
                        dict(id='%14', agent='claude', thread=None, pane_name='Second reviewer')]
        self.enterContext(patch.object(server, 'sessions', return_value=self.entries))
        self.enterContext(patch.object(server, 'agent_folder', return_value=str(self.root)))
        self.enterContext(patch.object(auth, 'session_value', return_value='test-credential'))
        self.run = self.enterContext(patch.object(server.subprocess, 'run'))

    def request(self, payload, headers=None, peer='127.0.0.1', denied=False):
        handler = object.__new__(server.Handler)
        handler.path, handler.command = agent_tabs.PATH, 'POST'
        handler.client_address = (peer, 1234)
        encoded = json.dumps(dict({'session': '%12'}, **payload)).encode()
        handler.rfile = io.BytesIO(encoded)
        handler.headers = {'Host': 'localhost', 'Origin': 'http://localhost', 'X-Codex-Mobile': '1',
                           'X-Tether-Hook': 'test-credential', 'Content-Length': str(len(encoded)), **(headers or {})}
        handler.deny = Mock(return_value=denied)
        handler.respond = Mock()
        handler.do_POST()
        return handler.respond.call_args.args if handler.respond.called else None

    def link(self, worker='%12', reviewer='%13'):
        review_links.add(worker=worker, reviewer=reviewer, reviewer_thread=THREAD, name='task', agent='codex')

    def test_hook_auth_rejects_missing_wrong_and_nonlocal_credentials(self):
        with patch.object(delegate, 'start') as start:
            for headers, peer in (({'X-Tether-Hook': ''}, '127.0.0.1'),
                                  ({'X-Tether-Hook': 'wrong'}, '127.0.0.1'),
                                  ({'X-Tether-Hook': 'é'}, '127.0.0.1'), ({}, '192.0.2.1')):
                with self.subTest(headers=headers, peer=peer):
                    self.assertEqual(self.request({'action': 'start'}, headers=headers, peer=peer),
                                     (403, {'error': 'Local hook authentication required.'}))
            for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
                self.assertEqual(self.request({'action': 'start'}, headers=headers)[0], 403)
            self.assertIsNone(self.request({'action': 'start'}, denied=True))
        start.assert_not_called()
        self.run.assert_not_called()

    def test_start_fills_thread_defaults_agent_and_holds_send_lock(self):
        def start(entry, body, folder, run, recent, cwd):
            self.assertTrue(server.SEND_LOCK.locked())
            self.assertIs(entry, self.entries[0])
            self.assertEqual((body['thread'], body['agent']), (THREAD, 'codex'))
            self.assertEqual((folder, cwd), (str(self.root), str(self.root)))
            self.assertIs(run, server.subprocess.run)
            self.assertEqual(recent(), [{'role': 'user', 'text': 'Recent task'}])
            return '%15', 'New tab'
        with patch.object(delegate, 'start', side_effect=start), \
                patch.object(server, 'snapshot', return_value={'messages': [{'role': 'user', 'text': 'Recent task'}]}):
            self.assertEqual(self.request({'action': 'start', 'message': '', 'review': 'new',
                                           'thread': 'ignored', 'name': 'New tab', 'cwd': str(self.root)}),
                             (201, {'pane': '%15', 'name': 'New tab'}))

    def test_agent_override_shell_default_and_large_body(self):
        with patch.object(delegate, 'start', return_value=('%15', 'task')) as start:
            self.assertEqual(self.request({'action': 'start', 'message': '😀' * 20000, 'agent': 'claude'})[0], 201)
            self.assertEqual(start.call_args.args[1]['agent'], 'claude')
            self.entries[0].update(agent='shell', thread=None)
            self.assertEqual(self.request({'action': 'start', 'message': 'task'})[0], 201)
            self.assertEqual(start.call_args.args[1]['agent'], 'claude')

    def test_validation_errors_and_unknown_session_do_not_launch(self):
        with patch.object(delegate, 'start') as start:
            self.assertEqual(self.request({'action': 'nope'}), (400, {'error': 'Unknown action.'}))
            self.assertEqual(self.request({'action': 'list', 'session': '%99'}),
                             (400, {'error': 'That session has ended. Select another tab.'}))
            self.assertEqual(self.request({'action': 'start', 'message': 'task', 'review': 1}),
                             (400, {'error': 'Context and review must be true or false.'}))
        start.assert_not_called()

    def test_bad_cwd_is_rejected_before_any_terminal_write(self):
        for cwd in (None, 1, 'relative', str(self.root / 'missing')):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.request({'action': 'start', 'message': 'task', 'cwd': cwd}),
                                 (400, {'error': 'Choose an absolute path to an existing directory.'}))
        self.run.assert_not_called()

    def test_stop_by_worker_removes_pair_or_all(self):
        self.link()
        self.link(reviewer='%14')
        self.assertEqual(self.request({'action': 'stop', 'worker': '%12', 'reviewer': '%13'}),
                         (200, {'stopped': True}))
        self.assertEqual(review_links.reviewed_by(), {'%12': ['%14']})
        self.assertEqual(self.request({'action': 'stop', 'worker': '%12'}), (200, {'stopped': True}))
        self.assertEqual(self.request({'action': 'stop', 'worker': '%12'}), (200, {'stopped': False}))
        self.assertEqual(review_links.reviewed_by(), {})

    def test_reviewer_stop_removes_only_its_own_link_with_or_without_reviewer(self):
        for extra in ({}, {'reviewer': '%12'}):
            with self.subTest(extra=extra):
                self.link(worker='%13', reviewer='%12')
                self.link(worker='%13', reviewer='%14')
                self.assertEqual(self.request(dict(action='stop', worker='%13', **extra)), (200, {'stopped': True}))
                self.assertEqual(review_links.reviewed_by(), {'%13': ['%14']})

    def test_foreign_stop_is_rejected_and_preserves_links(self):
        self.link(worker='%13', reviewer='%14')
        for extra in ({}, {'reviewer': '%14'}):
            self.assertEqual(self.request(dict(action='stop', worker='%13', **extra)),
                             (400, {'error': 'That review link is not on this tab.'}))
        for extra in ({'worker': 'bad'}, {'worker': '%13', 'reviewer': None}):
            self.assertEqual(self.request(dict(action='stop', **extra)),
                             (400, {'error': 'Select a tab to stop reviewing.'}))
        self.assertEqual(review_links.reviewed_by(), {'%13': ['%14']})

    def test_list_reports_both_sides_with_names_in_store_order(self):
        self.link()
        self.link(reviewer='%14')
        self.link(worker='%13', reviewer='%12')
        self.assertEqual(self.request({'action': 'list'}), (200, {
            'reviewing': [{'pane': '%13', 'name': 'Reviewer'}],
            'reviewers': [{'pane': '%13', 'name': 'Reviewer'}, {'pane': '%14', 'name': 'Second reviewer'}]}))
        self.assertEqual(self.request({'action': 'list', 'session': '%13'}), (200, {
            'reviewing': [{'pane': '%12', 'name': 'Source'}],
            'reviewers': [{'pane': '%12', 'name': 'Source'}]}))
        self.run.assert_not_called()

    def test_success_invalidates_session_cache(self):
        self.assertIn(agent_tabs.PATH, server.TMUX_WRITES)
        handler = object.__new__(server.Handler)
        handler.path, handler.command = agent_tabs.PATH, 'POST'
        handler.wfile = io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        with patch.object(server, 'forget_sessions') as forget:
            handler.respond(201, {'pane': '%15', 'name': 'task'})
        forget.assert_called_once_with()


class CLITests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'TMUX_PANE': '%12', 'MOBILE_TMUX_PORT': '12345'}))
        self.stdout = self.enterContext(patch.object(sys, 'stdout', new_callable=io.StringIO))
        self.stderr = self.enterContext(patch.object(sys, 'stderr', new_callable=io.StringIO))
        self.token = self.enterContext(patch.object(auth, 'TOKEN_FILE'))
        self.token.read_text.return_value = 'test-secret\n'
        self.credential = self.enterContext(patch.object(auth, 'session_value', return_value='derived-credential'))
        self.enterContext(patch.object(cli, 'install_settings', return_value={'port': 23456, 'ask_bridge': False}))
        self.opener = MagicMock()
        self.build = self.enterContext(patch.object(cli, 'build_opener', return_value=self.opener))
        self.opener.open.return_value.__enter__.return_value.read.return_value = b'{"pane": "%13", "name": "task"}'

    def payload(self):
        return json.loads(self.opener.open.call_args.args[0].data)

    def test_start_maps_arguments_and_uses_read_only_hook_credentials_without_proxy(self):
        self.assertEqual(cli.main(['start', '--agent', 'codex', '--review-it', '--context',
                                   '--name', 'Name', '--cwd', 'relative', 'task']), 0)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:12345/mobile/api/agent-tab')
        self.assertEqual(self.payload(), dict(action='start', session='%12', message='task', review='source',
                                             context=True, agent='codex', name='Name', cwd=os.path.abspath('relative')))
        for name, value in (('Origin', 'http://127.0.0.1:12345'), ('Content-type', 'application/json'),
                            ('X-codex-mobile', '1'), ('X-tether-hook', 'derived-credential'),
                            ('Cookie', auth.COOKIE + '=derived-credential')):
            self.assertEqual(request.get_header(name), value)
        self.assertEqual(self.opener.open.call_args.kwargs, {'timeout': 30})
        self.assertEqual(self.build.call_args.args[0].proxies, {})
        self.token.read_text.assert_called_once_with()
        self.credential.assert_called_once_with('test-secret')
        self.assertEqual(self.stdout.getvalue(), '{"pane": "%13", "name": "task"}\n')
        self.assertEqual(self.stderr.getvalue(), '')

    def test_reviews_me_allows_omitted_task_and_stdin_maps_to_message(self):
        self.assertEqual(cli.main(['start', '--reviews-me']), 0)
        self.assertEqual(self.payload(), dict(action='start', session='%12', message='', review='new', context=False))
        with patch.object(sys, 'stdin', io.StringIO('Task from stdin\n')):
            self.assertEqual(cli.main(['start', '-']), 0)
        self.assertEqual(self.payload()['message'], 'Task from stdin\n')

    def test_stop_and_list_and_settings_port(self):
        with patch.dict(os.environ, {'MOBILE_TMUX_PORT': ''}):
            self.assertEqual(cli.main(['stop', '--worker', '%13', '--reviewer', '%12']), 0)
        self.assertEqual(self.opener.open.call_args.args[0].full_url, 'http://127.0.0.1:23456/mobile/api/agent-tab')
        self.assertEqual(self.payload(), dict(action='stop', session='%12', worker='%13', reviewer='%12'))
        self.assertEqual(cli.main(['stop', '--worker', '%13']), 0)
        self.assertNotIn('reviewer', self.payload())
        self.assertEqual(cli.main(['list']), 0)
        self.assertEqual(self.payload(), dict(action='list', session='%12'))

    def test_missing_tmux_pane_never_opens_connection(self):
        with patch.dict(os.environ, {'TMUX_PANE': ''}):
            self.assertEqual(cli.main(['list']), 1)
        self.assertEqual(self.stderr.getvalue(), 'Run this inside a Tether tab.\n')
        self.opener.open.assert_not_called()
        self.token.read_text.assert_not_called()

    def test_invalid_arguments_return_one_with_tab_error(self):
        for args in (['start'], ['start', '--review-it'], ['start', '--review-it', '--reviews-me'],
                     ['start', '--agent', 'shell', 'task'], ['stop'], ['unknown']):
            with self.subTest(args=args):
                self.stderr.seek(0)
                self.stderr.truncate()
                self.assertEqual(cli.main(args), 1)
                self.assertTrue(self.stderr.getvalue().startswith('Tether tab error: '))
        self.opener.open.assert_not_called()

    def test_http_errors_print_server_text_or_code_and_never_credentials(self):
        for response, expected in ((b'{"error": "That session has ended."}', 'That session has ended.'),
                                   (b'not-json', '403'), (b'{}', '403'), (b'[]', '403')):
            with self.subTest(response=response):
                self.stderr.seek(0)
                self.stderr.truncate()
                self.opener.open.side_effect = HTTPError('http://localhost', 403, 'Forbidden', {}, io.BytesIO(response))
                self.assertEqual(cli.main(['list']), 1)
                self.assertEqual(self.stderr.getvalue(), f'Tether tab error: {expected}\n')

    def test_connection_and_local_config_errors_do_not_print_secrets(self):
        for error in (URLError('sensitive detail'), OSError('sensitive detail')):
            self.opener.open.side_effect = error
            self.assertEqual(cli.main(['list']), 1)
        self.assertNotIn('sensitive detail', self.stderr.getvalue())
        self.opener.open.reset_mock()
        self.token.read_text.side_effect = FileNotFoundError('sensitive path')
        self.assertEqual(cli.main(['list']), 1)
        self.opener.open.assert_not_called()
        self.assertNotIn('sensitive path', self.stderr.getvalue())

    def test_script_runs_by_absolute_path_from_another_folder(self):
        # No pane means this subprocess cannot contact a server or control tmux.
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(Path(cli.__file__).resolve()), 'list'], cwd=directory,
                                    env=dict(os.environ, TMUX_PANE=''), text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, 'Run this inside a Tether tab.\n')


if __name__ == '__main__':
    unittest.main()
