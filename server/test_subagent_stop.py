"""All terminal writes use a fake tmux runner and isolated transcript files."""
import io
import json
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


class FakeTasks:
    def __init__(self, meta):
        self.meta = meta
        self.names = ['Beta wait', 'Alpha wait']
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

    def screen(self):
        if not self.open:
            return self.before or f'──────────\n❯ {self.draft}\n──────────\n'
        if self.detail:
            hint = DETAIL_HINT.replace(' · x to stop', '') if self.stopped else DETAIL_HINT
            return f'general-purpose › {self.names[-1]}\n28s · 42.3k tokens · 1 tool · Haiku 5.5\n\n   Progress\n   › Bash(sleep 600)\n...\n{hint}'
        if self.after_move and any(key in (('Down',), ('Up',)) for key in self.keys):
            return self.after_move
        rows = '\n'.join(f"{'❯' if self.at == index and not self.stopped else ' '} ● {name}    running · Haiku 5.5"
                         for index, name in enumerate(self.names))
        return ('Background\n2 active agents\n  Local agents (2)\n' + rows + '\n'
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
                      'thread': 'chat', 'writable': True, 'owner_pid': 123}
        self.pane = FakeTasks(self.meta)
        self.verify = self.enterContext(patch.object(terminal, 'verify'))
        self.enterContext(patch.object(ask_bridge, 'overlay', side_effect=lambda entry, status: status))
        self.enterContext(patch.object(terminal, '_attention_seen', {}))
        self.addCleanup(subagents._cache.clear)
        subagents._cache.clear()

    def stop(self, identifier='alpha'):
        return stop.stop(self.entry, identifier, run=self.pane, sleep=lambda _: None)

    def test_list_stops_second_row_with_exact_keys_and_reads_after_each_key(self):
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Down',), ('x',), ('Escape',)])
        self.verify.assert_called_once_with(self.entry, self.pane)
        self.assertFalse(self.pane.needs_read)

    def test_detail_stops_and_closes_after_stop_hint_disappears(self):
        self.pane.detail = True
        self.assertTrue(self.stop()['ok'])
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
