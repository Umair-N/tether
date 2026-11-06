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
WORKER_THREAD = '17b9c72a-a916-46a0-bc10-bc41a8c6249a'


class ReviewLinksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.file = Path(temporary.name) / 'cache' / 'review-links.json'
        self.enterContext(patch.object(review_links, 'FILE', self.file))
        self.enterContext(patch.object(review_links, '_last_sent', {}))
        self.clock = self.enterContext(patch.object(review_links.time, 'monotonic', return_value=1000))
        self.worker = dict(id='%13', agent='codex', thread=WORKER_THREAD, transcript='/project/worker.jsonl')
        self.reviewer = dict(id='%12', agent='claude', thread=THREAD, writable=True)
        self.states = {'%13': 'Idle', '%12': 'Idle'}
        self.status = Mock(side_effect=lambda entry: {'state': self.states[entry['id']]})
        self.send = Mock()
        self.reply = Mock(return_value='I fixed it.')
        self.add()

    def add(self, **extra):
        review_links.add(**dict({'worker': '%13', 'reviewer': '%12', 'reviewer_thread': THREAD, 'name': 'Fix the bug', 'agent': 'codex'}, **extra))

    def links(self):
        return json.loads(self.file.read_text())

    def observe(self, *, entries=None, elapsed=6, **states):
        self.clock.return_value += elapsed
        self.states.update(states)
        review_links.observe(entries if entries is not None else [self.worker, self.reviewer],
                             self.status, self.send, self.reply)

    def test_feedback_waits_for_workers_review_reply_before_reaching_worker(self):
        for review_reply in ('APPROVED', 'Needs a worker test.'):
            with self.subTest(review_reply=review_reply):
                self.add()
                self.send.reset_mock()
                worker = dict(self.worker, id='%20')
                self.worker['writable'] = True
                entries = [self.worker, self.reviewer, worker]
                self.states.update({'%13': 'Idle', '%12': 'Idle', '%20': 'Idle'})
                replies = {'%13': 'Work done.', '%12': 'Old review.', '%20': 'Other work done.'}
                self.reply.side_effect = lambda entry: replies[entry['id']]
                self.request_review()
                review_links.add(worker='%20', reviewer='%13', reviewer_thread=WORKER_THREAD,
                                 name='Other task', agent='codex', seen_working=True)
                self.observe(entries=entries)
                self.assertEqual([call.args[0]['id'] for call in self.send.call_args_list], ['%12', '%13'])
                replies['%12'] = 'Fix the original task.'
                self.observe(entries=entries, **{'%13': 'Working'})
                self.assertEqual(self.send.call_count, 2)
                self.assertTrue(self.links()[0]['awaiting'])
                self.assertEqual(self.links()[0]['wait_ticks'], 1)

                replies['%13'] = review_reply
                self.observe(entries=entries, **{'%13': 'Idle'})
                self.assertTrue(self.links()[0]['awaiting'])
                self.assertFalse(self.links()[1]['awaiting'])
                if review_reply == 'APPROVED':
                    self.assertEqual(self.send.call_count, 2)
                else:
                    self.assertEqual(self.send.call_count, 3)
                    self.assertEqual(self.send.call_args.args[0], worker)
                    self.assertIn(review_reply, self.send.call_args.args[1])

                def send(entry, text, state):
                    if entry['id'] == '%13':
                        replies['%13'] = 'READY'
                self.send.side_effect = send
                self.observe(entries=entries)
                self.assertEqual(self.send.call_args.args[0], self.worker)
                self.assertIn('Fix the original task.', self.send.call_args.args[1])
                self.assertFalse(self.links()[0]['awaiting'])
                self.assertFalse(self.links()[1]['awaiting'])
                self.send.side_effect = None
                review_links.remove('%20')

    def test_shared_reviewer_gets_one_request_until_first_reply_is_read(self):
        self.add(seen_working=True)
        worker = dict(self.worker, id='%20')
        self.states['%20'] = 'Idle'
        entries = [self.worker, self.reviewer, worker]
        self.add(worker='%20', seen_working=True)
        self.observe(entries=entries)
        self.send.assert_called_once()
        first, second = self.links()
        self.assertTrue(first['awaiting'])
        self.assertTrue(second['seen_working'])
        self.assertEqual(second['wait_ticks'], 1)
        self.assertNotIn('retries', second)
        self.observe(entries=entries)
        self.assertEqual(self.send.call_count, 1)
        self.reply.return_value = 'APPROVED'
        self.observe(entries=entries)
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertTrue(self.links()[1]['seen_working'])
        self.assertEqual(self.send.call_count, 1)
        self.observe(entries=entries, elapsed=0)
        self.assertEqual(self.send.call_count, 2)
        self.assertIn('(%20) finished a turn.', self.send.call_args.args[1])

    def test_review_turn_does_not_request_review_of_reviewers_work(self):
        worker = dict(self.worker, id='%20')
        self.worker['writable'] = True
        self.states['%20'] = 'Idle'
        entries = [self.worker, self.reviewer, worker]
        review_links.add(worker='%20', reviewer='%13', reviewer_thread=WORKER_THREAD,
                         name='Other task', agent='codex', seen_working=True)
        self.observe(entries=entries)
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.observe(entries=entries, **{'%13': 'Working'})
        self.assertFalse(self.links()[0]['seen_working'])
        self.assertTrue(self.links()[1]['reviewer_seen_working'])
        self.reply.return_value = 'APPROVED'
        self.observe(entries=entries, **{'%13': 'Idle'})
        self.observe(entries=entries)
        self.assertEqual(self.send.call_count, 1)
        self.assertFalse(self.links()[0]['seen_working'])

    def test_send_gap_blocks_request_and_feedback_until_six_seconds(self):
        self.assertEqual(review_links.SEND_GAP, 6)
        for feedback in (False, True):
            with self.subTest(feedback=feedback):
                self.add(seen_working=True)
                self.send.reset_mock()
                link, = self.links()
                if feedback:
                    link.update(awaiting=True, reviewer_seen_working=True)
                    self.file.write_text(json.dumps([link]))
                pane = '%13' if feedback else '%12'
                review_links._last_sent[pane] = self.clock.return_value
                self.observe(elapsed=5)
                self.send.assert_not_called()
                self.assertEqual(self.links()[0]['wait_ticks'], 1)
                self.assertNotIn('retries', self.links()[0])
                self.observe(elapsed=1)
                self.send.assert_called_once()
                self.assertEqual(self.send.call_args.args[0]['id'], pane)
                self.assertEqual(review_links._last_sent[pane], self.clock.return_value)
                self.assertNotIn('wait_ticks', self.links()[0])

    def test_guard_wait_gives_up_after_review_wait(self):
        for feedback in (False, True):
            with self.subTest(feedback=feedback):
                self.add(seen_working=True)
                link, = self.links()
                if feedback:
                    link.update(awaiting=True, reviewer_seen_working=True)
                    self.file.write_text(json.dumps([link]))
                pane = '%13' if feedback else '%12'
                review_links._last_sent[pane] = self.clock.return_value
                for _ in range(review_links.REVIEW_WAIT - 1):
                    self.observe(elapsed=0)
                self.assertTrue(self.links()[0]['awaiting' if feedback else 'seen_working'])
                self.assertEqual(self.links()[0]['wait_ticks'], review_links.REVIEW_WAIT - 1)
                self.observe(elapsed=0)
                self.assertFalse(self.links()[0]['awaiting' if feedback else 'seen_working'])
                self.assertNotIn('wait_ticks', self.links()[0])
                self.assertNotIn('retries', self.links()[0])
                self.send.assert_not_called()

    def test_reviewed_by_reads_once_under_lock_and_skips_malformed_rows(self):
        links = [*self.links(), {'worker': '%20', 'reviewer': '%21'}, None, [], {},
                 {'worker': [], 'reviewer': '%12'}, {'worker': '%30'},
                 {'worker': '%31', 'reviewer': 12}, {'worker': 'bad', 'reviewer': '%12'},
                 {'worker': '%32', 'reviewer': '%12\n'}]
        def read():
            self.assertTrue(review_links._lock.locked())
            return links
        with patch.object(review_links, '_read', side_effect=read) as read_store:
            self.assertEqual(review_links.reviewed_by(), {'%13': ['%12'], '%20': ['%21']})
        read_store.assert_called_once_with()

    def test_remove_drops_every_matching_link_and_keeps_other_rows_under_lock(self):
        link, = self.links()
        other = dict(link, worker='%20')
        self.file.write_text(json.dumps([link, other, dict(link, reviewer='%21'), None, {}]))
        write_store = review_links._write
        def write(links):
            self.assertTrue(review_links._lock.locked())
            write_store(links)
        with patch.object(review_links, '_write', side_effect=write) as write_mock:
            self.assertTrue(review_links.remove('%13'))
        write_mock.assert_called_once_with([other, None, {}])
        self.assertEqual(self.links(), [other, None, {}])

    def test_remove_missing_worker_returns_false_without_writing(self):
        with patch.object(review_links, '_write') as write:
            self.assertFalse(review_links.remove('%99'))
        write.assert_not_called()
        self.assertEqual(review_links.reviewed_by(), {'%13': ['%12']})

    def test_pairs_are_unique_and_reviewers_keep_store_order_without_duplicates(self):
        self.add(reviewer='%21')
        self.add(name='Replacement')
        self.assertEqual([(link['reviewer'], link['name']) for link in self.links()],
                         [('%21', 'Fix the bug'), ('%12', 'Replacement')])
        self.file.write_text(json.dumps(self.links() + self.links()))
        self.assertEqual(review_links.reviewed_by(), {'%13': ['%21', '%12']})
        self.assertTrue(review_links.remove('%13', '%21'))
        self.assertEqual(review_links.reviewed_by(), {'%13': ['%12']})
        self.assertFalse(review_links.remove('%13', '%21'))
        self.assertTrue(review_links.remove('%13'))
        self.assertEqual(self.links(), [])

    def test_unpinned_reviewer_waits_for_uuid_then_sends_when_both_idle(self):
        self.add(reviewer_thread=None, seen_working=True)
        for thread in (None, 'pending-123-1', 'pending-123-2', 'not-a-uuid', THREAD.upper()):
            self.reviewer['thread'] = thread
            self.observe()
            self.assertIsNone(self.links()[0]['reviewer_thread'])
            self.send.assert_not_called()
        self.reviewer['thread'] = THREAD
        self.observe(**{'%12': 'Working'})
        self.assertEqual(self.links()[0]['reviewer_thread'], THREAD)
        self.send.assert_not_called()
        self.observe(**{'%12': 'Idle'})
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        self.observe()
        self.assertEqual(self.send.call_count, 1)

    def test_unpinned_reviewer_expires_only_after_start_wait(self):
        self.assertEqual(review_links.START_WAIT, 300)
        with patch.object(review_links.time, 'time', return_value=1000):
            self.add(reviewer_thread=None, seen_working=True)
        self.reviewer['thread'] = 'pending-1-1'
        for now in (1031, 1299, 1300):
            with patch.object(review_links.time, 'time', return_value=now):
                self.observe()
            self.assertEqual(len(self.links()), 1)
        with patch.object(review_links.time, 'time', return_value=1301):
            self.observe()
        self.assertEqual(self.links(), [])
        self.send.assert_not_called()

    def test_several_reviewers_receive_each_workers_turn(self):
        self.add(reviewer='%21')
        reviewer = dict(self.reviewer, id='%21')
        self.states['%21'] = 'Idle'
        for state in ('Working', 'Idle'):
            self.states['%13'] = state
            review_links.observe([self.worker, self.reviewer, reviewer], self.status, self.send, self.reply)
        self.assertEqual([call.args[0]['id'] for call in self.send.call_args_list], ['%12', '%21'])

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
        self.assertEqual(self.links()[0]['worker_thread'], WORKER_THREAD)
        self.observe(**{'%13': 'Idle'})
        self.send.assert_called_once_with(self.reviewer,
            '[Tether review] The Codex tab “Fix the bug” (%13) finished a turn. '
            'Review its work: check the changes and the claims, then list what is wrong or missing. '
            'Start your reply with APPROVED if nothing is. Tether sends your reply to that tab. '
            'Its last reply:\n\nI fixed it.\n\nTranscript: /project/worker.jsonl', 'Idle')
        self.assertFalse(self.links()[0]['seen_working'])
        self.assertTrue(self.links()[0]['awaiting'])
        self.assertFalse(self.links()[0]['reviewer_seen_working'])
        self.observe()
        self.observe()
        self.assertEqual(self.send.call_count, 1)
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})
        self.assertEqual(self.send.call_count, 1)  # no new request while awaiting
        self.reply.return_value = 'APPROVED'
        self.observe(**{'%12': 'Working'})
        self.observe(**{'%12': 'Idle'})
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

    def test_working_reviewer_waits_until_idle(self):
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle', '%12': 'Working'})
        self.send.assert_not_called()
        self.assertTrue(self.links()[0]['seen_working'])
        self.observe(**{'%12': 'Idle'})
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[2], 'Idle')

    def test_reviewer_wait_expires_after_review_wait_and_next_turn_can_review(self):
        for state in ('Working', 'Approval', 'Input needed', 'Unavailable'):
            with self.subTest(state=state):
                self.add()
                self.send.reset_mock()
                self.observe(**{'%13': 'Working', '%12': state})
                for _ in range(review_links.REVIEW_WAIT - 1):
                    self.observe(**{'%13': 'Idle'})
                self.assertTrue(self.links()[0]['seen_working'])
                self.observe()
                self.assertFalse(self.links()[0]['seen_working'])
                self.observe(**{'%12': 'Idle'})
                self.send.assert_not_called()
                self.observe(**{'%13': 'Working'})
                self.observe(**{'%13': 'Idle'})
                self.send.assert_called_once()

    def test_draft_and_runtime_errors_retry_and_success_resets_retries(self):
        self.observe(**{'%13': 'Working'})
        self.send.side_effect = [DraftError('draft'), RuntimeError('unavailable'), None]
        for count in (1, 2):
            self.observe(**{'%13': 'Idle'})
            self.assertTrue(self.links()[0]['seen_working'])
            self.assertEqual(self.links()[0]['retries'], count)
            self.assertNotIn('%12', review_links._last_sent)
        self.observe()
        self.assertEqual(review_links._last_sent['%12'], self.clock.return_value)
        self.assertFalse(self.links()[0]['seen_working'])
        self.assertNotIn('retries', self.links()[0])
        self.observe()
        self.assertEqual(self.send.call_count, 3)

    def test_retries_stop_after_30_loops_and_next_turn_can_review(self):
        for blocked in ('draft', 'runtime', 'unwritable'):
            with self.subTest(blocked=blocked):
                self.add()
                self.send.reset_mock(side_effect=True)
                self.reviewer['writable'] = blocked != 'unwritable'
                self.states['%12'] = 'Idle'
                if blocked != 'unwritable':
                    self.send.side_effect = DraftError('draft') if blocked == 'draft' else RuntimeError('unavailable')
                self.observe(**{'%13': 'Working'})
                for _ in range(29):
                    self.observe(**{'%13': 'Idle'})
                self.assertTrue(self.links()[0]['seen_working'])
                self.observe()
                self.assertFalse(self.links()[0]['seen_working'])
                self.observe()
                self.assertEqual(self.send.call_count, 0 if blocked == 'unwritable' else 30)
                self.send.side_effect = None
                self.reviewer['writable'] = True
                self.observe(**{'%13': 'Working', '%12': 'Idle'})
                self.observe(**{'%13': 'Idle'})
                self.assertFalse(self.links()[0]['seen_working'])
                self.assertEqual(self.send.call_count, 1 if blocked == 'unwritable' else 31)

    def request_review(self):
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})

    def finish_review(self, reply='Needs a regression test.'):
        self.reply.return_value = reply
        self.observe(**{'%12': 'Working'})
        self.observe(**{'%12': 'Idle'})

    def test_reviewer_reply_returns_exact_feedback_once_and_next_turn_gets_reviewed(self):
        self.request_review()
        self.finish_review('  Needs a regression test.\n ')
        self.assertEqual(self.send.call_count, 2)
        self.send.assert_called_with(self.worker,
            '[Tether review] Feedback from the reviewer tab (%12):\n\n'
            'Needs a regression test.\n\nFix what it raises, then stop and wait.', 'Idle')
        self.reply.assert_called_with(self.reviewer)
        self.assertEqual(self.links()[0]['rounds'], 1)
        self.assertFalse(self.links()[0]['awaiting'])
        self.observe()
        self.observe()
        self.assertEqual(self.send.call_count, 2)
        self.request_review()
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(self.send.call_args.args[0], self.reviewer)

    def test_review_turn_shorter_than_a_tick_is_seen_by_a_changed_reply(self):
        self.request_review()
        self.reply.return_value = 'Missing a test.'  # the reviewer never shows Working between ticks
        self.observe()
        self.send.assert_called_with(self.worker, '[Tether review] Feedback from the reviewer tab (%12):\n\n'
                                     'Missing a test.\n\nFix what it raises, then stop and wait.', 'Idle')
        self.assertFalse(self.links()[0]['awaiting'])

    def test_unchanged_idle_reviewer_gives_up_after_30_ticks(self):
        self.request_review()
        calls = self.send.call_count
        for _ in range(29):
            self.observe()
        self.assertTrue(self.links()[0]['awaiting'])
        self.observe()
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertNotIn('reviewer_before', self.links()[0])
        self.assertEqual(self.send.call_count, calls)

    def test_feedback_is_stripped_then_cut_to_3000(self):
        self.request_review()
        self.finish_review(' \n' + 'x' * 4000 + '\n ')
        self.send.assert_called_with(self.worker,
            '[Tether review] Feedback from the reviewer tab (%12):\n\n'
            + 'x' * 3000 + '\n\nFix what it raises, then stop and wait.', 'Idle')

    def test_approved_reply_does_not_return_feedback(self):
        for reply in ('APPROVED', '  approved: all checks pass.\n', 'Approved with notes'):
            with self.subTest(reply=reply):
                self.add()
                self.send.reset_mock()
                self.request_review()
                self.finish_review(reply)
                self.assertEqual(self.send.call_count, 1)
                self.assertFalse(self.links()[0]['awaiting'])
                self.assertEqual(self.links()[0].get('rounds', 0), 0)

    def test_feedback_stops_after_max_rounds_and_last_request_warns_reviewer(self):
        self.assertEqual(review_links.MAX_ROUNDS, 3)
        for round_number in range(review_links.MAX_ROUNDS + 1):
            self.request_review()
            request = self.send.call_args.args[1]
            last_round = ' This is the last round: your reply stays here.'
            if round_number == review_links.MAX_ROUNDS:
                self.assertTrue(request.endswith(last_round))
            else:
                self.assertNotIn(last_round, request)
            self.finish_review()
            self.assertEqual(self.links()[0].get('rounds'), min(round_number + 1, review_links.MAX_ROUNDS))
            self.assertFalse(self.links()[0]['awaiting'])
        self.assertEqual(self.send.call_count, 2 * review_links.MAX_ROUNDS + 1)
        self.assertEqual(self.send.call_args.args[0], self.reviewer)

    def test_feedback_waits_for_worker_idle(self):
        for state in ('Working', 'Approval', 'Input needed', 'Unavailable'):
            with self.subTest(state=state):
                self.add()
                self.send.reset_mock()
                self.states['%12'] = 'Idle'
                self.request_review()
                self.observe(**{'%13': state})
                self.finish_review()
                for _ in range(review_links.MAX_RETRIES):
                    self.observe()
                self.assertEqual(self.send.call_count, 1)
                self.assertTrue(self.links()[0]['awaiting'])
                self.observe(**{'%13': 'Idle'})
                self.assertEqual(self.send.call_count, 2)
                self.assertEqual(self.send.call_args.args[0], self.worker)
                self.assertEqual(self.links()[0]['rounds'], 1)

    def test_feedback_wait_expires_after_review_wait(self):
        self.request_review()
        self.observe(**{'%13': 'Working'})
        self.finish_review()
        for _ in range(review_links.REVIEW_WAIT - 2):
            self.observe()
        self.assertTrue(self.links()[0]['awaiting'])
        self.observe()
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertEqual(self.links()[0].get('rounds', 0), 0)
        self.observe(**{'%13': 'Idle'})
        self.assertEqual(self.send.call_count, 1)

    def test_feedback_draft_and_runtime_errors_retry_without_counting_a_round(self):
        self.request_review()
        self.send.reset_mock()
        self.send.side_effect = [DraftError('draft'), RuntimeError('unavailable'), None]
        self.finish_review()
        self.assertTrue(self.links()[0]['awaiting'])
        self.assertEqual(self.links()[0].get('rounds', 0), 0)
        self.assertNotIn('%13', review_links._last_sent)
        self.observe()
        self.assertTrue(self.links()[0]['awaiting'])
        self.assertNotIn('%13', review_links._last_sent)
        self.observe()
        self.assertEqual(review_links._last_sent['%13'], self.clock.return_value)
        self.assertEqual(self.send.call_count, 3)
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertEqual(self.links()[0]['rounds'], 1)
        self.assertNotIn('retries', self.links()[0])

    def test_feedback_send_errors_stop_after_max_retries(self):
        self.request_review()
        self.send.reset_mock()
        self.send.side_effect = DraftError('draft')
        self.finish_review()
        for _ in range(review_links.MAX_RETRIES - 2):
            self.observe()
        self.assertTrue(self.links()[0]['awaiting'])
        self.observe()
        self.assertFalse(self.links()[0]['awaiting'])
        self.observe()
        self.assertEqual(self.send.call_count, review_links.MAX_RETRIES)
        self.assertEqual(self.links()[0].get('rounds', 0), 0)

    def test_old_link_without_review_loop_fields_completes_a_round(self):
        link, = self.links()
        self.assertNotIn('awaiting', link)
        self.assertNotIn('reviewer_seen_working', link)
        self.assertNotIn('rounds', link)
        self.request_review()
        self.finish_review()
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.links()[0]['rounds'], 1)

    def test_missing_worker_survives_grace_then_drops(self):
        with patch.object(review_links.time, 'time', return_value=1000):
            self.add()
        original = self.links()
        for now, expected in ((1001, original), (1030, []), (1031, [])):
            with self.subTest(now=now):
                self.file.write_text(json.dumps(original))
                with patch.object(review_links.time, 'time', return_value=now):
                    review_links.observe([self.reviewer], self.status, self.send, self.reply)
                self.assertEqual(self.links(), expected)
        self.status.assert_not_called()
        self.send.assert_not_called()

    def test_missing_reviewer_survives_within_grace(self):
        with patch.object(review_links.time, 'time', return_value=1000):
            self.add()
        original = self.links()
        with patch.object(review_links.time, 'time', return_value=1001):
            review_links.observe([self.worker], self.status, self.send, self.reply)
        self.assertEqual(self.links(), original)
        self.status.assert_not_called()
        self.send.assert_not_called()

    def test_changed_reviewer_thread_drops_link_even_with_missing_worker(self):
        for entries in ([self.worker, dict(self.reviewer, thread='new-thread')],
                        [dict(self.reviewer, thread='new-thread')]):
            with patch.object(review_links.time, 'time', return_value=1000):
                self.add()
                review_links.observe(entries, self.status, self.send, self.reply)
            self.assertEqual(self.links(), [])
        self.send.assert_not_called()

    def test_malformed_created_is_skipped_without_stopping_other_links(self):
        link, = self.links()
        bad = dict(link, worker='%99', created='invalid')
        self.file.write_text(json.dumps([bad, link]))
        self.observe(**{'%13': 'Working'})
        self.assertEqual(self.links()[0], bad)
        self.assertTrue(self.links()[1]['seen_working'])

    def test_worker_thread_is_pinned_once_known_and_changes_drop_link(self):
        for thread in (None, 'pending-123-1', 'not-a-uuid', WORKER_THREAD.upper()):
            self.worker['thread'] = thread
            self.observe(**{'%13': 'Working'})
            self.assertIsNone(self.links()[0]['worker_thread'])
        self.worker['thread'] = WORKER_THREAD
        self.observe()
        self.assertEqual(self.links()[0]['worker_thread'], WORKER_THREAD)
        for thread in ('pending-123-2', None, 'not-a-uuid'):
            self.worker['thread'] = thread
            self.observe()
            self.assertEqual(self.links()[0]['worker_thread'], WORKER_THREAD)
        self.worker['thread'] = THREAD
        self.observe(**{'%13': 'Idle'})
        self.assertEqual(self.links(), [])
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
