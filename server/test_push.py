import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

import push
import server


class PushTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(server.WATCHING, clear=True))
        self.enterContext(patch.dict(server.WATCHED, clear=True))
        self.clock = self.enterContext(patch.object(server.time, 'monotonic', return_value=100))
        self.entry = {'id': '%7', 'label': 'Agent task'}

    def poll(self, watch):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/chat?session=%257' + ('&watch=1' if watch else '')
        handler.headers = {}
        handler.client_address = ('127.0.0.1', 1234)
        handler.respond = Mock()
        with patch.object(handler, 'deny', return_value=False), patch.object(server, 'choose', return_value=self.entry), patch.object(server, 'chat_payload', return_value={}):
            handler.do_GET()
        handler.respond.assert_called_once_with(200, {})

    def test_host_route_returns_short_hostname(self):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/host'
        handler.headers = {}
        handler.client_address = ('127.0.0.1', 1234)
        handler.respond = Mock()
        with patch.object(handler, 'deny', return_value=False), patch.object(server.socket, 'gethostname', return_value='box.example.net'), patch.object(server.host_stats, 'enabled', return_value=False), patch.object(server, 'TTYD_PORT', 0):
            handler.do_GET()
        handler.respond.assert_called_once_with(200, {'name': 'box', 'resources': False, 'terminal': False})

    def test_watch_poll_expires_and_regular_poll_does_not_refresh_it(self):
        self.poll(False)
        self.assertFalse(server.watching('%7'))
        self.poll(True)
        self.assertTrue(server.watching('%7'))
        self.assertFalse(server.watching('%8'))
        self.clock.return_value = 109.9
        self.assertTrue(server.watching('%7'))
        self.poll(False)
        self.clock.return_value = 110
        self.assertFalse(server.watching('%7'))
        self.poll(True)
        self.assertTrue(server.watching('%7'))

    def test_open_stream_stays_watched_after_poll_expires(self):
        self.poll(True)
        server.WATCHING['%7'] = 1
        self.clock.return_value = 200
        self.assertTrue(server.watching('%7'))
        server.WATCHING['%7'] = 0
        self.assertFalse(server.watching('%7'))

    def test_notification_payload_contains_session_and_legacy_url(self):
        for state, kind in [('Approval', 'approval'), ('Input needed', 'input'), ('Idle', 'done')]:
            with self.subTest(state=state):
                send = Mock()
                notifier = push.Notifier(send, settle=0, minimum_work=0, clock=lambda: 100)
                notifier.observe(self.entry, {'state': 'Working'})
                notifier.observe(self.entry, {'state': state})
                payload, urgent = send.call_args.args
                self.assertEqual(payload['session'], '%7')
                self.assertEqual(payload['url'], '/mobile/#session=' + quote('%7', safe=''))
                self.assertEqual(payload['tag'], 'session-%7')
                self.assertEqual(payload['body'], push.Notifier.MESSAGES[kind])
                self.assertEqual(urgent, kind != 'done')
                notifier.observe(self.entry, {'state': state})
                send.assert_called_once()

    def test_approval_push_offers_approve_and_deny_only_for_a_plain_yes_no_prompt(self):
        options = lambda *texts: [{'id': i + 1, 'text': t} for i, t in enumerate(texts)]
        command = {'id': 'abc', 'actionable': True, 'text': '╭──╮\n│ Bash command │\n│   git push -q example-user HEAD │\n│ Push the branch │',
                   'options': options('Yes', "Yes, and don't ask again for git push", 'No, and tell Claude what to do differently (esc)')}
        cases = [(command, True),
                 (dict(command, actionable=False), False),
                 (dict(command, id=None), False),
                 (dict(command, options=options('Yes, auto mode', 'Yes, manually approve edits', 'No, keep planning')), False),
                 (dict(command, options=options('Yes, I trust this folder', 'No, exit')), False),
                 (dict(command, options=options("Yes, and don't ask again", 'No')), False),
                 (dict(command, text='Bash command\n  rm -rf build/'), False),
                 (dict(command, text='Bash command\n  git push --force origin main'), False),
                 (dict(command, text='Bash command\n  git push -qf origin main'), False),
                 (dict(command, text='Bash command\n  gcloud run deploy api'), False),
                 (dict(command, text='Bash command\n  echo ' + 'x' * 200), False)]
        for approval, offered in cases:
            with self.subTest(options=approval['options'][0]['text'], actionable=approval['actionable'], id=approval['id']):
                send = Mock()
                notifier = push.Notifier(send, settle=0, minimum_work=0, clock=lambda: 100)
                notifier.observe(dict(self.entry, thread='t1'), {'state': 'Approval', 'approval': approval})
                payload, urgent = send.call_args.args
                self.assertTrue(urgent)
                if offered:
                    self.assertEqual((payload['approval'], payload['thread']), ('abc', 't1'))
                    self.assertEqual(payload['body'], 'Needs approval: Bash command git push -q example-user HEAD Push the branch')
                else:
                    self.assertNotIn('approval', payload)
                    self.assertTrue(payload['body'].startswith('Needs approval: Bash command'))

    def test_notifier_does_not_send_while_watched(self):
        self.poll(True)
        send = Mock()
        notifier = push.Notifier(send, server.watching, settle=0, clock=lambda: self.clock.return_value)
        notifier.observe(self.entry, {'state': 'Approval', 'approval': {'id': 'first'}})
        send.assert_not_called()
        self.clock.return_value = 111
        notifier.observe(self.entry, {'state': 'Input needed', 'attention': {'id': 'second'}})
        self.assertEqual(send.call_args.args[0]['session'], '%7')



class MuteTests(unittest.TestCase):
    def test_set_muted_round_trip_and_validation(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / 'muted.json'
            self.assertEqual(push.muted(store, 'r1'), [])
            self.assertEqual(push.set_muted('%18', True, store, 'r1'), ['%18'])
            self.assertEqual(push.set_muted('%3', True, store, 'r1'), ['%18', '%3'])
            self.assertEqual(push.set_muted('%18', False, store, 'r1'), ['%3'])
            for bad in ('18', '%x', '../a', None):
                with self.assertRaises(ValueError):
                    push.set_muted(bad, True, store, 'r1')
            with self.assertRaises(ValueError):
                push.set_muted('%3', 'yes', store, 'r1')

    def test_mute_does_not_survive_a_tmux_restart(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / 'muted.json'
            push.set_muted('%3', True, store, 'run-a')
            self.assertEqual(push.muted(store, 'run-b'), [])  # %3 is a different pane now
            self.assertEqual(push.muted(store, ''), [])
            self.assertEqual(push.set_muted('%5', True, store, 'run-b'), ['%5'])  # old entries are dropped
            self.assertEqual(push.muted(store, 'run-a'), [])

    def test_muted_pane_sends_nothing(self):
        sent, now = [], [0]
        notifier = push.Notifier(lambda payload, urgent: sent.append(payload), clock=lambda: now[0], muted=lambda: ['%1'])
        for pane in ('%1', '%2'):
            notifier.observe({'id': pane, 'label': pane}, {'state': 'Approval', 'approval': {'id': 'a'}})
        now[0] = 10
        for pane in ('%1', '%2'):
            notifier.observe({'id': pane, 'label': pane}, {'state': 'Approval', 'approval': {'id': 'a'}})
        self.assertEqual([payload['session'] for payload in sent], ['%2'])


class TitleTests(unittest.TestCase):
    def test_title_uses_the_pane_name(self):
        title = push.notification_title
        self.assertEqual(title({'label': 'web:0.1', 'tmux_session': '$0', 'pane_name': 'api'}), 'web · api')
        self.assertEqual(title({'label': 'web:0.1', 'tmux_session': '$0', 'agent_name': 'fix login'}), 'web · fix login')
        self.assertEqual(title({'label': 'explorer-tether', 'tmux_session': '$0', 'agent_name': 'explorer-tether'}), 'explorer-tether')
        self.assertEqual(title({'label': 'web:0.0', 'tmux_session': '$0'}), 'web')
        self.assertEqual(title({'label': 'edgarization-31-oct', 'tmux_session': '$3', 'pane_name': 'review'}), 'edgarization-31-oct · review')
        self.assertEqual(title({'label': 'Agent task'}), 'Agent task')
        self.assertEqual(title({}), 'Agent')


if __name__ == '__main__':
    unittest.main()
