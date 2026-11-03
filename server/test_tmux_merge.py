"""tmux_merge against a real tmux server in its own socket folder (TMUX_TMPDIR), never the user's tmux."""
import functools
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from pathlib import Path

import paths
import projects
import side_shells
import tmux_merge


@unittest.skipUnless(shutil.which(paths.TMUX) or os.path.exists(paths.TMUX), 'tmux is not installed')
class TmuxMergeTests(unittest.TestCase):
    def setUp(self):
        self.socket_dir = tempfile.mkdtemp(prefix='tmg', dir='/tmp')  # short: long socket paths fail
        env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')}
        env['TMUX_TMPDIR'] = self.socket_dir
        self.run_tmux = functools.partial(subprocess.run, env=env)
        scope = mock.patch.object(paths, 'SCOPE_TMUX', False)
        scope.start()
        self.addCleanup(scope.stop)
        self.a = self.new('alpha')
        self.tmux('split-window', '-t', '=alpha:')
        self.tmux('new-window', '-t', '=alpha:')
        self.b = self.new('beta')

    def tearDown(self):
        self.tmux('kill-server')
        shutil.rmtree(self.socket_dir, ignore_errors=True)

    def tmux(self, *args):
        return self.run_tmux([paths.TMUX, *args], capture_output=True, text=True, timeout=5)

    def new(self, name):
        self.tmux('new-session', '-d', '-s', name, '-x', '120', '-y', '40')
        return self.tmux('display-message', '-p', '-t', f'={name}:', '#{session_id}').stdout.strip()

    def panes(self, session):
        return self.tmux('list-panes', '-s', '-t', session + ':', '-F', '#{pane_id}').stdout.split()

    def sessions(self):
        return sorted(self.tmux('list-sessions', '-F', '#{session_name}').stdout.split())

    def test_merge_session_moves_every_window_and_closes_the_source(self):
        moving = self.panes(self.a)
        result = tmux_merge.merge('session', self.a, self.b, run=self.run_tmux)
        self.assertEqual((result['session'], result['name']), (self.b, 'beta'))
        self.assertEqual(self.sessions(), ['beta'])
        self.assertEqual(set(moving) - set(self.panes(self.b)), set())
        # The split window keeps both panes together.
        layout = self.tmux('list-windows', '-t', self.b + ':', '-F', '#{window_panes}').stdout.split()
        self.assertEqual(sorted(layout), ['1', '1', '2'])

    def with_projects(self):
        home = tempfile.mkdtemp(prefix='tmgp')
        self.addCleanup(shutil.rmtree, home, True)
        patch = mock.patch.object(paths, 'TETHER', Path(home) / 'tether')
        patch.start()
        self.addCleanup(patch.stop)
        ids = {}
        for session, pid, text in ((self.a, 'a1a1a1a1a1a1', 'Alpha rules.'), (self.b, 'b2b2b2b2b2b2', 'Beta rules.')):
            self.tmux('set-option', '-t', session, '@project', pid)
            projects.set_instructions(pid, text, 0)
            ids[session] = pid
        return ids[self.a], ids[self.b]

    def project(self, session):
        return self.tmux('show-options', '-v', '-t', session, '@project').stdout.strip()

    def archived(self):
        folder = projects.root() / '.archive'
        return sorted(item.name[:12] for item in folder.iterdir()) if folder.exists() else []

    def test_merge_asks_which_project_when_both_have_text(self):
        alpha, beta = self.with_projects()
        with self.assertRaises(ValueError) as asked:
            tmux_merge.merge('session', self.a, self.b, run=self.run_tmux)
        self.assertEqual(str(asked.exception), tmux_merge.CHOOSE)
        self.assertEqual(self.sessions(), ['alpha', 'beta'])  # nothing moved
        with self.assertRaises(ValueError):
            tmux_merge.merge('session', self.a, self.b, run=self.run_tmux, keep='both')
        tmux_merge.merge('session', self.a, self.b, run=self.run_tmux, keep='source')
        self.assertEqual(self.sessions(), ['beta'])
        self.assertEqual(self.project(self.b), alpha)
        self.assertEqual(self.archived(), [beta])
        self.assertIn('Alpha rules.', projects.render(alpha))

    def test_merge_keeps_the_target_project(self):
        alpha, beta = self.with_projects()
        tmux_merge.merge('session', self.a, self.b, run=self.run_tmux, keep='target')
        self.assertEqual(self.project(self.b), beta)
        self.assertEqual(self.archived(), [alpha])

    def test_merge_keeps_the_only_project_with_text(self):
        alpha, beta = self.with_projects()
        projects.set_instructions(beta, '', 1)  # beta is empty now: agents get nothing from it
        tmux_merge.merge('session', self.a, self.b, run=self.run_tmux)
        self.assertEqual(self.project(self.b), alpha)
        self.assertEqual(self.archived(), [beta])

    def test_merge_into_a_session_with_no_project_takes_the_source_project(self):
        alpha, _ = self.with_projects()
        self.tmux('set-option', '-u', '-t', self.b, '@project')
        tmux_merge.merge('session', self.a, self.b, run=self.run_tmux)
        self.assertEqual(self.project(self.b), alpha)
        self.assertEqual(self.archived(), [])

    def test_moving_a_pane_changes_no_project(self):
        alpha, beta = self.with_projects()
        tmux_merge.merge('pane', self.panes(self.a)[0], self.b, run=self.run_tmux)
        self.assertEqual((self.project(self.a), self.project(self.b)), (alpha, beta))
        self.assertEqual(self.archived(), [])

    def test_move_pane_keeps_its_id(self):
        pane = self.panes(self.a)[0]
        result = tmux_merge.merge('pane', pane, self.b, run=self.run_tmux)
        self.assertEqual(result, {'session': self.b, 'name': 'beta', 'pane': pane})
        self.assertIn(pane, self.panes(self.b))
        self.assertNotIn(pane, self.panes(self.a))

    def test_break_out_makes_a_new_session_with_only_that_pane(self):
        pane = self.panes(self.a)[0]
        result = tmux_merge.merge('break', pane, None, run=self.run_tmux)
        self.assertEqual(self.panes(result['session']), [pane])
        self.assertEqual(len(self.sessions()), 3)
        self.assertRegex(result['name'], r'^split-\d{6}$')

    def names(self, session):
        out = self.tmux('list-panes', '-s', '-t', session + ':', '-F', '#{pane_id}\t#{@chat_name}\t#{@chat_name_at}').stdout
        return {line.split('\t')[0]: tuple(line.split('\t')[1:]) for line in out.splitlines()}

    def test_merged_panes_keep_the_session_name(self):
        first, second, third = self.panes(self.a)
        self.tmux('set-option', '-p', '-t', second, '@chat_name', 'review')
        self.tmux('set-option', '-p', '-t', self.panes(self.b)[0], '@chat_name', 'alpha')  # already used in the target
        tmux_merge.merge('session', self.a, self.b, run=self.run_tmux)
        names = self.names(self.b)
        # A name the user gave stays; the others take the old session name, never a name the target already has.
        # No time is stored, so a later /rename in Claude or Codex still wins (agents.list_panes).
        self.assertEqual((names[first], names[second], names[third]), (('alpha 2', ''), ('review', ''), ('alpha 3', '')))

    def test_moved_pane_keeps_the_session_name_and_breaks_out_under_it(self):
        pane = self.panes(self.a)[0]
        tmux_merge.merge('pane', pane, self.b, run=self.run_tmux)
        self.assertEqual(self.names(self.b)[pane], ('alpha', ''))
        result = tmux_merge.merge('break', pane, None, run=self.run_tmux)
        self.assertEqual(result['name'], 'alpha 2')  # 'alpha' is still a session
        self.assertEqual(self.panes(result['session']), [pane])

    def test_numbered_session_gives_no_name(self):
        self.tmux('rename-session', '-t', self.a + ':', '7')
        pane = self.panes(self.a)[0]
        tmux_merge.merge('pane', pane, self.b, run=self.run_tmux)
        self.assertEqual(self.names(self.b)[pane], ('', ''))

    def test_refusals(self):
        lone = self.panes(self.b)[0]
        with self.assertRaisesRegex(ValueError, 'already a session'):
            tmux_merge.merge('break', lone, None, run=self.run_tmux)
        with self.assertRaisesRegex(ValueError, 'different session'):
            tmux_merge.merge('session', self.a, self.a, run=self.run_tmux)
        with self.assertRaisesRegex(ValueError, 'already in that session'):
            tmux_merge.merge('pane', lone, self.b, run=self.run_tmux)
        with self.assertRaisesRegex(ValueError, 'gone'):
            tmux_merge.merge('session', '$999', self.b, run=self.run_tmux)
        for bad in ('beta', '$1;kill-server', '%1 x', None):
            with self.assertRaises(ValueError):
                tmux_merge.merge('session', bad, self.b, run=self.run_tmux)
            with self.assertRaises(ValueError):
                tmux_merge.merge('pane', bad, self.b, run=self.run_tmux)
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            tmux_merge.merge('swap', self.a, self.b, run=self.run_tmux)
        self.assertEqual(self.sessions(), ['alpha', 'beta'])

    def test_terminal_panel_session_is_never_a_source_or_target(self):
        shells = self.new(side_shells.SESSION)
        with self.assertRaisesRegex(ValueError, 'Terminal panel'):
            tmux_merge.merge('session', self.a, shells, run=self.run_tmux)
        with self.assertRaisesRegex(ValueError, 'Terminal panel'):
            tmux_merge.merge('pane', self.panes(shells)[0], self.b, run=self.run_tmux)
        self.assertEqual(self.sessions(), ['alpha', 'beta', side_shells.SESSION])


if __name__ == '__main__':
    unittest.main()
