"""Task launch tests use temporary prompt files and mocked processes, never live tmux."""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import delegate
import paths
import server

THREAD = '0f8fad5b-d9cb-469f-a165-70867728950e'


def entry(**extra):
    return dict({'id': '%12', 'agent': 'claude', 'thread': THREAD, 'writable': True,
                 'transcript': '/project/chat.jsonl'}, **extra)


def body(**extra):
    return dict({'session': '%12', 'thread': THREAD, 'agent': 'claude', 'message': 'Fix the bug'}, **extra)


class ValidationTests(unittest.TestCase):
    def test_normalize_review_accepts_only_the_documented_types(self):
        for value, expected in ((False, None), (True, 'source'), ('source', 'source'), ('new', 'new')):
            with self.subTest(value=value):
                self.assertEqual(delegate.normalize_review(value), expected)
                delegate.validate(body(review=value), [entry()])
        for value in (0, 1, None, 'false', '', [], {}, 1.0):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Context and review'):
                delegate.normalize_review(value)

    def test_new_reviewer_requires_a_saved_writable_source_and_allows_empty_task(self):
        error = ('A reviewer tab needs a Claude or Codex chat with a saved conversation. '
                 'Send a first message, then try again.')
        for source in (entry(thread=None), entry(thread='pending-123-1'), entry(pending_conversation=True),
                       entry(transcript=None), entry(transcript=''), entry(writable=False),
                       entry(agent='shell'), entry(agent='antigravity')):
            with self.subTest(source=source), self.assertRaises(ValueError) as raised:
                delegate.validate(body(thread=source['thread'], review='new', context=True), [source])
            self.assertEqual(str(raised.exception), error)
        for agent in ('claude', 'codex'):
            delegate.validate(body(review='new', message=' \n\t '), [entry(agent=agent)])
        for message in (None, 'x' * 20001, '\r'):
            with self.subTest(message=message), self.assertRaises(ValueError):
                delegate.validate(body(review='new', message=message), [entry()])

    def test_optional_name_is_bounded_and_has_no_controls(self):
        for name in (None, 1, '', '  ', 'x' * 61, 'name\n', '\tname', 'name\x7f'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'tab name'):
                delegate.validate(body(name=name), [entry()])
        delegate.validate(body(name='  ' + 'x' * 60 + '  '), [entry()])

    def test_accepts_defaults_and_plain_tasks_from_shells(self):
        source = entry()
        self.assertIs(delegate.validate(body(), [source]), source)
        source = entry(agent='shell', thread=None, writable=False)
        self.assertIs(delegate.validate(body(thread=None), [source]), source)

    def test_rejects_bad_requests(self):
        for extra in ({'session': None}, {'session': '%1;pwd'}, {'session': '%99'},
                      {'thread': None}, {'agent': 'antigravity'}, {'agent': []},
                      {'message': None}, {'message': ' \n\t '}, {'message': 'x' * 20001},
                      {'context': 1}, {'context': None}, {'review': 'false'}, {'review': 0}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                delegate.validate(body(**extra), [entry()])

    def test_rejects_controls_even_at_stripped_edges(self):
        for control in ('\0', '\r', '\x1b', '\x7f', '\x85', '\x9b'):
            with self.subTest(control=repr(control)), self.assertRaisesRegex(ValueError, 'control characters'):
                delegate.validate(body(message=control + 'task'), [entry()])
        delegate.validate(body(message='  Hello\n\tworld  '), [entry()])
        delegate.validate(body(message=' ' + 'x' * 20000 + ' '), [entry()])

    def test_thread_error_is_specific(self):
        with self.assertRaisesRegex(ValueError, '^This tab changed conversations. Reload it and try again.$'):
            delegate.validate(body(thread='stale'), [entry()])

    def test_context_requires_a_saved_supported_conversation(self):
        for source in (entry(thread=None), entry(thread='unsafe;command'), entry(pending_conversation=True),
                       entry(agent='antigravity'), entry(agent='shell')):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'Context needs'):
                delegate.validate(body(thread=source['thread'], context=True), [source])
        for agent in ('claude', 'codex'):
            delegate.validate(body(context=True), [entry(agent=agent)])

    def test_review_requires_writable_supported_source(self):
        for source in (entry(writable=False), entry(agent='shell'), entry(agent='antigravity')):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'Review needs'):
                delegate.validate(body(review=True), [source])
        delegate.validate(body(thread=None, review=True), [entry(thread=None)])


class LaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='delegate test ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(paths, 'CACHE', self.root))

    def test_names_use_first_line_and_cut_at_a_word_boundary(self):
        for message, expected in (
                ('  Fix\t the   bug\nIgnore this', 'Fix the bug'),
                ('a' * 32, 'a' * 32), ('a' * 33, 'a' * 32 + '…'),
                ('One two three four five six seven eight', 'One two three four five six…'),
                ('a' * 30 + ' longer word', 'a' * 30 + '…')):
            with self.subTest(message=message):
                self.assertEqual(delegate.task_name(message), expected)

    def test_reviewer_prompt_has_exact_instructions_with_optional_recent_turns(self):
        for agent in ('claude', 'codex'):
            for recent in ('', 'User: test this'):
                source = entry(agent=agent, pane_name='Fix this', label='Fallback')
                expected = (f'You are the reviewer of the {agent.title()} tab “Fix this” (%12) in this project.'
                            '\n\nWhat to check:\nCheck this')
                if recent:
                    expected += '\n\nIts recent turns, cut short:\n\n' + recent
                expected += ('\n\nIts full transcript (JSONL) is at /project/chat.jsonl. Read it when you review.'
                             '\n\nDo not change its work and do not send to that tab yourself. Reply only "Ready." now and stop. '
                             'Tether sends you a review request after that tab finishes a turn, and it sends your reply back to that tab.')
                with self.subTest(agent=agent, recent=recent):
                    self.assertEqual(delegate.reviewer_prompt(source, ' Check this ', recent), expected)
        self.assertIn('tab “Fallback”', delegate.reviewer_prompt(entry(label='Fallback'), '', ''))
        self.assertIn('What to check:\nCheck the work in that tab.', delegate.reviewer_prompt(entry(), '', ''))
        self.assertIn('tab “that tab”', delegate.reviewer_prompt(entry(), '', ''))

    def test_new_reviewer_reverses_link_and_always_reads_context(self):
        source = entry(pane_name='Source title')
        recent = Mock(return_value=[{'role': 'user', 'text': 'Recent task'}])
        with patch.object(delegate, 'new_window_after', return_value='%13'), \
                patch.object(delegate.review_links, 'add') as add:
            result = delegate.start(source, body(review='new', message='', context=False), '/project',
                                    run=Mock(), recent=recent)
        self.assertEqual(result, ('%13', 'Review: Source title'))
        recent.assert_called_once_with()
        prompt, = (self.root / 'delegate').glob('*.md')
        self.assertEqual(prompt.read_text(), delegate.reviewer_prompt(source, '', 'User: Recent task'))
        add.assert_called_once_with(worker='%12', reviewer='%13', reviewer_thread=None,
                                    name='Source title', agent='claude', seen_working=True)

    def test_source_reviewer_with_pending_thread_stays_unpinned(self):
        for thread in (None, 'pending-123-1'):
            with patch.object(delegate, 'new_window_after', return_value='%13'), \
                    patch.object(delegate.review_links, 'add') as add:
                delegate.start(entry(thread=thread), body(review='source'), '/project', run=Mock())
            add.assert_called_once_with(worker='%13', reviewer='%12', reviewer_thread=None,
                                        name='Fix the bug', agent='claude')

    def test_name_and_cwd_overrides_and_reviewer_default_names(self):
        for request, source, expected in (
                (body(name='  Custom name  ', cwd='/ignored'), entry(), 'Custom name'),
                (body(review='new'), entry(label='Fallback'), 'Review: Fallback'),
                (body(review='new'), entry(), 'Review: that tab'),
                (body(review='new'), entry(pane_name='x' * 80), 'Review: ' + 'x' * 52),
                (body(review='new', name='Explicit'), entry(), 'Explicit')):
            with self.subTest(request=request), patch.object(delegate, 'new_window_after', return_value='%13') as window, \
                    patch.object(delegate.review_links, 'add'):
                self.assertEqual(delegate.start(source, request, '/project', run=Mock(), cwd=str(self.root)),
                                 ('%13', expected))
                self.assertEqual(window.call_args.kwargs['cwd'], str(self.root))
        for cwd in ('relative', '/does/not/exist', 123, str(self.root / 'file')):
            with self.subTest(cwd=cwd), patch.object(delegate, 'new_window_after') as window:
                with self.assertRaisesRegex(ValueError, 'absolute path'):
                    delegate.start(entry(), body(), '/project', run=Mock(), cwd=cwd)
                window.assert_not_called()

    def test_prompt_context_is_a_short_brief_for_every_pair(self):
        recent = 'User: hi\n\nAgent: hello'
        for source_agent, target in (('claude', 'claude'), ('claude', 'codex'), ('codex', 'claude'), ('codex', 'codex')):
            name = 'Claude' if source_agent == 'claude' else 'Codex'
            expected = (f'Context: this task comes from another {name} chat in this project. Its recent turns, cut short:\n\n'
                        'User: hi\n\nAgent: hello\n\nIts full transcript (JSONL) is at /project/chat.jsonl. '
                        'Read it only if the task needs more.\n\nTask:\nFix it')
            with self.subTest(source=source_agent, target=target):
                self.assertEqual(delegate.prompt_text(entry(agent=source_agent), 'Fix it', target, context=True, recent=recent), expected)
        self.assertEqual(delegate.prompt_text(entry(), 'Fix it', 'claude'), 'Fix it')
        self.assertEqual(delegate.prompt_text(entry(), ' Fix it ', 'codex', review=True),
                         'Fix it\n\nWhen you finish, stop and wait: a reviewer in another tab reads your work and may send follow-ups.')

    def test_brief_keeps_recent_text_turns_and_stays_small(self):
        messages = [{'role': 'user', 'text': f'question {n}'} for n in range(20)]
        messages += [{'role': 'tool', 'text': 'x' * 9000}, {'role': 'assistant', 'text': 'y' * 5000}, {'role': 'assistant', 'text': '  '}]
        text = delegate.brief(messages)
        lines = text.split('\n\n')
        self.assertEqual(len(lines), delegate.BRIEF_TURNS)
        self.assertEqual(lines[-1], 'Agent: ' + 'y' * delegate.BRIEF_EACH + '…')
        self.assertEqual(lines[0], 'User: question 15')
        self.assertNotIn('x' * 100, text)
        self.assertLessEqual(len(text), delegate.BRIEF_TOTAL + delegate.BRIEF_TURNS * 2)
        self.assertEqual(delegate.brief([{'role': 'user', 'text': 'z' * 600 + ' tail'}] * 10).count('User:'), 4)
        self.assertEqual(delegate.brief(None), '')

    def test_commands_quote_path_and_never_fork(self):
        path = self.root / "prompt's $(danger).md"
        suffix = f' "$(cat {shlex.quote(str(path))})"'
        name = "Fix 'quotes' $(danger)"
        self.assertEqual(delegate.launch_command(entry(), 'claude', path, name), 'claude' + suffix)
        self.assertEqual(delegate.launch_command(entry(), 'claude', path, name, context=True), 'claude' + suffix)
        self.assertEqual(delegate.launch_command(entry(), 'codex', path, name), 'codex' + suffix)
        self.assertEqual(delegate.launch_command(entry(), 'codex', path, name, profile='foundry'), 'codex -p foundry' + suffix)
        for profile in ('bad;command', '', 1):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                delegate.launch_command(entry(), 'codex', path, name, profile=profile)

    def test_prompt_files_are_private_unique_and_old_files_are_pruned(self):
        directory = self.root / 'delegate'
        directory.mkdir(mode=0o755)
        old = directory / 'old.md'
        old.write_text('old')
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        recent = directory / 'recent.md'
        recent.write_text('recent')
        first = delegate.write_prompt('task\nwith Unicode: ✓')
        second = delegate.write_prompt('second')
        self.assertNotEqual(first, second)
        self.assertRegex(first.name, r'^[a-f0-9]{32}\.md$')
        self.assertEqual(first.read_text(), 'task\nwith Unicode: ✓')
        self.assertEqual(first.stat().st_mode & 0o777, 0o600)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_start_creates_next_window_names_and_sends_only_to_worker(self):
        def result(argv, **kwargs):
            output = ('@3\t/source\n' if '#{window_id}\t#{pane_current_path}' in argv
                      else 'latest\t120\t40\n' if argv[1] == 'display-message' else '%13\n')
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr='')
        run = Mock(side_effect=result)
        with patch.object(delegate.review_links, 'add') as add, patch.object(delegate.time, 'time', return_value=123):
            pane, name = delegate.start(entry(), body(review=True), '/project', run=run)
        self.assertEqual((pane, name), ('%13', 'Fix the bug'))
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(calls[2][1:], ['new-window', '-a', '-t', '@3', '-c', '/project', '-P', '-F', '#{pane_id}'])
        self.assertEqual(calls[3][1:], ['set-option', '-p', '-t', '%13', '@chat_name', name, ';',
                                     'set-option', '-p', '-t', '%13', '@chat_name_at', '123000'])
        path, = (self.root / 'delegate').glob('*.md')
        self.assertEqual(calls[4][1:], ['send-keys', '-t', '%13', '-l', f'claude "$(cat {shlex.quote(str(path))})"'])
        self.assertEqual(calls[5][1:], ['send-keys', '-t', '%13', 'Enter'])
        self.assertEqual(len(calls), 6)
        add.assert_called_once_with(worker='%13', reviewer='%12', reviewer_thread=THREAD, name=name, agent='claude')

    def test_start_uses_selected_codex_profile_and_does_not_register_without_review(self):
        run = Mock()
        with patch.object(delegate, 'new_window_after', return_value='%13'), \
                patch.object(delegate.codex_provider, 'launch_profile', return_value='foundry') as profile, \
                patch.object(delegate.review_links, 'add') as add:
            delegate.start(entry(), body(agent='codex'), '/project', run=run)
        profile.assert_called_once_with()
        self.assertTrue(run.call_args_list[1].args[0][-1].startswith('codex -p foundry "$(cat '))
        add.assert_not_called()

    def test_invalid_profile_does_not_open_window_or_write_prompt(self):
        with patch.object(delegate.codex_provider, 'launch_profile', return_value='bad;command'), \
                patch.object(delegate, 'new_window_after') as window:
            with self.assertRaises(ValueError):
                delegate.start(entry(), body(agent='codex'), '/project', run=Mock())
        window.assert_not_called()
        self.assertFalse((self.root / 'delegate').exists())


class EndpointTests(unittest.TestCase):
    def request(self, payload, headers=None, denied=False, path='/mobile/api/delegate'):
        handler = object.__new__(server.Handler)
        handler.path = path
        handler.command = 'POST'
        encoded = json.dumps(payload).encode()
        handler.rfile = io.BytesIO(encoded)
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1',
                           'Content-Length': str(len(encoded)), **(headers or {})}
        handler.deny = Mock(return_value=denied)
        handler.respond = Mock()
        handler.do_POST()
        return handler.respond.call_args.args

    def test_review_stop_returns_result_without_choose_or_tmux_writes(self):
        with patch.object(server, 'choose') as choose, \
                patch.object(server.subprocess, 'run') as run, \
                patch.object(server.review_links, 'remove') as remove:
            for stopped in (True, False):
                remove.return_value = stopped
                self.assertEqual(self.request({'worker': '%13'}, path='/mobile/api/review-stop'),
                                 (200, {'stopped': stopped}))
                remove.assert_called_with('%13', None)
        choose.assert_not_called()
        run.assert_not_called()

    def test_review_stop_rejects_bad_worker_with_specific_error(self):
        with patch.object(server.review_links, 'remove') as remove:
            for worker in (None, '', '13', '%13;pwd', '%13\n', 13, [], {}):
                with self.subTest(worker=worker):
                    self.assertEqual(self.request({'worker': worker}, path='/mobile/api/review-stop'),
                                     (400, {'error': 'Select a tab to stop reviewing.'}))
        remove.assert_not_called()

    def test_review_stop_accepts_a_pair_and_rejects_invalid_reviewer(self):
        with patch.object(server.review_links, 'remove', return_value=True) as remove:
            self.assertEqual(self.request({'worker': '%13', 'reviewer': '%12'}, path='/mobile/api/review-stop'),
                             (200, {'stopped': True}))
            remove.assert_called_once_with('%13', '%12')
            remove.reset_mock()
            for reviewer in (None, '', 12, '%12\n', []):
                self.assertEqual(self.request({'worker': '%13', 'reviewer': reviewer}, path='/mobile/api/review-stop'),
                                 (400, {'error': 'Select a tab to stop reviewing.'}))
            remove.assert_not_called()

    def test_review_stop_preserves_auth_guards(self):
        with patch.object(server.review_links, 'remove') as remove:
            for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
                self.assertEqual(self.request({'worker': '%13'}, headers=headers,
                                              path='/mobile/api/review-stop')[0], 403)
            handler = object.__new__(server.Handler)
            handler.path = '/mobile/api/review-stop'
            handler.deny = Mock(return_value=True)
            handler.respond = Mock()
            handler.do_POST()
            handler.respond.assert_not_called()
        remove.assert_not_called()

    def test_sessions_adds_reviewers_and_survives_store_read_errors(self):
        rows = [{'id': '%13'}, {'id': '%12'}, {'id': '%20'}]
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/sessions'
        handler.deny = Mock(return_value=False)
        handler.respond = Mock()
        with patch.object(server, 'sessions', return_value=rows), \
                patch.object(server, 'session_row', side_effect=lambda row: dict(row)), \
                patch.object(server, 'settings', return_value={'thread': THREAD}), \
                patch.object(server.review_links, 'reviewed_by', return_value={'%13': ['%12', '%20'], '%12': []}) as reviewed_by:
            handler.do_GET()
            reviewed_by.assert_called_once_with()
            handler.respond.assert_called_with(200, {'sessions': [
                {'id': '%13', 'reviewers': ['%12', '%20']}, {'id': '%12'}, {'id': '%20'}], 'default_thread': THREAD})
            reviewed_by.reset_mock()
            reviewed_by.side_effect = OSError('disk')
            handler.do_GET()
            reviewed_by.assert_called_once_with()
            handler.respond.assert_called_with(200, {'sessions': rows, 'default_thread': THREAD})

    def test_route_bypasses_default_choose_and_holds_send_lock(self):
        source = entry()
        def start(config, request, folder, run, recent):
            self.assertTrue(server.SEND_LOCK.locked())
            self.assertIs(config, source)
            self.assertEqual(request, body())
            self.assertEqual(folder, str(paths.HOME))
            self.assertIs(run, server.subprocess.run)
            return '%13', 'Fix the bug'
        with patch.object(server, 'sessions', return_value=[source]), \
                patch.object(server, 'agent_folder', return_value='/missing/project'), \
                patch.object(server, 'choose') as choose, patch.object(delegate, 'start', side_effect=start):
            self.assertEqual(self.request(body()), (201, {'pane': '%13', 'name': 'Fix the bug'}))
        choose.assert_not_called()

    def test_existing_project_folder_is_used(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'sessions', return_value=[entry()]), \
                patch.object(server, 'agent_folder', return_value=folder), \
                patch.object(delegate, 'start', return_value=('%13', 'Fix the bug')) as start:
            self.assertEqual(self.request(body())[0], 201)
            self.assertEqual(start.call_args.args[2], folder)

    def test_browser_ignores_cwd_in_body(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'sessions', return_value=[entry()]), \
                patch.object(server, 'agent_folder', return_value=folder), \
                patch.object(delegate, 'start', return_value=('%13', 'task')) as start:
            self.assertEqual(self.request(body(cwd='/untrusted/body/path'))[0], 201)
            self.assertEqual(start.call_args.args[2], folder)
            self.assertNotIn('cwd', start.call_args.kwargs)

    def test_specific_validation_errors_and_auth_guards_prevent_launch(self):
        with patch.object(server, 'sessions', return_value=[entry()]), patch.object(delegate, 'start') as start:
            self.assertEqual(self.request(body(thread=None)),
                             (400, {'error': 'This tab changed conversations. Reload it and try again.'}))
            self.assertEqual(self.request(body(agent='bad'))[0], 400)
            for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
                self.assertEqual(self.request(body(), headers=headers)[0], 403)
            handler = object.__new__(server.Handler)
            handler.path = '/mobile/api/delegate'
            handler.deny = Mock(return_value=True)
            handler.respond = Mock()
            handler.do_POST()
            handler.respond.assert_not_called()
        start.assert_not_called()

    def test_accepts_20000_unicode_characters_in_json(self):
        with patch.object(server, 'sessions', return_value=[entry()]), \
                patch.object(server, 'agent_folder', return_value=None), \
                patch.object(delegate, 'start', return_value=('%13', 'task')):
            self.assertEqual(self.request(body(message='😀' * 20000))[0], 201)

    def test_success_expires_session_cache(self):
        handler = object.__new__(server.Handler)
        handler.path, handler.command = '/mobile/api/delegate', 'POST'
        handler.wfile = io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        with patch.object(server, 'forget_sessions') as forget:
            handler.respond(201, {'pane': '%13', 'name': 'task'})
        forget.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
