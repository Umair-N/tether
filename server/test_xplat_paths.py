"""macOS discovery and host samples, forced on Linux without real processes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import browser
import host_stats
import paths
import procinfo


VM_STAT = '''Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                5000.
Pages active:                            100000.
Pages inactive:                           90000.
Pages speculative:                         2000.
Pages throttled:                              0.
Pages wired down:                         50000.
Pages purgeable:                           1000.
Pages occupied by compressor:             20000.
'''


class XplatPathsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(procinfo, 'DARWIN', True))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(paths, 'HOME', self.home))

    def test_tmux_order_and_fallback(self):
        with patch.object(paths.shutil, 'which', return_value=None) as which, \
                patch.object(paths.os, 'access', return_value=False) as access:
            self.assertEqual(paths._find_tmux(), '/usr/bin/tmux')
            access.assert_has_calls([call('/opt/homebrew/bin/tmux', os.X_OK),
                                     call('/usr/local/bin/tmux', os.X_OK)])
            for chosen in ('/opt/homebrew/bin/tmux', '/usr/local/bin/tmux'):
                access.side_effect = lambda path, mode: path == chosen
                self.assertEqual(paths._find_tmux(), chosen)
            access.reset_mock()
            which.return_value = '/path/tmux'
            self.assertEqual(paths._find_tmux(), '/path/tmux')
            access.assert_not_called()
            with patch.dict(os.environ, MOBILE_TMUX_TMUX='/override/tmux'):
                which.reset_mock()
                self.assertEqual(paths._find_tmux(), '/override/tmux')
                which.assert_not_called()
            which.return_value = None
            with patch.object(procinfo, 'DARWIN', False):
                self.assertEqual(paths._find_tmux(), '/usr/bin/tmux')
                access.assert_not_called()

    def test_chrome_app_order(self):
        candidates = [str(Path(root) / (app + '.app') / 'Contents/MacOS' / app)
                      for root in ('/Applications', self.home / 'Applications')
                      for app in ('Chromium', 'Google Chrome')]
        with patch.object(browser.shutil, 'which', return_value=None), \
                patch.object(browser.os, 'access') as access, patch.object(browser.glob, 'glob') as glob:
            for index, chosen in enumerate(candidates):
                with self.subTest(chosen=chosen):
                    access.reset_mock()
                    access.side_effect = lambda path, mode: path == chosen
                    self.assertEqual(browser.find_chrome(), chosen)
                    self.assertEqual(access.call_args_list, [call(path, os.X_OK) for path in candidates[:index + 1]])
            glob.assert_not_called()
            with patch.object(browser.shutil, 'which', return_value='/path/chromium'):
                self.assertEqual(browser.find_chrome(), '/path/chromium')
            with patch.dict(os.environ, MOBILE_TMUX_CHROME='/override/chrome'):
                access.side_effect = lambda path, mode: path == '/override/chrome'
                self.assertEqual(browser.find_chrome(), '/override/chrome')

    def test_playwright_mac_globs_newest_executable(self):
        builds = []
        for build, arch, app in ((99, 'chrome-mac', 'Chromium'),
                                 (100, 'chrome-mac-arm64', 'Google Chrome for Testing'),
                                 (101, 'chrome-mac-x64', 'Chromium')):
            binary = self.home / 'Library/Caches/ms-playwright' / ('chromium-%d' % build) / arch / (app + '.app') / 'Contents/MacOS' / app
            binary.parent.mkdir(parents=True)
            binary.touch()
            binary.chmod(0o700)
            builds.append(binary)
        with patch.object(browser.shutil, 'which', return_value=None):
            for binary in reversed(builds):
                self.assertEqual(browser.find_chrome(), str(binary))
                binary.chmod(0o600)
            self.assertIsNone(browser.find_chrome())

    def test_signed_chrome_apps_only(self):
        candidates = [str(Path(root) / 'Google Chrome.app/Contents/MacOS/Google Chrome')
                      for root in ('/Applications', self.home / 'Applications')]
        with patch.object(browser.os, 'access') as access:
            for index, chosen in enumerate(candidates):
                access.reset_mock()
                access.side_effect = lambda path, mode: path == chosen
                self.assertEqual(browser.find_signed_chrome(), chosen)
                self.assertEqual(access.call_args_list, [call(path, os.X_OK) for path in candidates[:index + 1]])
            access.side_effect = None
            access.return_value = False
            self.assertIsNone(browser.find_signed_chrome())

    def test_linux_chrome_skips_mac_paths(self):
        with patch.object(procinfo, 'DARWIN', False), \
                patch.object(browser.shutil, 'which', return_value=None), \
                patch.object(browser.os, 'access') as access, \
                patch.object(browser.glob, 'glob', return_value=[]) as glob:
            self.assertIsNone(browser.find_chrome())
            access.assert_not_called()
            glob.assert_called_once_with(str(self.home / '.cache/ms-playwright/chromium-*/chrome-linux*/chrome'))

    def test_vm_stat_used(self):
        self.assertEqual(host_stats._vm_stat_used(VM_STAT), 2785280000)
        self.assertEqual(host_stats._vm_stat_used(VM_STAT.replace('16384', '4096')), 696320000)
        for text in ('', VM_STAT.replace('Pages active:', 'Missing:'), VM_STAT.replace('100000.', 'bad.')):
            with self.subTest(text=text):
                self.assertIsNone(host_stats._vm_stat_used(text))

    def test_darwin_host(self):
        with patch.object(host_stats.subprocess, 'run', side_effect=[
                Mock(returncode=0, stdout='8589934592\n'), Mock(returncode=0, stdout=VM_STAT)]) as run, \
                patch.object(host_stats.os, 'getloadavg', return_value=(1.0, 2.0, 3.0)), \
                patch.object(procinfo, 'uptime', return_value=123.5), \
                patch.object(host_stats.os, 'cpu_count', return_value=8), \
                patch.object(host_stats.os, 'statvfs', return_value=Mock(f_blocks=100, f_frsize=4096, f_bfree=25)), \
                patch.object(host_stats, 'read', side_effect=AssertionError('Darwin must not read proc')):
            self.assertEqual(host_stats.Sampler().host(), dict(cpu_pct=None, cpus=8, mem_total=8589934592,
                             mem_bytes=2785280000, load=[1.0, 2.0, 3.0], uptime=123.5,
                             disk_total=409600, disk_bytes=307200))
            self.assertEqual(run.call_args_list, [
                call(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True, timeout=2),
                call(['vm_stat'], capture_output=True, text=True, timeout=2)])

    def test_darwin_host_failures_are_independent(self):
        failures = [OSError('missing'), subprocess.TimeoutExpired('sysctl', 2),
                    subprocess.CalledProcessError(1, 'sysctl'), Mock(returncode=1, stdout='123'),
                    Mock(returncode=0, stdout='invalid')]
        with patch.object(host_stats.os, 'getloadavg', return_value=(0, 0, 0)), \
                patch.object(procinfo, 'uptime', side_effect=OSError('unavailable')):
            for failure in failures:
                with self.subTest(failure=failure), patch.object(host_stats.subprocess, 'run', side_effect=[
                        failure, Mock(returncode=0, stdout=VM_STAT)]):
                    host = host_stats.Sampler().host()
                    self.assertIsNone(host['mem_total'])
                    self.assertEqual(host['mem_bytes'], 2785280000)
                    self.assertIsNone(host['uptime'])
            for failure in failures:
                with self.subTest(vm_stat=failure), patch.object(host_stats.subprocess, 'run', side_effect=[
                        Mock(returncode=0, stdout='8589934592'), failure]):
                    host = host_stats.Sampler().host()
                    self.assertEqual(host['mem_total'], 8589934592)
                    self.assertIsNone(host['mem_bytes'])

    def test_missing_proc_sandbox_and_systemd_fallback(self):
        with patch('builtins.open', side_effect=FileNotFoundError):
            self.assertFalse(browser.needs_no_sandbox())
        with patch.object(paths, 'SYSTEMD_RUN', None), patch.object(paths, 'SCOPE_TMUX', True):
            self.assertEqual(paths.tmux_new_session(), [paths.TMUX])


if __name__ == '__main__':
    unittest.main()
