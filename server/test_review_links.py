"""Watcher transitions use isolated stores and mocked sends, never live panes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import review_links
import server
from terminal_input import DraftError

THREAD = '0f8fad5b-d9cb-469f-a165-70867728950e'


class ReviewLinksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.file = Path(temporary.name) / 'cache' / 'review-links.json'
        self.enterContext(patch.object(review_links, 'FILE', self.file))
        self.worker = dict(id='%13', agent='codex', thread='worker-thread', transcript='/project/worker.jsonl')
        self.reviewer = dict(id='%12', agent='claude', thread=THREAD, writable=True)
        self.states = {'%13': 'Idle', '%12': 'Idle'}
        self.status = Mock(side_effect=lambda entry: {'state': self.states[entry['id']]})
        self.send = Mock()
        self.reply = Mock(return_value='I fixed it.')
        self.add()

    def add(self, **extra):
        review_links.add(**dict(worker='%13', reviewer='%12', reviewer_thread=THREAD, name='Fix the bug', agent='codex', **extra))

    def links(self):
        return json.loads(self.file.read_text())

    def observe(self, **states):
        self.states.update(states)
        review_links.observe([self.worker, self.reviewer], self.status, self.send, self.reply)

    def test_store_fields_atomic_replace_and_bound(self):
        link, = self.links()
        self.assertEqual(set(link), {'worker', 'reviewer', 'reviewer_thread', 'name', 'agent',
                                     'created', 'seen_working', 'worker_thread'})
        self.assertFalse(link['seen_working'])
        self.assertIsNone(link['worker_thread'])
        self.assertIsInstance(link['created'], float)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        with patch.object(review_links.os, 'replace', wraps=review_links.os.replace) as replace:
            for index in range(51):
                review_links.add(worker=f'%{index + 20}', reviewer='%12', reviewer_thread=THREAD, name='task', agent='claude')
        self.assertEqual(replace.call_count, 51)
        self.assertEqual(len(self.links()), 50)
        self.assertEqual(self.links()[0]['worker'], '%21')
        self.assertEqual(list(self.file.parent.iterdir()), [self.file])

    def test_working_to_idle_sends_once_per_turn_with_exact_text(self):
        self.observe(**{'%13': 'Working'})
        self.send.assert_not_called()
        self.assertTrue(self.links()[0]['seen_working'])
        self.assertEqual(self.links()[0]['worker_thread'], 'worker-thread')
        self.observe(**{'%13': 'Idle'})
        self.send.assert_called_once_with(self.reviewer,
            '[Tether review] The Codex tab “Fix the bug” (%13) finished a turn. '
            'Review its work: check the changes and the claims, then tell me what is wrong or missing. '
            'Its last reply:\n\nI fixed it.\n\nTranscript: /project/worker.jsonl', 'Idle')
        self.assertFalse(self.links()[0]['seen_working'])
        self.observe()
        self.observe()
        self.assertEqual(self.send.call_count, 1)
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})
        self.assertEqual(self.send.call_count, 2)

    def test_idle_to_idle_does_not_send_or_rewrite(self):
        self.observe()  # record the first worker thread
        with patch.object(review_links, '_write') as write:
            self.observe()
            self.observe()
        write.assert_not_called()
        self.send.assert_not_called()

    def test_worker_approval_and_input_do_not_finish_turn(self):
        self.observe(**{'%13': 'Working'})
        for state in ('Approval', 'Input needed', 'Unavailable'):
            self.observe(**{'%13': state})
            self.send.assert_not_called()
            self.assertTrue(self.links()[0]['seen_working'])
        self.observe(**{'%13': 'Idle'})
        self.send.assert_called_once()

    def test_reviewer_approval_input_unavailable_or_unwritable_retries(self):
        self.observe(**{'%13': 'Working'})
        for state in ('Approval', 'Input needed', 'Unavailable'):
            self.observe(**{'%13': 'Idle', '%12': state})
            self.send.assert_not_called()
            self.assertTrue(self.links()[0]['seen_working'])
        self.reviewer['writable'] = False
        self.observe(**{'%12': 'Idle'})
        self.send.assert_not_called()
        self.reviewer['writable'] = True
        self.observe()
        self.send.assert_called_once()

    def test_working_reviewer_can_receive_review(self):
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle', '%12': 'Working'})
        self.assertEqual(self.send.call_args.args[2], 'Working')

    def test_draft_and_runtime_errors_retry_and_success_resets_retries(self):
        self.observe(**{'%13': 'Working'})
        self.send.side_effect = [DraftError('draft'), RuntimeError('unavailable'), None]
        for count in (1, 2):
            self.observe(**{'%13': 'Idle'})
            self.assertTrue(self.links()[0]['seen_working'])
            self.assertEqual(self.links()[0]['retries'], count)
        self.observe()
        self.assertFalse(self.links()[0]['seen_working'])
        self.assertNotIn('retries', self.links()[0])
        self.observe()
        self.assertEqual(self.send.call_count, 3)

    def test_retries_stop_after_30_loops_and_next_turn_can_review(self):
        for blocked in ('draft', 'approval', 'unwritable'):
            with self.subTest(blocked=blocked):
                self.add()
                self.send.reset_mock(side_effect=True)
                self.reviewer['writable'] = blocked != 'unwritable'
                self.states['%12'] = 'Approval' if blocked == 'approval' else 'Idle'
                if blocked == 'draft':
                    self.send.side_effect = DraftError('draft')
                self.observe(**{'%13': 'Working'})
                for _ in range(29):
                    self.observe(**{'%13': 'Idle'})
                self.assertTrue(self.links()[0]['seen_working'])
                self.observe()
                self.assertFalse(self.links()[0]['seen_working'])
                self.observe()
                self.assertEqual(self.send.call_count, 30 if blocked == 'draft' else 0)
                self.send.side_effect = None
                self.reviewer['writable'] = True
                self.observe(**{'%13': 'Working', '%12': 'Idle'})
                self.observe(**{'%13': 'Idle'})
                self.assertFalse(self.links()[0]['seen_working'])
                self.assertEqual(self.send.call_count, 31 if blocked == 'draft' else 1)

    def test_missing_worker_or_reviewer_or_changed_reviewer_thread_drops_link(self):
        for entries in ([self.reviewer], [self.worker], [self.worker, dict(self.reviewer, thread='new-thread')]):
            self.add()
            review_links.observe(entries, self.status, self.send, self.reply)
            self.assertEqual(self.links(), [])
        self.send.assert_not_called()

    def test_worker_thread_is_pinned_once_known_and_changes_drop_link(self):
        self.worker['thread'] = None
        self.observe(**{'%13': 'Working'})
        self.assertIsNone(self.links()[0]['worker_thread'])
        self.worker['thread'] = 'first-thread'
        self.observe()
        self.assertEqual(self.links()[0]['worker_thread'], 'first-thread')
        for thread in ('new-thread', None):
            self.worker['thread'] = thread
            self.observe(**{'%13': 'Idle'})
            self.assertEqual(self.links(), [])
            self.add()
            self.worker['thread'] = 'first-thread'
            self.observe(**{'%13': 'Working'})
        self.send.assert_not_called()

    def test_reply_is_cut_to_1500_and_claude_label_is_used(self):
        review_links.add(worker='%13', reviewer='%12', reviewer_thread=THREAD, name='task', agent='claude')
        self.reply.return_value = 'x' * 4001
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})
        text = self.send.call_args.args[1]
        self.assertIn('The Claude tab “task”', text)
        self.assertIn('\n\n' + 'x' * 1500 + '\n\nTranscript:', text)
        self.assertNotIn('x' * 4001, text)

    def test_bad_link_or_callback_does_not_stop_other_links(self):
        self.file.write_text(json.dumps([{}, *self.links()]))
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})
        self.send.assert_called_once()
        with patch.object(review_links, '_read', side_effect=OSError('disk')):
            self.observe()
        with patch.object(review_links, '_write', side_effect=OSError('disk')):
            self.observe(**{'%13': 'Working'})
        self.status.side_effect = RuntimeError('bad pane')
        self.observe()

    def test_callback_can_add_link_without_deadlock_or_lost_update(self):
        self.observe(**{'%13': 'Working'})
        def send(*args):
            self.assertFalse(review_links._lock.locked())
            review_links.add(worker='%20', reviewer='%12', reviewer_thread=THREAD, name='other', agent='claude')
        self.send.side_effect = send
        self.observe(**{'%13': 'Idle'})
        self.assertEqual([link['worker'] for link in self.links()], ['%13', '%20'])
        self.assertFalse(self.links()[0]['seen_working'])

    def test_store_survives_reloading_and_no_change_does_not_write(self):
        self.observe(**{'%13': 'Working'})
        saved = self.file.read_text()
        with patch.object(review_links, '_write') as write:
            self.observe()
        write.assert_not_called()
        self.assertEqual(self.file.read_text(), saved)
        self.observe(**{'%13': 'Idle'})
        self.send.assert_called_once()


class ServerWatcherTests(unittest.TestCase):
    def test_last_reply_uses_latest_nonempty_assistant_text(self):
        messages = [{'role': 'assistant', 'text': 'old'}, {'role': 'assistant', 'text': 'latest'},
                    {'role': 'user', 'text': 'question'}, {'role': 'assistant', 'text': '  '},
                    {'role': 'assistant', 'text': None}]
        with patch.object(server, 'snapshot', return_value={'messages': messages}):
            self.assertEqual(server.last_reply({}), 'latest')
        for snapshot in ({'messages': []}, {}):
            with patch.object(server, 'snapshot', return_value=snapshot):
                self.assertEqual(server.last_reply({}), '')
        with patch.object(server, 'snapshot', side_effect=RuntimeError('no history')):
            self.assertEqual(server.last_reply({}), '')

    def test_monitor_runs_watcher_on_each_loop(self):
        entries = [{'id': '%12'}]
        with patch.object(server, 'sessions', return_value=entries), \
                patch.object(server.review_links, 'observe') as observe, \
                patch.object(server.time, 'sleep', side_effect=[None, StopIteration]):
            with self.assertRaises(StopIteration):
                server.monitor_notices()
        self.assertEqual(observe.call_count, 2)
        observe.assert_called_with(entries, server.shared_status, server.send_text, server.last_reply)


if __name__ == '__main__':
    unittest.main()
