import unittest
from unittest.mock import MagicMock

import terminal_input as terminal

ID = '0f8fad5b-d9cb-469f-a165-70867728950e'


def entry(**extra):
    return dict({'id': '%5', 'tmux_session': '$1', 'label': 'work', 'agent': 'claude', 'thread': ID, 'agent_name': 'docs-update', 'pane_name': ''}, **extra)


class ForkTests(unittest.TestCase):
    def test_name_takes_the_next_free_number_in_the_session(self):
        base = entry()
        others = [base, entry(id='%6', agent_name='docs-update 2'), entry(id='%7', tmux_session='$2', agent_name='docs-update 3')]
        self.assertEqual(terminal.fork_name(base, others), 'docs-update 3')

    def test_name_falls_back_to_pane_then_session_and_drops_an_old_number(self):
        self.assertEqual(terminal.fork_name(entry(agent_name='', pane_name='review 2'), []), 'review 2')
        self.assertEqual(terminal.fork_name(entry(agent_name='', pane_name='', label='my big session'), []), 'my big session 2')

    def run_fork(self, **extra):
        run = MagicMock(return_value=MagicMock(returncode=0, stdout='%9\n', stderr=''))
        pane, name = terminal.fork_chat(entry(**extra), [entry(**extra)], '/tmp', run=run)
        return pane, name, [call.args[0] for call in run.call_args_list]

    def test_claude_fork_opens_a_window_in_the_same_session(self):
        pane, name, calls = self.run_fork()
        self.assertEqual((pane, name), ('%9', 'docs-update 2'))
        self.assertEqual(calls[0][1:5], ['new-window', '-a', '-t', '%5'])
        self.assertIn(f"claude --resume {ID} --fork-session --name 'docs-update 2'", calls[2])
        self.assertIn('@chat_name', calls[1])

    def test_codex_fork(self):
        _, _, calls = self.run_fork(agent='codex')
        self.assertIn(f'codex fork {ID}', calls[2])

    def test_refuses_without_a_real_chat_and_never_calls_tmux(self):
        for extra in ({'agent': 'shell'}, {'thread': None}, {'thread': 'x; rm -rf ~'}, {'pending_conversation': True}):
            run = MagicMock()
            with self.assertRaises(ValueError):
                terminal.fork_chat(entry(**extra), [], '/tmp', run=run)
            run.assert_not_called()

    def test_quotes_a_name_with_shell_characters(self):
        _, _, calls = self.run_fork(agent_name="it's $(x)")
        self.assertIn("--name 'it'\"'\"'s $(x) 2'", calls[2][-1])


if __name__ == '__main__':
    unittest.main()
