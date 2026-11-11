"""All terminal writes use a fake tmux runner and isolated transcript files."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import ask_bridge
import server
import subagent_stop as stop
import subagents
import terminal_input as terminal


LIST_HINT = ('↑/↓ to select · Enter to view · f to foreground · x to stop · '
             'ctrl+x ctrl+k to stop all agents · Esc to close')
DETAIL_HINT = '← to go back · Esc/Enter/Space to close · x to stop · f to foreground'


class RowsTests(unittest.TestCase):
    def test_exact_screen_with_shells(self):
        screen = '''   Background
   3 active shells · 1 active agent

     Shells (3)
   ❯ ● Wait for /tmp/claude-1000/never-created-xyz to ex…   running
     ● python3 -c "import time; time.sleep(1500); print(…   running
     ● sleep 1500                                           running

     Local agents (1)
     ● Short build                                          running · Haiku 5.5

       Earlier in this session (0)

   ↑/↓ to select · Enter to view · x to stop · Esc to close
'''
        self.assertEqual(stop.rows(screen), [
            {'label': 'Wait for /tmp/claude-1000/never-created-xyz to ex…',
             'selected': True, 'section': 'Shells'},
            {'label': 'python3 -c "import time; time.sleep(1500); print(…',
             'selected': False, 'section': 'Shells'},
            {'label': 'sleep 1500', 'selected': False, 'section': 'Shells'},
            {'label': 'Short build', 'selected': False, 'section': 'Local agents'},
        ])

    def test_five_rows_across_sections_ignore_other_lines_and_stop_at_hint(self):
        screen = ('● Before section\nShells (2)\n❯ ● Shell one running\n'
                  '● Shell two completed\n\nOther text\nLocal agents (2)\n'
                  '● Agent one failed\n● Agent two stopped\nEarlier in this session (1)\n'
                  '● Old agent killed\n' + LIST_HINT + '\n● After hint running\n')
        parsed = stop.rows(screen)
        self.assertEqual(len(parsed), 5)
        self.assertEqual([row['section'] for row in parsed],
                         ['Shells', 'Shells', 'Local agents', 'Local agents', 'Earlier in this session'])
        self.assertEqual([row['label'] for row in parsed],
                         ['Shell one', 'Shell two', 'Agent one', 'Agent two', 'Old agent'])


class FakeTasks:
    def __init__(self, meta):
        self.meta = meta
        self.names = ['Beta wait', 'Alpha wait']
        self.shells = []
        self.at = 0
        self.detail = False
        self.open = False
        self.opens = True
        self.closes = True
        self.confirms = True
        self.stopped = False
        self.draft = ''
        self.before = None
        self.after_move = None
        self.keys = []
        self.commands = []
        self.needs_read = False
        self.fail_key = None
        self.on_stop = None
        self.detail_header = None

    def screen(self):
        if not self.open:
            return self.before or f'──────────\n❯ {self.draft}\n──────────\n'
        if self.detail:
            hint = DETAIL_HINT.replace(' · x to stop', '') if self.stopped else DETAIL_HINT
            header = self.detail_header or f'general-purpose › {self.names[-1]}'
            return f'{header}\n28s · 42.3k tokens · 1 tool · Haiku 5.5\n\n   Progress\n   › Bash(sleep 600)\n...\n{hint}'
        if self.after_move and any(key in (('Down',), ('Up',)) for key in self.keys):
            return self.after_move
        shells = '\n'.join(f"{'❯' if self.at == index and not self.stopped else ' '} ● {name}    running"
                           for index, name in enumerate(self.shells))
        rows = '\n'.join(f"{'❯' if self.at == index + len(self.shells) and not self.stopped else ' '} ● {name}    running · Haiku 5.5"
                         for index, name in enumerate(self.names))
        return ('Background\n2 active agents\n'
                + (f'  Shells ({len(self.shells)})\n{shells}\n\n' if self.shells else '')
                + '  Local agents (2)\n' + rows + '\n'
                + ('❯' if self.stopped else ' ') + '   Earlier in this session (2)\n' + LIST_HINT)

    def __call__(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[1] == 'capture-pane':
            self.needs_read = False
            return subprocess.CompletedProcess(argv, 0, self.screen(), '')
        if argv[1] == 'display-message' and 'pane_width' in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, '80\t24\t80\t24\tfake\tfake\tmanual', '')
        if argv[1] != 'send-keys':
            raise AssertionError(f'Unexpected tmux call: {argv}')
        if self.needs_read:
            raise AssertionError('A key was sent without reading the screen after the previous key')
        keys = tuple(argv[4:])
        self.keys.append(keys)
        self.needs_read = True
        if keys == self.fail_key:
            raise subprocess.CalledProcessError(1, argv, stderr='private terminal data')
        if keys == ('-l', '/tasks'):
            self.draft = '/tasks'
        elif keys == ('Enter',):
            if self.open:
                raise AssertionError('A second Enter must never reach the dialog')
            self.open = self.opens
            self.draft = ''
        elif keys in (('Down',), ('Up',)):
            self.at += 1 if keys == ('Down',) else -1
        elif keys == ('x',):
            if not self.open:
                raise AssertionError('x sent outside the tasks dialog')
            self.stopped = True
            if self.confirms and self.on_stop is not None:
                self.on_stop()
            if self.confirms:
                self.meta.write_text('{"description":"Alpha wait","stoppedByUser":true}')
        elif keys == ('Escape',):
            if not self.open:
                raise AssertionError('Escape would interrupt the main agent')
            if self.closes:
                self.open = False
        else:
            raise AssertionError(f'Forbidden keys: {keys}')
        return subprocess.CompletedProcess(argv, 0, '', '')


class StopTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        main = root / 'chat.jsonl'
        main.write_text('')
        agents = root / 'chat' / 'subagents'
        agents.mkdir(parents=True)
        (agents / 'agent-alpha.jsonl').write_text('{"type":"user","message":{"content":"Wait"}}\n')
        self.meta = agents / 'agent-alpha.meta.json'
        self.meta.write_text('{"description":"Alpha wait"}')
        self.entry = {'id': '%fake-stop', 'agent': 'claude', 'transcript': str(main),
                      'thread': 'chat', 'writable': True, 'owner_pid': 123, 'process_start': '456'}
        self.enterContext(patch.object(subagents, 'process_start', return_value='456'))
        self.enterContext(patch.object(subagents.procinfo, 'start_ms', return_value=1800000000000))
        self.pane = FakeTasks(self.meta)
        self.verify = self.enterContext(patch.object(terminal, 'verify'))
        self.enterContext(patch.object(ask_bridge, 'overlay', side_effect=lambda entry, status: status))
        self.enterContext(patch.object(terminal, '_attention_seen', {}))
        self.addCleanup(subagents._cache.clear)
        self.enterContext(patch.object(subagents, '_fd_links', return_value=[]))
        subagents._tasks_cache.clear()
        self.addCleanup(subagents._tasks_cache.clear)
        subagents._cache.clear()

    def stop(self, identifier='alpha'):
        return stop.stop(self.entry, identifier, run=self.pane, sleep=lambda _: None)

    def test_list_stops_second_row_with_exact_keys_and_reads_after_each_key(self):
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Down',), ('x',), ('Escape',)])
        self.verify.assert_called_once_with(self.entry, self.pane)
        self.assertFalse(self.pane.needs_read)

    def test_list_moves_across_shells_to_second_agent(self):
        self.pane.shells = ['First shell', 'Second shell']
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',),
                                         ('Down',), ('Down',), ('Down',), ('x',), ('Escape',)])

    def test_shell_with_matching_label_is_not_a_target(self):
        self.pane.shells = ['Alpha wait']
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',),
                                         ('Down',), ('Down',), ('x',), ('Escape',)])

    def test_detail_stops_and_closes_after_stop_hint_disappears(self):
        self.pane.detail = True
        self.assertTrue(self.stop()['ok'])
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('x',), ('Escape',)])

    def test_two_hour_old_log_under_live_owner_can_be_stopped(self):
        path = self.meta.with_name('agent-alpha.jsonl')
        old = subagents.time.time() - 2 * 60 * 60
        os.utime(path, (old, old))
        self.pane.detail = True
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('x',), ('Escape',)])

    def test_list_can_move_up(self):
        self.pane.names.reverse()
        self.pane.at = 1
        self.assertTrue(self.stop()['ok'])
        self.assertIn(('Up',), self.pane.keys)

    def test_cut_description_matches_in_list_and_detail(self):
        for detail in (False, True):
            with self.subTest(detail=detail):
                self.meta.write_text('{"description":"Alpha wait"}')
                self.pane = FakeTasks(self.meta)
                self.pane.detail = detail
                self.pane.names[-1] = 'Alpha…'
                self.assertTrue(self.stop()['ok'])

    def test_duplicate_or_ambiguous_cut_names_refuse_and_close(self):
        for names in (['Alpha wait', 'Alpha wait'], ['Alpha…', 'Alpha wait']):
            with self.subTest(names=names):
                self.pane = FakeTasks(self.meta)
                self.pane.names = names
                with self.assertRaisesRegex(stop.StopError, 'Two agents share this name. Stop it in the terminal.'):
                    self.stop()
                self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Escape',)])

    def test_mismatched_list_or_detail_refuses_and_closes(self):
        for detail in (False, True):
            with self.subTest(detail=detail):
                self.pane = FakeTasks(self.meta)
                self.pane.detail = detail
                self.pane.names = ['Beta wait', 'Alpha']  # no ellipsis means no prefix match
                with self.assertRaisesRegex(stop.StopError, 'Could not identify'):
                    self.stop()
                self.assertEqual(self.pane.keys[-1], ('Escape',))
                self.assertNotIn(('x',), self.pane.keys)

    def test_draft_refused_without_any_keys(self):
        self.pane.draft = 'Keep my draft'
        with self.assertRaisesRegex(stop.StopError, 'unsent draft'):
            self.stop()
        self.assertEqual(self.pane.keys, [])
        self.assertEqual(self.pane.draft, 'Keep my draft')

    def test_approval_refused_without_any_keys(self):
        self.pane.before = ('Bash command\n  rm a.py\nDo you want to proceed?\n'
                            '❯ 1. Yes\n  2. No\nEsc to cancel')
        with self.assertRaisesRegex(stop.StopError, 'waiting for you'):
            self.stop()
        self.assertEqual(self.pane.keys, [])

    def test_question_refused_without_any_keys_even_before_attention_delay(self):
        self.pane.before = 'Which option?\n❯ 1. First\n  2. Second\nEnter to select · Esc to cancel'
        with self.assertRaises(stop.StopError):
            self.stop()
        self.assertEqual(self.pane.keys, [])

    def test_non_idle_non_working_states_refused(self):
        for state in ('Input needed', 'Unavailable', 'Unknown'):
            with self.subTest(state=state), patch.object(terminal, 'live_status', return_value={'state': state}):
                with self.assertRaises(stop.StopError):
                    self.stop()
                self.assertEqual(self.pane.keys, [])

    def test_working_main_agent_can_stop_a_subagent(self):
        with patch.object(terminal, 'live_status', return_value={'state': 'Working'}):
            self.assertTrue(self.stop()['ok'])

    def test_never_opened_dialog_sends_no_escape(self):
        self.pane.opens = False
        with self.assertRaisesRegex(stop.StopError, 'tasks dialog did not open'):
            self.stop()
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',)])

    def test_agent_not_running_refused_without_terminal_writes(self):
        self.meta.write_text('{"finished":true}')
        with self.assertRaisesRegex(stop.StopError, 'not running'):
            self.stop()
        self.assertEqual(self.pane.commands, [])

    def test_invalid_ids_and_non_claude_refused_without_terminal_access(self):
        for identifier in ('../alpha', '', 'a/b', None, 'a' * 65):
            with self.subTest(identifier=identifier), self.assertRaises(stop.StopError):
                self.stop(identifier)
        self.entry['agent'] = 'codex'
        with self.assertRaisesRegex(stop.StopError, 'Only Claude'):
            self.stop()
        self.assertEqual(self.pane.commands, [])

    def test_verify_failure_never_drives_unverified_pane(self):
        self.verify.side_effect = RuntimeError('The Claude conversation changed. Reload its tab.')
        with self.assertRaisesRegex(stop.StopError, 'conversation changed'):
            self.stop()
        self.assertEqual(self.pane.commands, [])

    def test_failure_to_confirm_raises_after_closing(self):
        self.pane.confirms = False
        with self.assertRaisesRegex(stop.StopError, 'The agent did not stop. Check the terminal view.'):
            self.stop()
        self.assertFalse(self.pane.open)
        self.assertEqual(self.pane.keys[-1], ('Escape',))

    def test_leftover_dialog_is_closed_before_opening_another(self):
        self.pane.open = True
        self.assertTrue(self.stop()['ok'])
        self.assertEqual(self.pane.keys[0], ('Escape',))

    def test_dialog_that_will_not_close_gets_only_three_escapes(self):
        self.pane.closes = False
        with self.assertRaisesRegex(stop.StopError, 'tasks dialog did not close'):
            self.stop()
        self.assertEqual(self.pane.keys.count(('Escape',)), 3)

    def test_leftover_dialog_that_will_not_close_gets_only_three_escapes(self):
        self.pane.open = True
        self.pane.closes = False
        with self.assertRaisesRegex(stop.StopError, 'tasks dialog did not close'):
            self.stop()
        self.assertEqual(self.pane.keys, [('Escape',)] * 3)

    def test_readonly_panel_is_not_closed_with_escape(self):
        self.pane.before = 'Settings: Status   Config   Usage\nSome usage\nEsc to close'
        with self.assertRaises(stop.StopError):
            self.stop()
        self.assertEqual(self.pane.keys, [])

    def test_changed_list_after_move_refuses_stop_and_closes(self):
        self.pane.after_move = ('Local agents (1)\n❯ ● Different task    running · Haiku 5.5\n' + LIST_HINT)
        with self.assertRaisesRegex(stop.StopError, 'Could not identify'):
            self.stop()
        self.assertNotIn(('x',), self.pane.keys)
        self.assertEqual(self.pane.keys[-1], ('Escape',))

    def test_navigation_is_bounded_at_40_keys(self):
        self.pane.after_move = (
            'Local agents (2)\n❯ ● Beta wait    running · Haiku 5.5\n'
            '  ● Alpha wait    running · Haiku 5.5\n' + LIST_HINT)
        with self.assertRaisesRegex(stop.StopError, 'Could not reach'):
            self.stop()
        self.assertEqual(self.pane.keys.count(('Down',)), 40)
        self.assertNotIn(('x',), self.pane.keys)

    def test_key_failure_still_closes_and_does_not_expose_subprocess_output(self):
        self.pane.fail_key = ('Down',)
        with self.assertRaisesRegex(stop.StopError, '^Could not stop the agent. Check the terminal view.$'):
            self.stop()
        self.assertFalse(self.pane.open)
        self.assertEqual(self.pane.keys[-1], ('Escape',))


class ProcessStopTests(unittest.TestCase):
    setUp = StopTests.setUp
    stop = StopTests.stop
    def make_tasks(self):
        self.tasks = self.meta.parent.parent.parent / 'tmp' / 'cwd' / '11111111-1111-1111-1111-111111111111' / 'tasks'
        self.tasks.mkdir(parents=True)
        self.enterContext(patch.object(subagents, 'TASKS_TMP', self.tasks.parent.parent.parent))
        self.enterContext(patch.object(subagents, '_fd_links', return_value=[self.tasks]))
        self.output = self.tasks / 'shell02.output'
        self.output.write_text('output')
        records = [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'id': 'bash-start', 'name': 'Bash',
                 'input': {'description': 'Second wait', 'command': 'sleep 600\necho done'}}]}},
            {'type': 'user', 'timestamp': '2027-01-15T08:00:00Z', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'bash-start'}]},
             'toolUseResult': {'backgroundTaskId': 'shell02'}}]
        Path(self.entry['transcript']).write_text(''.join(json.dumps(record) + '\n' for record in records))
        self.pane.names = ['sleep 600']
        self.pane.shells = ['sleep 300', 'sleep 600']
        self.pane.at = 2
        self.pane.on_stop = lambda: self.output.write_text('output\n[killed]\n')

    def test_stop_task_moves_up_from_agent_to_second_shell_only(self):
        self.make_tasks()
        self.assertEqual(self.stop('shell02'), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Up',), ('x',), ('Escape',)])

    def test_running_earlier_chat_task_can_confirm_after_it_leaves_listing(self):
        self.make_tasks()
        first = Path(self.entry['transcript']).with_name(self.tasks.parent.name + '.jsonl')
        first.write_text(Path(self.entry['transcript']).read_text())
        Path(self.entry['transcript']).write_text('')
        self.assertTrue(self.stop('shell02')['ok'])
        self.assertEqual(subagents.listing(self.entry)['tasks'], [])

    def test_task_can_target_description_when_command_differs(self):
        self.make_tasks()
        self.pane.shells[-1] = 'Second wait'
        self.assertTrue(self.stop('shell02')['ok'])

    def test_task_detail_header_is_checked_before_x(self):
        for header, success in (('Shell › sleep 600', True), ('Second wait', True), ('Unrelated task', False)):
            with self.subTest(header=header):
                if not hasattr(self, 'tasks'):
                    self.make_tasks()
                self.output.write_text('output')
                self.pane = FakeTasks(self.meta)
                self.pane.detail = True
                self.pane.detail_header = header
                self.pane.on_stop = lambda: self.output.write_text('[killed]')
                if success:
                    self.assertTrue(self.stop('shell02')['ok'])
                else:
                    with self.assertRaisesRegex(stop.StopError, 'Could not identify that task. Stop it in the terminal.'):
                        self.stop('shell02')
                    self.assertNotIn(('x',), self.pane.keys)
                self.assertEqual(self.pane.keys[-1], ('Escape',))

    def test_task_ambiguous_shells_refuse_with_task_error(self):
        self.make_tasks()
        self.pane.shells = ['sleep 600', 'Second wait']
        with self.assertRaisesRegex(stop.StopError, 'Two tasks share this name'):
            self.stop('shell02')
        self.assertNotIn(('x',), self.pane.keys)
        self.assertEqual(self.pane.keys[-1], ('Escape',))

    def test_task_confirmation_accepts_finished_and_failed(self):
        self.make_tasks()
        for code, state in ((0, 'finished'), (2, 'failed')):
            with self.subTest(code=code):
                self.output.write_text('output')
                self.pane = FakeTasks(self.meta)
                self.pane.shells = ['sleep 600']
                self.pane.names = []
                self.pane.on_stop = lambda: self.output.write_text(f'[exited with code {code}]')
                self.assertEqual(self.stop('shell02'), {'ok': True, 'state': state})

    def test_task_draft_and_failed_confirmation_are_safe(self):
        self.make_tasks()
        self.pane.draft = 'Keep draft'
        with self.assertRaisesRegex(stop.StopError, 'unsent draft'):
            self.stop('shell02')
        self.assertEqual(self.pane.keys, [])
        self.pane.draft = ''
        self.pane.confirms = False
        with self.assertRaisesRegex(stop.StopError, 'The task did not stop'):
            self.stop('shell02')
        self.assertFalse(self.pane.open)

    def test_foreground_output_without_start_is_not_a_task(self):
        self.make_tasks()
        self.output.with_name('unknown1.output').write_text('')
        with self.assertRaisesRegex(stop.StopError, 'no longer available'):
            self.stop('unknown1')
        self.assertEqual(self.pane.commands, [])

    def test_old_silent_agent_can_be_stopped_after_clear(self):
        self.make_tasks()
        old = self.tasks.parent.name
        new = '22222222-2222-2222-2222-222222222222'
        project = self.meta.parent.parent.parent / 'projects' / 'project'
        folder = project / old / 'subagents'
        folder.mkdir(parents=True)
        path = folder / 'agent-alpha.jsonl'
        path.write_text('{"type":"user","message":{"content":"Wait"}}\n')
        self.pane.meta = path.with_suffix('.meta.json')
        self.pane.meta.write_text('{"description":"Alpha wait"}')
        (self.tasks / 'alpha.output').symlink_to(path)
        main = project / f'{new}.jsonl'
        main.write_text('')
        self.entry.update(transcript=str(main), thread=new)
        self.enterContext(patch.object(subagents, 'PROJECTS', project.parent))
        self.pane.names = ['Alpha wait']
        self.pane.shells = []
        self.pane.at = 0
        self.pane.on_stop = None
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})


class StopRouteTests(unittest.TestCase):
    def setUp(self):
        self.config = {'id': '%fake', 'thread': 'chat', 'agent': 'claude', 'writable': True}

    def handler(self, **extra):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/subagent-stop'
        payload = json.dumps({'session': '%fake', 'id': 'alpha', **extra}).encode()
        handler.rfile = io.BytesIO(payload)
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test',
                           'X-Codex-Mobile': '1', 'Content-Length': str(len(payload))}
        handler.respond = Mock()
        handler.deny = Mock(return_value=False)
        return handler

    def test_route_selects_session_and_stops_under_same_lock_as_model_picker(self):
        handler = self.handler()

        def apply(config, identifier):
            self.assertIs(config, self.config)
            self.assertEqual(identifier, 'alpha')
            self.assertTrue(server.SEND_LOCK.locked())
            return {'ok': True, 'state': 'stopped'}

        with patch.object(server, 'choose', return_value=self.config) as choose, \
                patch.object(stop, 'stop', side_effect=apply):
            handler.do_POST()
        choose.assert_called_once_with('%fake')
        handler.respond.assert_called_once_with(200, {'ok': True, 'state': 'stopped'})

    def test_auth_origin_and_write_header_required_before_stop(self):
        for blocked in ('auth', 'origin', 'header'):
            handler = self.handler()
            if blocked == 'auth':
                handler.deny.return_value = True
            elif blocked == 'origin':
                handler.headers['Origin'] = 'http://elsewhere.test'
            else:
                del handler.headers['X-Codex-Mobile']
            with self.subTest(blocked=blocked), patch.object(server, 'choose') as choose, patch.object(stop, 'stop') as apply:
                handler.do_POST()
                choose.assert_not_called()
                apply.assert_not_called()
                self.assertEqual(handler.rfile.tell(), 0)

    def test_stale_thread_or_readonly_pane_refused(self):
        handler = self.handler(thread='old')
        with patch.object(server, 'choose', return_value=self.config), patch.object(stop, 'stop') as apply:
            handler.do_POST()
            self.assertEqual(handler.respond.call_args.args[0], 409)
            self.config['writable'] = False
            handler = self.handler()
            handler.do_POST()
            self.assertEqual(handler.respond.call_args.args[0], 503)
            apply.assert_not_called()

    def test_error_message_uses_existing_write_error_shape(self):
        handler = self.handler()
        with patch.object(server, 'choose', return_value=self.config), \
                patch.object(stop, 'stop', side_effect=stop.StopError('The agent did not stop. Check the terminal view.')):
            handler.do_POST()
        handler.respond.assert_called_once_with(503, {
            'error': 'The agent did not stop. Check the terminal view.', 'draft': False})


if __name__ == '__main__':
    unittest.main()
