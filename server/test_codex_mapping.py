import sqlite3
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import agents
import procinfo
import terminal_replies


class CodexMappingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE threads (id,rollout_path,cwd,archived,source,created_at_ms,updated_at_ms)')
        self.db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', [
            ('older', '/r/rollout-older.jsonl', '/work', 0, 'cli', 100000, 500000),
            ('newer', '/r/rollout-newer.jsonl', '/work', 0, 'cli', 200000, 600000),
            ('child', '/r/rollout-child.jsonl', '/work', 0, 'subagent', 200000, 700000)])

    def test_each_process_owns_its_open_transcript_despite_same_folder(self):
        for pid, expected in ((100, 'older'), (200, 'newer')):
            with patch.object(procinfo, 'argv', return_value=[b'codex']), \
                    patch.object(agents, 'started_ms', return_value=90000), \
                    patch.object(procinfo, 'fd_targets', return_value=[('7', f'/r/rollout-{expected}.jsonl'), ('8', '/r/rollout-child.jsonl')]):
                self.assertEqual(agents.codex_thread(self.db, pid, '1', '/work')[0], expected)

    def test_discovery_keeps_both_conversations_visible(self):
        run = Mock(return_value=SimpleNamespace(returncode=0, stdout='%1\t$1\twork\t0.0\t100\t/work\n%2\t$1\twork\t1.0\t200\t/work\n'))
        with patch.object(agents.pane_view, 'zoom_repair_all'), \
                patch.object(procinfo, 'table', return_value={100: (1, 'codex'), 200: (1, 'codex')}), \
                patch.object(agents, 'process_start', side_effect=str), \
                patch.object(agents, 'started_ms', return_value=90000), \
                patch.object(procinfo, 'argv', return_value=[b'codex']), \
                patch.object(procinfo, 'fd_targets', side_effect=lambda pid: [('7', '/r/rollout-' + ('older' if pid == 100 else 'newer') + '.jsonl')]), \
                patch.object(agents, 'codex_database', return_value=self.db), \
                patch.object(agents.codex_provider, 'pane_profile', return_value=None), \
                patch.object(agents, 'codex_names', return_value={}):
            entries = agents.discover({}, run)
        self.assertEqual([entry['thread'] for entry in entries], ['older', 'newer'])
        self.assertFalse(any(entry.get('pending_conversation') for entry in entries))

    def test_real_open_rollout_wins_over_newer_thread(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout-owner.jsonl'
            path.write_text('')
            self.db.execute("INSERT INTO threads VALUES ('owner', ?, '/elsewhere', 0, 'cli', 1, 2)", (str(path),))
            with path.open('a'):
                self.assertEqual(agents.codex_thread(self.db, os.getpid(), '1', '/work'), ('owner', str(path)))

    def test_disappearing_fds_allow_existing_fallback(self):
        with patch.object(procinfo, 'fd_targets', side_effect=ProcessLookupError), \
                patch.object(procinfo, 'argv', return_value=[b'codex']), \
                patch.object(agents, 'started_ms', return_value=90000):
            self.assertEqual(agents.codex_thread(self.db, 100, '1', '/work')[0], 'newer')


class CodexTerminalNoiseTests(unittest.TestCase):
    def test_slash_reply_does_not_collect_user_prompt_or_spinner(self):
        before = 'Ready\n› /goal\n  GPT medium · Context 7% used'
        after = 'Ready\nGoal started\n› See I started goal in codex\n  but it is missing in Tether\n• Working (1s • esc to interrupt)\n› Ask Codex to do anything\n  GPT medium · Context 7% used'
        self.assertEqual(terminal_replies.unwrap(terminal_replies.added_lines(before, after)), 'Goal started')

    def test_command_starting_a_turn_does_not_duplicate_transcript_output(self):
        screen = '› /goal Finish cleanup\n• I will check the code.\n• Working (1s • esc to interrupt)'
        with patch.object(terminal_replies, 'add') as add:
            self.assertIsNone(terminal_replies.record({}, '', lambda _: screen, sleep=lambda _: None))
        add.assert_not_called()

    def test_old_polluted_notices_are_hidden_without_changing_saved_data(self):
        notices = [{'id': 'terminal:bad', 'role': 'assistant', 'time': '2026-11-06T00:00:00Z', 'text': 'partial input Working (1s • esc to interrupt)'},
                   {'id': 'terminal:ok', 'role': 'assistant', 'time': '2026-11-06T00:00:00Z', 'text': 'Goal started'}]
        with patch.object(terminal_replies, 'saved', return_value=notices):
            self.assertEqual([m['id'] for m in terminal_replies.merge({'agent': 'codex'}, [])], ['terminal:ok'])
        self.assertEqual(len(notices), 2)


class CodexSendTests(unittest.TestCase):
    def test_upload_paths_are_messages_and_only_commands_capture_terminal_replies(self):
        import server
        for message, expected in (('/uploads/shot.png See this bug', False), ('/fixtures/screenshots/shot.png See this', False), ('/goal Finish this task', True), ('/status', True), ('/model', False)):
            with self.subTest(message=message), patch.object(server, 'submit') as submit, \
                    patch.object(server, 'capture', return_value='') as capture, \
                    patch.object(server.threading, 'Thread') as thread:
                server.send_text({'agent': 'codex', 'id': '%fixture'}, message, state='Idle')
                self.assertEqual(capture.called, expected)
                self.assertEqual(thread.called, expected)
                submit.assert_called_once_with(message, {'agent': 'codex', 'id': '%fixture'})
