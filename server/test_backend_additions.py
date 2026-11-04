"""Backend routes use in-memory requests and mocked tmux, never live panes."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

import history
import paths
import server

CHAT = '12345678-1234-1234-1234-123456789abc'
OTHER = '87654321-1234-1234-1234-123456789abc'


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.project = self.root / 'projects' / 'project'
        self.project.mkdir(parents=True)
        self.patch = patch.object(paths, 'CLAUDE', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        history._previews.clear()
        self.addCleanup(history._previews.clear)
        self.run = self.enterContext(patch.object(server.subprocess, 'run'))
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout='%13\n', stderr='')
        self.entries = self.enterContext(patch.object(server, 'sessions', return_value=[{'id': '%12', 'thread': CHAT, 'writable': True}]))
        self.verify = self.enterContext(patch.object(server, 'verify'))

    def transcript(self, records, identifier=CHAT):
        path = self.project / (identifier + '.jsonl')
        path.write_text('\n'.join(json.dumps(record) for record in records) + '\n')
        return path

    def request(self, route, body=None, method='POST', headers=None, denied=False):
        handler = object.__new__(server.Handler)
        handler.path = route
        handler.command = method
        payload = json.dumps(body).encode()
        handler.rfile = io.BytesIO(payload)
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test',
                           'X-Codex-Mobile': '1', 'Content-Length': str(len(payload))}
        if headers:
            handler.headers.update(headers)
        handler.respond = Mock()
        handler.deny = Mock(return_value=denied)
        getattr(handler, 'do_' + method)()
        return handler.respond.call_args.args if handler.respond.called else None

    def test_control_c_uses_existing_guards_and_tmux_key(self):
        self.assertEqual(self.request('/mobile/api/key', {'session': '%12', 'thread': CHAT, 'key': 'C-c'})[0], 200)
        self.verify.assert_called_once()
        self.assertEqual(self.run.call_args.args[0], [paths.TMUX, 'send-keys', '-t', '%12', 'C-c'])
        self.assertEqual(self.request('/mobile/api/key', {'session': '%12', 'key': 'C-c'})[0], 409)
        self.assertEqual(self.request('/mobile/api/key', {'session': '%12', 'thread': CHAT, 'key': 'C-z'})[0], 400)

    def test_double_escape_guard_uses_a_fake_clock_and_keeps_response_contract(self):
        import terminal_input as terminal
        entry = {'id': '%esc-guard', 'agent': 'claude', 'thread': CHAT, 'writable': True}
        self.entries.return_value = [entry]
        body = {'session': entry['id'], 'thread': CHAT, 'key': 'Escape'}
        screen = ['──────────\n❯ \n──────────\n']
        clock = [100.0]
        with patch.dict(terminal._escape_seen, clear=True), \
                patch.object(server.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(terminal, 'capture', side_effect=lambda *args, **kwargs: screen[0]), \
                patch('builtins.print') as log:
            def press():
                self.run.reset_mock()
                self.assertEqual(self.request('/mobile/api/key', body), (200, {'accepted': True}))
                return [call.args[0][-1] for call in self.run.call_args_list if 'send-keys' in call.args[0]]

            self.assertEqual(press(), ['Escape'])
            clock[0] += .5
            self.assertEqual(press(), [])
            log.assert_called_once()
            self.assertIn('Escape', log.call_args.args[0])
            clock[0] += 1.0
            self.assertEqual(press(), ['Escape'])
            clock[0] += .2
            screen[0] = '──────────\n❯ Keep this draft\n──────────\n'
            self.assertEqual(press(), ['Escape'])
            clock[0] += .2
            screen[0] = (Path(__file__).parent / 'test_fixtures/pickers/claude_rewind_150x39.txt').read_text()
            self.assertEqual(press(), ['Escape'])  # Esc must still cancel an open picker.
            clock[0] += .2
            screen[0] = 'No input box on screen'
            self.assertEqual(press(), ['Escape'])
            clock[0] += .2
            screen[0] = '──────────\n❯ \n──────────\n'
            entry['agent'] = 'codex'
            self.assertEqual(press(), ['Escape'])
            log.assert_called_once()

    def test_split_directions_do_not_require_thread_or_writable(self):
        self.entries.return_value = [{'id': '%12', 'thread': CHAT, 'writable': False}]
        for direction, flag in [('right', '-h'), ('down', '-v')]:
            self.assertEqual(self.request('/mobile/api/split', {'session': '%12', 'direction': direction}), (201, {'pane': '%13'}))
            self.assertEqual(self.run.call_args.args[0], [paths.TMUX, 'split-window', '-t', '%12', flag,
                                                        '-c', '#{pane_current_path}', '-P', '-F', '#{pane_id}'])
            self.assertEqual(self.run.call_args.kwargs['timeout'], 5)
        self.verify.assert_not_called()

    def test_pane_name_sets_and_clears_a_tmux_pane_option(self):
        self.assertEqual(self.request('/mobile/api/pane-name', {'pane': '%12', 'name': ' build logs '}), (200, {'pane': '%12', 'name': 'build logs'}))
        command = self.run.call_args_list[0].args[0]
        self.assertEqual(command[:7], [paths.TMUX, 'set-option', '-p', '-t', '%12', '@chat_name', 'build logs'])
        # The pane is alone in its tmux session, so the session takes the same name.
        self.assertEqual(self.run.call_args.args[0], [paths.TMUX, 'rename-session', '-t', '%12', 'build logs'])
        self.assertEqual(command[7:13], [';', 'set-option', '-p', '-t', '%12', '@chat_name_at'])
        self.assertTrue(command[13].isdigit())
        self.run.reset_mock()
        self.assertEqual(self.request('/mobile/api/pane-name', {'pane': '%12', 'name': ''})[0], 200)
        self.assertEqual(self.run.call_args_list[0].args[0], [paths.TMUX, 'set-option', '-p', '-t', '%12', '-u', '@chat_name', ';',
                                                        'set-option', '-p', '-t', '%12', '-u', '@chat_name_at'])
        self.run.reset_mock()
        for body in ({'pane': '$1', 'name': 'x'}, {'pane': '%12', 'name': 'x' * 41}, {'pane': '%12', 'name': 'a\nb'}, {'pane': '%12'}):
            self.assertEqual(self.request('/mobile/api/pane-name', body)[0], 400)
        self.run.assert_not_called()

    def test_pane_icon_sets_and_clears_a_tmux_pane_option(self):
        self.run.reset_mock()
        status, _ = self.request('/mobile/api/pane-icon', {'pane': '%3', 'icon': 'bug'})
        self.assertEqual(status, 200)
        command = self.run.call_args_list[0].args[0]
        self.assertEqual(command[1:7], ['set-option', '-p', '-t', '%3', '@chat_icon', 'bug'])
        self.assertIn('@chat_icon_by', command)
        self.run.reset_mock()
        status, _ = self.request('/mobile/api/pane-icon', {'pane': '%3', 'icon': ''})
        self.assertEqual(status, 200)
        self.assertIn('-u', self.run.call_args_list[0].args[0])
        status, _ = self.request('/mobile/api/pane-icon', {'pane': '%3', 'icon': 'Bad Icon!'})
        self.assertEqual(status, 400)

    def test_claude_draws_a_pane_icon_and_the_ui_can_load_it(self):
        with patch.object(server.pane_icons, 'draw', return_value='dabc') as draw:
            self.run.reset_mock()
            self.assertEqual(self.request('/mobile/api/pane-icon/draw', {'pane': '%3', 'label': 'api', 'hint': 'a plug'}), (200, {'pane': '%3', 'icon': 'svg-dabc'}))
            draw.assert_called_once_with('api', 'a plug')
            self.assertIn('svg-dabc', self.run.call_args_list[0].args[0])
        with patch.object(server.pane_icons, 'draw', side_effect=RuntimeError('Claude did not return a usable icon. Try again.')):
            self.assertEqual(self.request('/mobile/api/pane-icon/draw', {'pane': '%3', 'label': 'api'})[:2], (503, {'error': 'Claude did not return a usable icon. Try again.', 'draft': False}))
        with patch.object(server.pane_icons, 'read', return_value='<svg/>'):
            self.assertEqual(self.request('/mobile/api/icon/dabc.svg', method='GET'), (200, '<svg/>', 'image/svg+xml', 'max-age=300'))
        with patch.object(server.pane_icons, 'read', return_value=None):
            self.assertEqual(self.request('/mobile/api/icon/nope.svg', method='GET')[0], 404)

    def test_pane_name_shows_in_tmux_window_or_border(self):
        def panes(count):
            def run(command, **kwargs):
                return Mock(returncode=0, stdout=f'{count}\n' if '#{window_panes}' in command else '')
            return run
        commands = lambda: [call.args[0][1:] for call in self.run.call_args_list]
        self.run.side_effect = panes(1)
        self.assertEqual(self.request('/mobile/api/pane-name', {'pane': '%12', 'name': 'api'})[0], 200)
        self.assertIn(['rename-window', '-t', '%12', 'api', ';', 'set-option', '-w', '-t', '%12', 'automatic-rename', 'off'], commands())
        self.run.reset_mock()
        self.assertEqual(self.request('/mobile/api/pane-name', {'pane': '%12', 'name': ''})[0], 200)
        self.assertIn(['set-option', '-w', '-t', '%12', '-u', 'automatic-rename'], commands())
        self.run.reset_mock()
        self.run.side_effect = panes(2)
        self.assertEqual(self.request('/mobile/api/pane-name', {'pane': '%12', 'name': 'api'})[0], 200)
        self.assertIn(['set-option', '-w', '-t', '%12', 'pane-border-status', 'top', ';', 'set-option', '-w', '-t', '%12',
                       'pane-border-format', server.BORDER_FORMAT], commands())
        self.assertFalse(any(command[0] == 'rename-window' for command in commands()))  # several panes: the window keeps its name

    def test_one_name_per_pane(self):
        claude = {'id': '%12', 'tmux_session': '$1', 'label': 'web:0.0', 'agent': 'claude', 'writable': True, 'thread': CHAT}
        self.entries.return_value = [claude, {'id': '%13', 'tmux_session': '$1', 'label': 'web:0.1', 'agent': 'shell'}]
        with patch.object(server, 'rename_agent', return_value='') as agent:
            self.assertEqual(self.request('/mobile/api/pane-name', {'pane': '%12', 'name': 'api'}), (200, {'pane': '%12', 'name': 'api'}))
            agent.assert_called_once_with(claude, 'api')
            self.assertFalse(any(call.args[0][1] == 'rename-session' for call in self.run.call_args_list))  # two panes: the session keeps its name
            agent.reset_mock()
            self.run.reset_mock()
            self.assertEqual(self.request('/mobile/api/rename', {'pane': '%12', 'name': 'shop'}), (200, {'session': '%12', 'name': 'shop'}))
            agent.assert_not_called()  # a session name with two panes is not the agent's name
            self.entries.return_value = [claude]
            agent.return_value = 'Claude is busy, so its own name did not change. Rename again when it is idle.'
            self.run.reset_mock()
            status, body = self.request('/mobile/api/rename', {'pane': '%12', 'name': 'shop'})
            self.assertEqual((status, body['note'][:15]), (200, 'Claude is busy,'))
            agent.assert_called_once_with(claude, 'shop')
            self.assertEqual(self.run.call_args.args[0][1:7], ['set-option', '-p', '-t', '%12', '@chat_name', 'shop'])

    def test_usage_summary_reads_claude_and_codex_reports(self):
        import agents
        metrics = self.root / 'usage'
        metrics.mkdir()
        (metrics / 'a.json').write_text(json.dumps({'reported_at': 50, 'rate_limits': {
            'five_hour': {'used_percentage': 12, 'resets_at': 200}, 'seven_day': {'used_percentage': 40, 'resets_at': 50}}}))
        rates = self.root / 'codex_rates.json'
        rates.write_text(json.dumps({'windows': [{'label': '168h', 'percent': 48.0, 'resets_at': 300}], 'reported_at': 'T'}))
        with patch.object(agents, 'METRICS', metrics), patch.object(server, 'RATES_FILE', rates):
            report = server.usage_summary(now=100)
        self.assertEqual(report['claude'], {'reported_at': 50, 'windows': [
            {'label': '5h', 'percent': 12, 'resets_at': 200}, {'label': '7d', 'percent': 0, 'resets_at': None}]})
        self.assertEqual(report['codex'], {'reported_at': 'T', 'windows': [{'label': '7d', 'percent': 48.0, 'resets_at': 300}]})
        with patch.object(agents, 'METRICS', self.root / 'none'), patch.object(server, 'RATES_FILE', self.root / 'none.json'), \
                patch.object(server, 'recent_rates', return_value=([], None)):
            empty = server.usage_summary(now=100)
        self.assertEqual((empty['claude'], empty['codex']), (None, None))

    def test_split_rejects_bad_input_and_reports_tmux_errors(self):
        for body in ({}, {'session': '%12', 'direction': 'left'}, {'session': '$1', 'direction': 'right'},
                     {'session': '%99', 'direction': 'right'}, {'session': 12, 'direction': 'down'}):
            self.assertEqual(self.request('/mobile/api/split', body)[0], 400)
        self.run.assert_not_called()
        self.run.return_value = subprocess.CompletedProcess([], 1, stdout='', stderr='no space for new pane')
        status, body = self.request('/mobile/api/split', {'session': '%12', 'direction': 'down'})
        self.assertEqual((status, body['error']), (503, 'no space for new pane'))
        self.run.side_effect = subprocess.TimeoutExpired('tmux', 5)
        self.assertEqual(self.request('/mobile/api/split', {'session': '%12', 'direction': 'down'})[0], 503)

    def test_new_post_routes_use_auth_origin_and_header_guards(self):
        for route in ('/mobile/api/split', '/mobile/api/resume'):
            self.assertIsNone(self.request(route, {}, denied=True))
            for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
                self.assertEqual(self.request(route, {}, headers=headers)[0], 403)
        self.run.assert_not_called()
        self.entries.assert_not_called()

    def test_history_titles_cwd_live_order_and_limit(self):
        older = self.transcript([{'type': 'user', 'cwd': str(Path.home()), 'message': {'content': 'first'}},
                                 {'type': 'custom-title', 'customTitle': 'Old'},
                                 {'type': 'summary', 'summary': 'Summary'},
                                 {'type': 'ai-title', 'aiTitle': 'Newest'}])
        os.utime(older, (1, 1))
        self.transcript([{'type': 'user', 'message': {'content': '<command-name>ignore</command-name>\n hello   world'}}], OTHER)
        status, body = self.request('/mobile/api/history', method='GET')
        self.assertEqual(status, 200)
        chats = body['chats']
        self.assertEqual([chat['id'] for chat in chats], [OTHER, CHAT])
        self.assertEqual(chats[0]['title'], 'hello world')
        self.assertIsNone(chats[0]['dir'])
        self.assertFalse(chats[0]['live'])
        self.assertEqual((chats[1]['title'], chats[1]['dir'], chats[1]['live']), ('Newest', '~', True))
        self.assertTrue(chats[1]['time'].endswith('+00:00'))
        self.assertEqual(len(self.request('/mobile/api/history?limit=1', method='GET')[1]['chats']), 1)
        for limit in ('0', '101', 'nope'):
            self.assertEqual(self.request('/mobile/api/history?limit=' + limit, method='GET')[0], 400)
        self.assertIsNone(self.request('/mobile/api/history', method='GET', denied=True))

    def test_history_bounded_reads_cache_and_invalidation(self):
        path = self.transcript([{'type': 'user', 'cwd': '/old', 'message': {'content': 'First'}}])
        with path.open('ab') as stream:
            stream.write(b'x' * 200000 + b'\n{bad json}\nnull\n{"type": []}\n')
            stream.write(json.dumps({'type': 'custom-title', 'customTitle': 'Tail title', 'cwd': '/new'}).encode() + b'\n')
        with patch('builtins.open', wraps=open) as opened:
            self.assertEqual(history.recent_chats()[0]['title'], 'Tail title')
            self.assertEqual(history.recent_chats()[0]['cwd'], '/new')
            opened.assert_called_once()
        with patch('builtins.open', side_effect=AssertionError('cached files must not be read')):
            self.assertEqual(history.recent_chats()[0]['title'], 'Tail title')
        with path.open('a') as stream:
            stream.write(json.dumps({'type': 'ai-title', 'aiTitle': 'Updated'}) + '\n')
        self.assertEqual(history.find_chat(CHAT)['title'], 'Updated')
        # Enforce byte bounds, including on large files.
        real_open = open
        reads = []
        class Reader:
            def __init__(self, file):
                self.file = file
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.file.close()
            def seek(self, *args):
                return self.file.seek(*args)
            def read(self, size):
                reads.append(size)
                return self.file.read(size)
        history._previews.clear()
        with patch('builtins.open', side_effect=lambda *args: Reader(real_open(*args))):
            history.recent_chats()
        self.assertEqual(reads, [65536, 65536])

    def test_history_skips_subfolders_and_non_chat_files(self):
        sub = self.project / 'subagents'
        sub.mkdir()
        (sub / (CHAT + '.jsonl')).write_text('{}\n')
        (self.project / 'notes.jsonl').write_text('{}\n')
        self.assertEqual(history.recent_chats(), [])
        self.assertIsNone(history.find_chat(CHAT))
        path = self.transcript([{'type': 'user', 'isMeta': True, 'message': {'content': 'Skip'}},
                                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'Skip'}]}},
                                {'type': 'user', 'message': {'content': [{'type': 'text', 'text': 'word ' * 30}]}}])
        self.assertEqual(len(history.recent_chats()[0]['title']), 80)
        path.unlink()
        self.assertIsNone(history.find_chat(CHAT))

    def test_resume_uses_transcript_cwd_literal_text_enter_and_session_id(self):
        self.transcript([{'cwd': str(self.root)}])
        self.run.side_effect = [subprocess.CompletedProcess([], 0, stdout='%13\n', stderr=''),
                                subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
                                subprocess.CompletedProcess([], 0, stdout='$7\n')]
        self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'name': 'past-chat'}),
                         (201, {'session': '$7', 'pane': '%13', 'name': 'past-chat'}))
        commands = [call.args[0] for call in self.run.call_args_list]
        self.assertEqual(commands, [[paths.TMUX, 'new-session', '-d', '-s', 'past-chat', '-c', str(self.root), '-P', '-F', '#{pane_id}'],
                                    [paths.TMUX, 'send-keys', '-t', '%13', '-l', 'claude --resume ' + CHAT],
                                    [paths.TMUX, 'send-keys', '-t', '%13', 'Enter'],
                                    [paths.TMUX, 'display-message', '-p', '-t', '%13', '#{session_id}']])
        self.entries.assert_not_called()

    def test_resume_validation_fallback_duplicate_and_failures(self):
        for identifier in ('../bad', '', None, 123):
            self.assertEqual(self.request('/mobile/api/resume', {'id': identifier, 'name': 'ok'})[0], 400)
        self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'name': 'ok'})[0], 404)
        self.transcript([{'cwd': str(self.root / 'missing')}])
        for name in ('../bad', '-bad', '', 'x' * 65, None):
            self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'name': name})[0], 400)
        self.run.assert_not_called()
        for error, status in [('duplicate session: ok', 409), ('no server running', 503)]:
            self.run.return_value = subprocess.CompletedProcess([], 1, stdout='', stderr=error)
            self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'name': 'ok'})[0], status)
            self.assertEqual(self.run.call_args.args[0][6], str(paths.HOME))
        self.run.side_effect = subprocess.TimeoutExpired('tmux', 5)
        self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'name': 'ok'})[0], 503)


if __name__ == '__main__':
    unittest.main()
