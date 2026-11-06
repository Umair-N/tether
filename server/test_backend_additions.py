"""Backend routes use in-memory requests and mocked tmux, never live panes."""
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.parse import urlencode

import history
import paths
import server
import terminal_input

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
        self.enterContext(patch.object(paths, 'CODEX', self.root / 'codex'))
        self.enterContext(patch.object(paths, 'HOME', self.root))
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

    def codex_database(self, rows, columns=None, version=5):
        paths.CODEX.mkdir(exist_ok=True)
        database = paths.CODEX / f'state_{version}.sqlite'
        columns = columns or {'id': 'TEXT', 'name': 'TEXT', 'title': 'TEXT', 'first_user_message': 'TEXT',
                              'cwd': 'TEXT', 'updated_at_ms': 'INT', 'archived': 'INT',
                              'source': 'TEXT', 'has_user_event': 'INT'}
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE threads (' + ', '.join(f'{key} {kind}' for key, kind in columns.items()) + ')')
            for row in rows:
                values = dict(id=CHAT, title='Codex chat', cwd=str(self.root), updated_at_ms=1000, archived=0, source='cli', has_user_event=0)
                values.update(row)
                connection.execute('INSERT INTO threads VALUES (' + ','.join('?' for _ in columns) + ')',
                                   [values.get(key) for key in columns])
        return database

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

    def test_stop_escape_returns_restored_text_under_send_lock(self):
        import terminal_input as terminal
        entry = {'id': '%12', 'agent': 'claude', 'thread': CHAT, 'writable': True}
        self.entries.return_value = [entry]
        body = {'session': '%12', 'thread': CHAT, 'key': 'Escape', 'stop': True}

        def stop(config):
            self.assertIs(config, entry)
            self.assertTrue(server.SEND_LOCK.locked())
            self.verify.assert_called_once_with(entry)
            return {'restored': 'write about rivers'}

        with patch.object(terminal, 'stop_turn', side_effect=stop) as restore:
            self.assertEqual(self.request('/mobile/api/key', body),
                             (200, {'accepted': True, 'restored': 'write about rivers'}))
            restore.assert_called_once_with(entry)
            self.run.assert_not_called()
            self.assertEqual(self.request('/mobile/api/key', {**body, 'thread': OTHER})[0], 409)
            self.assertEqual(self.request('/mobile/api/key', body, headers={'X-Codex-Mobile': ''})[0], 403)
            self.assertEqual(restore.call_count, 1)

    def test_stop_restoration_requires_claude_escape_and_literal_true(self):
        import terminal_input as terminal
        entry = {'id': '%12', 'agent': 'claude', 'thread': CHAT, 'writable': True}
        self.entries.return_value = [entry]
        with patch.object(terminal, 'stop_turn') as restore, \
                patch.object(terminal, 'drop_double_escape', return_value=False):
            for agent, key, flag in (('claude', 'Escape', 1), ('claude', 'Escape', False),
                                     ('claude', 'C-c', True), ('codex', 'Escape', True),
                                     ('antigravity', 'Escape', True)):
                entry['agent'] = agent
                body = {'session': '%12', 'thread': CHAT, 'key': key, 'stop': flag}
                self.assertEqual(self.request('/mobile/api/key', body), (200, {'accepted': True}))
                self.assertEqual(self.run.call_args.args[0][-1], key)
            restore.assert_not_called()

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
        for direction in ('right', 'down'):
            self.run.reset_mock()
            def run(command, **kwargs):
                self.assertTrue(server.SEND_LOCK.locked())
                output = ('@7\t/a folder\n' if '#{window_id}\t#{pane_current_path}' in command
                          else 'manual\t160\t48\n' if '#{window-size}\t#{window_width}\t#{window_height}' in command
                          else '%13\n')
                return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')
            self.run.side_effect = run
            self.assertEqual(self.request('/mobile/api/split', {'session': '%12', 'direction': direction}), (201, {'pane': '%13'}))
            self.assertEqual([call.args[0] for call in self.run.call_args_list], [
                [paths.TMUX, 'display-message', '-p', '-t', '%12', '#{window_id}\t#{pane_current_path}'],
                [paths.TMUX, 'display-message', '-p', '-t', '@7', '#{window-size}\t#{window_width}\t#{window_height}'],
                [paths.TMUX, 'new-window', '-a', '-t', '@7', '-c', '/a folder', '-P', '-F', '#{pane_id}'],
                [paths.TMUX, 'resize-window', '-t', '%13', '-x', '160', '-y', '48'],
            ])
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
        for body in ({}, {'session': '%12'}, {'session': '%12', 'direction': 'left'}, {'session': '$1', 'direction': 'right'},
                     {'session': '%99', 'direction': 'right'}, {'session': 12, 'direction': 'down'}):
            self.assertEqual(self.request('/mobile/api/split', body)[0], 400)
        self.run.assert_not_called()
        self.run.return_value = subprocess.CompletedProcess([], 1, stdout='', stderr='no space for new pane')
        status, body = self.request('/mobile/api/split', {'session': '%12', 'direction': 'down'})
        self.assertEqual((status, body['error']), (503, 'no space for new pane'))
        self.run.side_effect = subprocess.TimeoutExpired('tmux', 5)
        self.assertEqual(self.request('/mobile/api/split', {'session': '%12', 'direction': 'down'})[0], 503)

    def test_split_keeps_fallback_error_and_pane_confirmation(self):
        self.run.return_value = subprocess.CompletedProcess([], 1, stdout='', stderr='')
        status, body = self.request('/mobile/api/split', {'session': '%12', 'direction': 'right'})
        self.assertEqual((status, body['error']), (503, 'Could not split the pane.'))
        for output in ('', '@13\n', '%13\n%14\n'):
            with self.subTest(output=output):
                self.run.side_effect = [
                    subprocess.CompletedProcess([], 0, stdout='@7\t/work\n', stderr=''),
                    subprocess.CompletedProcess([], 0, stdout='latest\t120\t40\n', stderr=''),
                    subprocess.CompletedProcess([], 0, stdout=output, stderr=''),
                ]
                status, body = self.request('/mobile/api/split', {'session': '%12', 'direction': 'right'})
                self.assertEqual((status, body['error']), (503, 'Pane creation could not be confirmed.'))

    def test_split_rejects_unconfirmed_source_window(self):
        for output in ('', '%12\t/work\n', '@7', '@7x\t/work\n', '@7\t\n'):
            with self.subTest(output=output):
                self.run.reset_mock()
                self.run.return_value = subprocess.CompletedProcess([], 0, stdout=output, stderr='')
                status, body = self.request('/mobile/api/split', {'session': '%12', 'direction': 'down'})
                self.assertEqual((status, body['error']), (503, 'Could not split the pane.'))
                self.assertEqual(self.run.call_count, 1)

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
        self.entries.return_value = []
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
        self.entries.assert_called_once()

    def test_resume_validation_fallback_duplicate_and_failures(self):
        self.entries.return_value = []
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

    def test_codex_history_interactive_rows_titles_and_search_text(self):
        ids = [f'00000000-0000-0000-0000-{number:012x}' for number in range(10)]
        self.codex_database([
            {'id': ids[0], 'name': '  My\n name  ', 'title': 'ignored', 'first_user_message': 'search ' * 60},
            {'id': ids[1], 'source': 'vscode', 'name': ' ', 'title': ' A\t title '},
            {'id': ids[2], 'title': None, 'first_user_message': ' first\n user ' + 'x' * 90},
            {'id': ids[3], 'title': 'Plain title'},
            {'id': ids[4], 'source': 'exec'},
            {'id': ids[5], 'source': 'subagent'},
            {'id': ids[6], 'source': '{"subagent":{"thread_spawn":{}}}'},
            {'id': ids[7], 'archived': 1},
            {'id': ids[8], 'title': None},  # an empty thread has no text at all
            {'id': ids[9], 'source': 'other'},
        ])
        self.transcript([{'type': 'user', 'message': {'content': 'Claude text'}}])
        chats = history.all_chats()
        codex = [chat for chat in chats if chat['agent'] == 'codex']
        self.assertEqual([chat['id'] for chat in codex], ids[:4])
        self.assertEqual([chat['title'] for chat in codex], ['My name', 'A title', ('first user ' + 'x' * 90)[:80], 'Plain title'])
        self.assertEqual(codex[0]['text'], ('search ' * 60)[:300])
        self.assertEqual(codex[0]['time'], '1970-01-01T00:00:01+00:00')
        self.assertEqual(codex[0]['cwd'], str(self.root))
        self.assertEqual(history.find_chat(ids[0], agent='codex'), codex[0])
        for identifier in ids[4:]:
            self.assertIsNone(history.find_chat(identifier, agent='codex'))
        self.assertEqual(chats[0]['agent'], 'claude')
        self.assertEqual(chats[0]['text'], 'Claude text')
        self.run.assert_not_called()

    def test_codex_history_missing_unreadable_and_older_schemas(self):
        self.assertEqual(history.all_chats(), [])
        self.assertIsNone(history.find_chat(CHAT, agent='codex'))
        columns = {'id': 'TEXT', 'cwd': 'TEXT', 'title': 'TEXT', 'updated_at_ms': 'INT',
                   'archived': 'INT', 'source': 'TEXT'}
        self.codex_database([{'title': 'Old title'}, {'id': OTHER, 'source': 'exec'}], columns, version=9)
        self.assertEqual([chat['title'] for chat in history.all_chats()], ['Old title'])
        # Choose the newest schema number, as agents.codex_database does.
        self.codex_database([{'name': 'New title'}], version=10)
        self.assertEqual(history.find_chat(CHAT, agent='codex')['title'], 'New title')
        self.codex_database([{}], {'id': 'TEXT', 'cwd': 'TEXT'}, version=11)
        chat = history.find_chat(CHAT, agent='codex')
        self.assertEqual((chat['title'], chat['text'], chat['time']), (CHAT, '', None))
        self.codex_database([{}], {'cwd': 'TEXT'}, version=12)
        self.assertEqual(history.all_chats(), [])
        (paths.CODEX / 'state_13.sqlite').write_bytes(b'not a database')
        self.assertEqual(history.all_chats(), [])
        self.assertIsNone(history.find_chat(CHAT, agent='codex'))
        with patch.object(history.sqlite3, 'connect', side_effect=sqlite3.OperationalError('unreadable')):
            self.assertEqual(history.all_chats(), [])

    def test_all_claude_chats_and_long_first_user_text(self):
        for number in range(35):
            identifier = f'00000000-0000-0000-0000-{number:012x}'
            self.transcript([{'type': 'user', 'isSidechain': True, 'message': {'content': 'skip'}},
                             {'type': 'user', 'message': {'content': 'a' * 90 + ' hidden ' + 'b' * 240}}], identifier)
        self.assertEqual(len(history.all_chats()), 35)
        self.assertEqual(len(history.recent_chats()), 30)
        chat = history.all_chats()[0]
        self.assertEqual(chat['title'], 'a' * 80)
        self.assertEqual(len(chat['text']), 300)
        self.assertIn('hidden', chat['text'])
        self.assertEqual(len(history.search_chats(history.all_chats(), q='hidden')), 35)

    def test_search_chats_terms_agent_state_and_displayed_directory(self):
        chats = [dict(id=CHAT, agent='claude', title='Build API', text='Hidden NEEDLE', cwd='/Users/test/app', time='2026-01-01'),
                 dict(id=OTHER, agent='codex', title='Build tests', text='needle', cwd='/Users/test/other', time='2026-02-01'),
                 dict(id='third', agent='codex', title='API', text='', cwd=None, time=None)]
        self.assertEqual(history.search_chats(chats), [chats[1], chats[0], chats[2]])
        self.assertEqual(history.search_chats(chats, q='  BUILD\tneedle APP '), [chats[0]])
        self.assertEqual(history.search_chats(chats, q='build absent'), [])
        self.assertEqual(history.search_chats(chats, agent='codex'), [chats[1], chats[2]])
        self.assertEqual(history.search_chats(chats, state='open', live={OTHER}), [chats[1]])
        self.assertEqual(history.search_chats(chats, state='closed', live={OTHER}), [chats[0], chats[2]])
        self.assertEqual(history.search_chats(chats, dir='~/app', display_dir=lambda cwd: server.home_path(cwd)), [])
        with patch.object(server.Path, 'home', return_value=Path('/Users/test')):
            self.assertEqual(history.search_chats(chats, dir='~/app', display_dir=server.home_path), [chats[0]])

    def test_history_route_paging_counts_and_directory_filters(self):
        chats = [dict(id=CHAT, agent='claude', title='API one', text='secret', cwd='/Users/test/a', time='2026-04-01'),
                 dict(id=OTHER, agent='codex', title='API two', text='secret', cwd='/Users/test/a', time='2026-03-01'),
                 dict(id='third', agent='codex', title='API three', text='secret', cwd='/Users/test/b', time='2026-02-01'),
                 dict(id='fourth', agent='claude', title='API four', text='secret', cwd=None, time='2026-01-01'),
                 dict(id='fifth', agent='claude', title='Unrelated', text='', cwd='/Users/test/z', time='2026-05-01')]
        with patch.object(history, 'all_chats', return_value=chats), \
                patch.object(server.Path, 'home', return_value=Path('/Users/test')):
            def get(**query):
                status, body = self.request('/mobile/api/history?' + urlencode(query), method='GET')
                self.assertEqual(status, 200)
                return body
            body = get(q='api', limit=2)
            self.assertEqual((body['total'], body['next']), (4, 2))
            self.assertEqual(body['counts'], {'all': 4, 'claude': 2, 'codex': 2})
            self.assertEqual(body['dirs'], [{'dir': '~/a', 'count': 2}, {'dir': '~/b', 'count': 1}])
            self.assertEqual(body['chats'][0], dict(id=CHAT, agent='claude', title='API one', dir='~/a',
                                                  time='2026-04-01', live=True, pane='%12'))
            self.assertIsNone(body['chats'][1]['pane'])
            self.assertFalse(body['chats'][1]['live'])
            self.assertTrue(all(set(chat) == {'id', 'agent', 'title', 'dir', 'time', 'live', 'pane'} for chat in body['chats']))
            page = get(q='api', limit=2, offset=body['next'])
            self.assertEqual((page['total'], page['next']), (4, None))
            self.assertEqual([chat['id'] for chat in page['chats']], ['third', 'fourth'])
            self.assertEqual(get(offset=99)['chats'], [])
            body = get(q='api', agent='codex', dir='~/b')
            self.assertEqual([chat['id'] for chat in body['chats']], ['third'])
            self.assertEqual(body['counts'], {'all': 1, 'claude': 0, 'codex': 1})
            self.assertEqual(body['dirs'], [{'dir': '~/a', 'count': 1}, {'dir': '~/b', 'count': 1}])
            body = get(q='api', agent='codex', dir='~/a', state='closed')
            self.assertEqual(body['counts'], {'all': 1, 'claude': 0, 'codex': 1})
            self.assertEqual(body['dirs'], [{'dir': '~/a', 'count': 1}, {'dir': '~/b', 'count': 1}])
            body = get(q='api', agent='codex', state='open')
            self.assertEqual(body['chats'], [])
            self.assertEqual(body['counts'], {'all': 1, 'claude': 1, 'codex': 0})
            self.assertEqual(body['dirs'], [])
            self.assertEqual(get(q='secret')['total'], 4)
        self.run.assert_not_called()

    def test_history_route_bad_parameters_query_cap_and_directory_cap(self):
        for query in ('limit=', 'limit=0', 'limit=101', 'limit=x', 'offset=-1', 'offset=1.2',
                      'offset=', 'offset=x', 'agent=', 'agent=exec', 'state=', 'state=live'):
            with self.subTest(query=query):
                status, body = self.request('/mobile/api/history?' + query, method='GET')
                self.assertEqual(status, 400)
                self.assertIn('error', body)
        chats = [dict(id=str(number), agent='claude', title='x' * 200, text='', cwd=f'/app/{number:02}', time='')
                 for number in range(35)]
        with patch.object(history, 'all_chats', return_value=chats):
            _, body = self.request('/mobile/api/history?' + urlencode({'q': 'x' * 200 + 'missing'}), method='GET')
        self.assertEqual(body['total'], 35)
        self.assertEqual(len(body['chats']), 30)
        self.assertEqual(len(body['dirs']), 30)
        self.assertEqual(body['dirs'][0], {'dir': '/app/00', 'count': 1})
        self.run.assert_not_called()

    def test_resume_codex_with_and_without_profile(self):
        self.entries.return_value = []
        self.codex_database([{}])
        for profile in (None, 'foundry'):
            with self.subTest(profile=profile), patch.object(server.codex_provider, 'launch_profile', return_value=profile) as launch:
                self.run.reset_mock()
                self.run.side_effect = [subprocess.CompletedProcess([], 0, stdout='%13\n', stderr=''),
                                        subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
                                        subprocess.CompletedProcess([], 0, stdout='$7\n')]
                self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'agent': 'codex', 'name': 'past-chat'}),
                                 (201, {'session': '$7', 'pane': '%13', 'name': 'past-chat'}))
                command = 'codex' + (' -p foundry' if profile else '') + ' resume ' + CHAT
                self.assertEqual(self.run.call_args_list[1].args[0], [paths.TMUX, 'send-keys', '-t', '%13', '-l', command])
                launch.assert_called_once_with()

    def test_resume_after_opens_next_window_names_and_launches_under_lock(self):
        self.entries.return_value = [{'id': '%12', 'thread': OTHER}]
        title = 'A chat title ' * 8
        self.codex_database([{'name': title}])
        commands = []
        outputs = ['@7\t/old\n', 'manual\t120\t40\n', '%13\n', '', '', '', '', '$7\n']
        def run(command, **kwargs):
            self.assertTrue(server.SEND_LOCK.locked())
            commands.append(command)
            return subprocess.CompletedProcess([], 0, stdout=outputs[len(commands) - 1], stderr='')
        self.run.side_effect = run
        with patch.object(server.codex_provider, 'launch_profile', return_value='foundry'):
            self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'agent': 'codex', 'after': '%12'}),
                             (201, {'session': '$7', 'pane': '%13', 'name': title[:60]}))
        self.assertEqual(commands[:4], [
            [paths.TMUX, 'display-message', '-p', '-t', '%12', '#{window_id}\t#{pane_current_path}'],
            [paths.TMUX, 'display-message', '-p', '-t', '@7', '#{window-size}\t#{window_width}\t#{window_height}'],
            [paths.TMUX, 'new-window', '-a', '-t', '@7', '-c', str(self.root), '-P', '-F', '#{pane_id}'],
            [paths.TMUX, 'resize-window', '-t', '%13', '-x', '120', '-y', '40']])
        self.assertEqual(commands[4][:13], [paths.TMUX, 'set-option', '-p', '-t', '%13', '@chat_name', title[:60], ';',
                                            'set-option', '-p', '-t', '%13', '@chat_name_at'])
        self.assertTrue(commands[4][13].isdigit())
        self.assertEqual(commands[5:], [
            [paths.TMUX, 'send-keys', '-t', '%13', '-l', 'codex -p foundry resume ' + CHAT],
            [paths.TMUX, 'send-keys', '-t', '%13', 'Enter'],
            [paths.TMUX, 'display-message', '-p', '-t', '%13', '#{session_id}']])

    def test_resume_targets_agent_unknown_pane_and_live_conflicts(self):
        self.transcript([{'type': 'user', 'message': {'content': 'Title'}}])
        for body in ({'id': CHAT}, {'id': CHAT, 'name': 'new', 'after': '%12'},
                     {'id': CHAT, 'agent': 'exec', 'name': 'new'}, {'id': CHAT, 'agent': None, 'name': 'new'}):
            self.assertEqual(self.request('/mobile/api/resume', body)[0], 400)
        for agent in ('claude', 'codex'):
            if agent == 'codex':
                self.codex_database([{}])
            for target in ({'name': 'new'}, {'after': '%12'}):
                self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'agent': agent, **target}),
                                 (409, {'error': 'That chat is already open.', 'pane': '%12'}))
        self.entries.return_value = [{'id': '%12', 'thread': OTHER}]
        for pane in ('%99', '$1', None, 12, ['%12']):
            status, body = self.request('/mobile/api/resume', {'id': CHAT, 'after': pane})
            self.assertEqual(status, 400)
            self.assertIn('live session list', body['error'])
        self.run.assert_not_called()

    def test_resume_rejects_bad_command_values_before_creating_a_target(self):
        for identifier, agent, profile in ((CHAT + ';echo', 'claude', None), (None, 'codex', None),
                                            (CHAT, 'exec', None), (CHAT, 'codex', 'bad;profile'),
                                            (CHAT, 'codex', ''), (CHAT, 'codex', 123)):
            with self.assertRaises(ValueError):
                terminal_input.resume_session('new', str(self.root), identifier, self.run, agent=agent, profile=profile)
        self.codex_database([{}])
        self.entries.return_value = [{'id': '%12', 'thread': OTHER}]
        with patch.object(server.codex_provider, 'launch_profile', return_value='bad;profile'):
            for target in ({'name': 'new'}, {'after': '%12'}):
                self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'agent': 'codex', **target})[0], 400)
        self.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
