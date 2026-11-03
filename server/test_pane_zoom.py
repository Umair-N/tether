"""Zoom sizing and cleanup use mocked tmux; no commands reach a user's terminal."""
import json
import os
from pathlib import Path
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import pane_view


class ZoomSize(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cache = patch.object(pane_view.paths, 'CACHE', Path(folder.name))
        cache.start()
        self.addCleanup(cache.stop)
        self.width, self.height = 120, 40
        self.local, self.global_size = '', 'latest'
        self.zoomed, self.active = False, '%8'
        self.calls = []
        tmux = patch.object(pane_view, 'tmux', side_effect=self.tmux)
        tmux.start()
        self.addCleanup(tmux.stop)

    def tmux(self, *args):
        self.calls.append(args)
        output = ''
        if args[0] == 'display-message':
            fmt = args[-1]
            if fmt == '#{socket_path}\n#{pid}\n#{window_id}':
                output = '/example/tmux.sock\n123\n@2\n'
            elif fmt == '#{window_width} #{window_height}':
                output = '%d %d' % (self.width, self.height)
            elif fmt == '#{window_zoomed_flag} #{window_panes} #{window_id}':
                output = '%d 2 @2' % self.zoomed
            elif fmt == '#{pane_id}':
                output = self.active
            elif fmt == '#{window_zoomed_flag} #{pane_active}':
                output = '%d %d' % (self.zoomed, self.active == args[3])
            else:
                raise AssertionError(args)
        elif args[0] == 'show-options':
            output = self.global_size if '-gwv' in args else self.local
        elif args[0] == 'resize-pane':
            self.zoomed = not self.zoomed
            self.active = args[-1]
        elif args[0] == 'select-pane':
            self.active = args[-1]
        elif args[0] == 'resize-window':
            self.width, self.height = int(args[4]), int(args[6])
            self.local = 'manual'
        elif args[0] == 'set-option':
            self.local = '' if '-wu' in args else args[-1]
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, output, '')

    def phone_resize(self):
        self.width, self.height = 50, 20

    def test_restores_size_inheritance_zoom_and_focus(self):
        view = pane_view.zoom_enter('%7')
        state = json.loads((view[0] / 'state.json').read_text())
        self.assertEqual((state['width'], state['height'], state['window_size'], state['global_size']),
                         (120, 40, '', 'latest'))
        self.phone_resize()
        pane_view.zoom_leave(view)
        self.assertEqual((self.width, self.height, self.local, self.global_size), (120, 40, '', 'latest'))
        self.assertEqual((self.zoomed, self.active), (False, '%8'))
        self.assertFalse((view[0] / 'state.json').exists())

    def test_explicit_window_option_survives(self):
        for option in ('manual', 'smallest', 'largest', 'latest'):
            with self.subTest(option=option):
                self.local = option
                view = pane_view.zoom_enter('%7')
                self.phone_resize()
                pane_view.zoom_leave(view)
                self.assertEqual(self.local, option)

    def test_only_last_tab_restores_even_if_first_tab_exits_first(self):
        first = pane_view.zoom_enter('%7')
        self.phone_resize()
        second = pane_view.zoom_enter('%7')
        pane_view.zoom_leave(first)
        self.assertEqual((self.width, self.zoomed), (50, True))
        pane_view.zoom_leave(second)
        self.assertEqual((self.width, self.height, self.zoomed, self.active), (120, 40, False, '%8'))

    def test_last_tab_can_be_first_owner(self):
        first = pane_view.zoom_enter('%7')
        second = pane_view.zoom_enter('%7')
        self.phone_resize()
        pane_view.zoom_leave(second)
        self.assertEqual(self.width, 50)
        pane_view.zoom_leave(first)
        self.assertEqual(self.width, 120)

    def test_hard_kill_repairs_before_next_snapshot(self):
        dead = pane_view.zoom_enter('%7')
        self.phone_resize()
        dead[2].close()  # SIGKILL releases the lease without running cleanup
        next_view = pane_view.zoom_enter('%7')
        self.assertEqual(self.width, 120)
        state = json.loads((next_view[0] / 'state.json').read_text())
        self.assertEqual((state['width'], state['height']), (120, 40))
        self.phone_resize()
        pane_view.zoom_leave(next_view)
        self.assertEqual(self.width, 120)

    def test_dead_owner_does_not_restore_while_another_tab_is_live(self):
        first = pane_view.zoom_enter('%7')
        self.phone_resize()
        second = pane_view.zoom_enter('%7')
        first[2].close()
        third = pane_view.zoom_enter('%7')
        self.assertEqual(self.width, 50)
        pane_view.zoom_leave(second)
        self.assertEqual(self.width, 50)
        pane_view.zoom_leave(third)
        self.assertEqual(self.width, 120)

    def test_preexisting_zoom_is_preserved(self):
        self.zoomed, self.active = True, '%7'
        view = pane_view.zoom_enter('%7')
        self.phone_resize()
        pane_view.zoom_leave(view)
        self.assertTrue(self.zoomed)
        self.assertEqual(self.width, 120)

    def test_restore_failure_keeps_repair_record(self):
        view = pane_view.zoom_enter('%7')
        self.phone_resize()
        with patch.object(pane_view, 'zoom_restore', side_effect=subprocess.TimeoutExpired('tmux', 5)):
            with self.assertRaises(subprocess.TimeoutExpired):
                pane_view.zoom_leave(view)
        self.assertTrue((view[0] / 'state.json').exists())
        next_view = pane_view.zoom_enter('%7')
        self.assertEqual(self.width, 120)
        pane_view.zoom_leave(next_view)

    def test_failure_after_zoom_starts_restores_immediately(self):
        failed = False
        def failing_tmux(*args):
            nonlocal failed
            result = self.tmux(*args)
            if args[0] == 'resize-pane' and not failed:
                failed = True
                raise subprocess.TimeoutExpired('tmux', 5)
            return result
        with patch.object(pane_view, 'tmux', side_effect=failing_tmux):
            with self.assertRaises(subprocess.TimeoutExpired):
                pane_view.zoom_enter('%7')
        self.assertEqual((self.width, self.height, self.zoomed, self.active), (120, 40, False, '%8'))
        self.assertFalse(list(pane_view.paths.CACHE.glob('zoom/*/state.json')))


class ZoomExit(unittest.TestCase):
    def run_view(self, zoom=True, exit_path='stdin', initial_zero=False):
        control = Mock()
        events, handlers = [], {}
        control.kill.side_effect = lambda: events.append('kill')
        control.wait.side_effect = lambda **_: events.append('wait')
        def leave(_):
            events.append('restore')
        def ready(*_):
            if initial_zero and not events:
                events.append('zero-size')
                return [], [], []
            if exit_path in (signal.SIGHUP, signal.SIGTERM):
                handlers[exit_path]()
            if exit_path == 'broken-pipe':
                raise BrokenPipeError()
            return ([control.stdout] if exit_path in ('control-eof', 'control-exit') else [0]), [], []
        def install(sig, handler):
            handlers[sig] = handler
        shot = None if exit_path == 'pane-ended' else ((0, 0, 1, 50, 20), [''] * 20, 0)
        with patch.object(pane_view.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '$1', '')), \
             patch.object(pane_view.subprocess, 'Popen', return_value=control), \
             patch.object(pane_view.os, 'isatty', return_value=False), \
             patch.object(pane_view.os, 'write'), \
             patch.object(pane_view.os, 'get_terminal_size', side_effect=[os.terminal_size((0, 0))] * 2 +
                          [os.terminal_size((50, 20))] * 2 if initial_zero else None,
                          return_value=os.terminal_size((50, 20))), \
             patch.object(pane_view.os, 'read', return_value=b'%exit\n' if exit_path == 'control-exit' else b''), \
             patch.object(pane_view.signal, 'signal', side_effect=install), \
             patch.object(pane_view, 'snapshot', return_value=shot), \
             patch.object(pane_view.select, 'select', side_effect=ready), \
             patch.object(pane_view, 'zoom_enter', return_value='view') as enter, \
             patch.object(pane_view, 'zoom_leave', side_effect=leave) as cleanup:
            try:
                pane_view.run('%7', zoom=zoom)
            except (SystemExit, BrokenPipeError):
                if exit_path not in (signal.SIGHUP, signal.SIGTERM, 'broken-pipe'):
                    raise
        return control, events, enter, cleanup

    def test_exit_paths_detach_before_restore(self):
        for exit_path in ('stdin', 'pane-ended', 'control-eof', 'control-exit', 'broken-pipe', signal.SIGHUP, signal.SIGTERM):
            with self.subTest(exit_path=exit_path):
                control, events, enter, cleanup = self.run_view(exit_path=exit_path)
                self.assertEqual(events, ['kill', 'wait', 'restore'])
                enter.assert_called_once_with('%7')
                cleanup.assert_called_once_with('view')
                self.assertIn(b'refresh-client -C 50x20\n', [call.args[0] for call in control.stdin.write.call_args_list])

    def test_non_zoom_never_resizes_or_saves_state(self):
        control, events, enter, cleanup = self.run_view(zoom=False)
        self.assertEqual(events, ['kill', 'wait'])
        enter.assert_not_called()
        cleanup.assert_not_called()
        control.stdin.write.assert_not_called()

    def test_zero_initial_size_waits_before_enabling_client_sizing(self):
        control, events, enter, cleanup = self.run_view(initial_zero=True)
        self.assertEqual(events, ['zero-size', 'kill', 'wait', 'restore'])
        enter.assert_called_once_with('%7')
        cleanup.assert_called_once_with('view')
        self.assertIn(b"refresh-client -f '!ignore-size'\n", [call.args[0] for call in control.stdin.write.call_args_list])


@unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
class IsolatedZoom(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.socket = str(Path(self.folder.name) / 's')
        self.env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')}
        self.binary = shutil.which('tmux')
        self.tmux('new-session', '-d', '-s', 'zoom-test', '-x', '120', '-y', '40').check_returncode()
        self.addCleanup(self.tmux, 'kill-server')
        self.pane = self.tmux('display-message', '-p', '-t', 'zoom-test', '#{pane_id}').stdout.strip()
        self.tmux('split-window', '-t', self.pane).check_returncode()
        for replacement in (patch.object(pane_view, 'tmux', side_effect=self.tmux),
                            patch.object(pane_view.paths, 'CACHE', Path(self.folder.name) / 'cache')):
            replacement.start()
            self.addCleanup(replacement.stop)

    def tmux(self, *args):
        return subprocess.run([self.binary, '-f', '/dev/null', '-S', self.socket, *args],
                              env=self.env, capture_output=True, text=True, timeout=5)

    def dimensions(self):
        return self.tmux('display-message', '-p', '-t', self.pane, '#{window_width} #{window_height}').stdout.strip()

    def test_detached_window_restores_dimensions_and_option_scope(self):
        for option in ('', 'manual', 'latest', 'smallest', 'largest'):
            with self.subTest(option=option):
                self.tmux('resize-window', '-t', self.pane, '-x', '120', '-y', '40').check_returncode()
                if option:
                    self.tmux('set-option', '-w', '-t', self.pane, 'window-size', option).check_returncode()
                else:
                    self.tmux('set-option', '-wu', '-t', self.pane, 'window-size').check_returncode()
                before = self.dimensions()
                global_size = self.tmux('show-options', '-gwv', 'window-size').stdout
                view = pane_view.zoom_enter(self.pane)
                self.tmux('resize-window', '-t', self.pane, '-x', '50', '-y', '20').check_returncode()
                pane_view.zoom_leave(view)
                self.assertEqual(self.dimensions(), before)
                self.assertEqual(self.tmux('show-options', '-wqv', '-t', self.pane, 'window-size').stdout.strip(), option)
                self.assertEqual(self.tmux('show-options', '-gwv', 'window-size').stdout, global_size)

    def test_sigkill_releases_cross_process_lease_and_next_zoom_repairs(self):
        code = '''
import subprocess, sys
from pathlib import Path
import pane_view
binary, socket, cache, pane = sys.argv[1:]
def tmux(*args):
    return subprocess.run([binary, '-f', '/dev/null', '-S', socket, *args],
                          capture_output=True, text=True, timeout=5)
pane_view.tmux = tmux
pane_view.paths.CACHE = Path(cache)
view = pane_view.zoom_enter(pane)
tmux('resize-window', '-t', pane, '-x', '50', '-y', '20').check_returncode()
print('ready', flush=True)
sys.stdin.buffer.read()
pane_view.zoom_leave(view)
'''
        process = subprocess.Popen([sys.executable, '-c', code, self.binary, self.socket,
                                    str(pane_view.paths.CACHE), self.pane], cwd=Path(__file__).parent, env=self.env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'Zoom child did not start')
            self.assertEqual(process.stdout.readline(), b'ready\n')
            self.assertEqual(self.dimensions(), '50 20')
            # A second process joins without restoring while the child holds its lease.
            second = pane_view.zoom_enter(self.pane)
            self.assertEqual(self.dimensions(), '50 20')
            pane_view.zoom_leave(second)
            self.assertEqual(self.dimensions(), '50 20')
            process.kill()
            self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
            next_view = pane_view.zoom_enter(self.pane)
            self.assertEqual(self.dimensions(), '120 40')
            pane_view.zoom_leave(next_view)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()

    def test_attached_desktop_takes_over_after_phone_detaches(self):
        self.tmux('set-option', '-w', '-t', self.pane, 'window-size', 'latest').check_returncode()
        clients = []
        def attach(width, height):
            client = subprocess.Popen([self.binary, '-S', self.socket, '-C', 'attach-session', '-t', 'zoom-test'],
                                      env=self.env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            clients.append(client)
            client.stdin.write(b'refresh-client -C %dx%d\n' % (width, height))
            client.stdin.flush()
            return client
        def wait_for(width):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.dimensions().split()[0] == str(width):
                    return
                time.sleep(.02)
            self.fail('Isolated window did not reach width %d: %s' % (width, self.dimensions()))
        try:
            desktop = attach(160, 50)
            wait_for(160)
            view = pane_view.zoom_enter(self.pane)
            phone = attach(50, 20)
            wait_for(50)
            desktop.stdin.write(b'refresh-client -C 180x55\n')
            desktop.stdin.flush()
            phone.kill()
            phone.wait(timeout=5)
            pane_view.zoom_leave(view)
            wait_for(180)
            self.assertEqual(self.tmux('show-options', '-wqv', '-t', self.pane, 'window-size').stdout.strip(), 'latest')
        finally:
            for client in clients:
                if client.poll() is None:
                    client.kill()
                client.wait(timeout=5)
                client.stdin.close()


if __name__ == '__main__':
    unittest.main()
