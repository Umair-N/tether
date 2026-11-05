"""Release tests use temporary homes, fake systemctl, and random HTTP ports."""
import contextlib
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import threading
import time
import unittest
from unittest import mock

import updates
REAL_PATH = os.environ.get('PATH', '')
import test_install as install_tests

installer = install_tests.installer

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('tether_cli', str(ROOT / 'tether'))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        with updates._LOCK:
            updates._RUNNING = False
            updates._ATTEMPTED.clear()

    def test_version_comparison(self):
        self.assertLess(updates.version('v0.9.0'), updates.version('1.0.0'))
        self.assertLess(updates.version('1.0.0-rc2'), updates.version('1.0.0-rc10'))
        self.assertLess(updates.version('1.0.0-rc10'), updates.version('1.0.0'))
        for invalid in ('../1.0.0', 'v1', '1.0.0/foo', '1.0.0;echo x'):
            with self.assertRaises(ValueError):
                updates.version(invalid)

    def test_daily_cache_and_quiet_failure(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temp, mock.patch.object(updates.paths, 'CACHE', Path(temp)), \
                mock.patch.object(updates, 'mode', return_value='notify'), \
                mock.patch.object(updates, 'fetch_release', return_value={
                    'tag_name': 'v0.2.0', 'html_url': 'https://example.invalid/release', 'body': 'Changes'}) as fetch:
            with mock.patch.object(updates.threading, 'Thread') as thread:
                first = updates.payload()
                self.assertIsNone(first['latest'])
                self.assertTrue(first['checking'])
                self.assertTrue(updates.payload()['checking'])
                thread.assert_called_once()
                args = thread.call_args.kwargs['args']
                updates.refresh(*args)
                cached = updates.payload()
                self.assertTrue(cached['available'])
                self.assertFalse(cached['checking'])
                self.assertEqual(cached['notes'], 'Changes')
                thread.assert_called_once()
            fetch.assert_called_once()
            file = Path(temp) / 'updates.json'
            cache = json.loads(file.read_text())
            cache['checked_at'] = time.time() - updates.DAY - 1
            file.write_text(json.dumps(cache))
            updates._ATTEMPTED.clear()
            with mock.patch.object(updates, 'fetch_release', side_effect=OSError('offline')):
                updates.refresh(file, cache)
            self.assertEqual(updates.read_cache(file)['latest'], 'v0.2.0')
            self.assertGreater(updates.read_cache(file)['checked_at'], cache['checked_at'])
            with mock.patch.object(updates.threading, 'Thread') as thread:
                self.assertFalse(updates.payload()['checking'])
                thread.assert_not_called()

    def test_request_returns_while_network_waits(self):
        from tempfile import TemporaryDirectory
        started, finish = threading.Event(), threading.Event()
        workers = []
        real_thread = threading.Thread

        def fetch():
            started.set()
            finish.wait(2)
            raise OSError('offline')

        def thread(**kwargs):
            worker = real_thread(**kwargs)
            workers.append(worker)
            return worker

        with TemporaryDirectory() as temp, mock.patch.object(updates.paths, 'CACHE', Path(temp)), \
                mock.patch.object(updates, 'mode', return_value='notify'), \
                mock.patch.object(updates, 'fetch_release', side_effect=fetch), \
                mock.patch.object(updates.threading, 'Thread', side_effect=thread):
            try:
                self.assertTrue(updates.payload()['checking'])
                self.assertTrue(started.wait(1))
                self.assertFalse(finish.is_set())
                updates.payload()
                self.assertEqual(len(workers), 1)
            finally:
                finish.set()
                for worker in workers:
                    worker.join(3)
                    self.assertFalse(worker.is_alive())

    def test_off_never_checks(self):
        with mock.patch.object(updates, 'mode', return_value='off'), \
                mock.patch.object(updates.threading, 'Thread') as thread:
            self.assertEqual(updates.payload()['mode'], 'off')
            self.assertFalse(updates.payload()['checking'])
            thread.assert_not_called()

    def test_route_payload_and_auth(self):
        import server
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/update'
        handler.deny = mock.Mock(return_value=False)
        handler.respond = mock.Mock()
        expected = dict(current='0.1.0', latest='v0.2.0', available=True, url='https://example.invalid',
                        notes='Changes', checked_at=1, mode='notify', checking=False)
        with mock.patch.object(updates, 'payload', return_value=expected) as payload:
            handler.do_GET()
            handler.respond.assert_called_once_with(200, expected)
            handler.deny.return_value = True
            handler.do_GET()
            payload.assert_called_once()

    def test_release_actions_are_pinned(self):
        workflow = (ROOT / '.github/workflows/release.yml').read_text()
        actions = [line.strip() for line in workflow.splitlines() if 'uses:' in line]
        self.assertEqual(len(actions), 4)
        for action in actions:
            self.assertRegex(action, r'^- uses: actions/[a-z-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$')

    def test_banner_refetches_only_checking_and_stops(self):
        # Run the real component with fake React, requests, storage, and timers.
        node = shutil.which('node')
        if not node or not (ROOT / 'ui/node_modules/typescript').is_dir():
            self.skipTest('UI dependencies and Node are required for the banner test')
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('./ui/node_modules/typescript');
const source = fs.readFileSync('./ui/src/UpdateBanner.tsx', 'utf8');
const code = ts.transpileModule(source, {compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX
}}).outputText;
async function scenario(payloads, expectedCalls, disabledStorage = false) {
  let calls = 0, subscribe, snapshot;
  const timers = [], listeners = [];
  const storage = new Map();
  const context = {exports: {}, setTimeout: (fn, ms) => {
    assert.equal(ms, 15000); timers.push(fn);
  }, localStorage: {
    getItem: key => { if (disabledStorage) throw Error('disabled'); return storage.get(key) ?? null; },
    setItem: (key, value) => { if (disabledStorage) throw Error('disabled'); storage.set(key, value); }
  }, require: name => {
    if (name === 'react') return {useSyncExternalStore: (sub, get) => {
      subscribe = sub; snapshot = get; return get();
    }};
    if (name === './api') return {request: async path => {
      assert.equal(path, 'update'); const data = payloads[Math.min(calls++, payloads.length - 1)];
      if (data instanceof Error) throw data;
      return data;
    }};
    if (name === 'react/jsx-runtime') return {jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props})};
    if (name === 'lucide-react') return {X: 'X'};
    throw Error(name);
  }};
  vm.runInNewContext(code, context);
  const banner = context.exports.UpdateBanner;
  banner();
  const unsubscribe = subscribe(() => listeners.push(snapshot()));
  subscribe(() => {}); // Multiple mounts still share one request.
  const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
  await flush();
  let limit = 5;
  while (timers.length && limit--) { timers.shift()(); await flush(); }
  assert.equal(timers.length, 0);
  assert.equal(calls, expectedCalls);
  if (snapshot()) {
    const view = banner();
    assert.equal(view.props.children[0].props.children.join(''), 'Update available: v0.2.0');
    view.props.children[1].props.onClick();
    assert.equal(snapshot(), null);
    assert.equal(banner(), null);
  }
  unsubscribe(); subscribe(() => {}); await flush();
  assert.equal(calls, expectedCalls); // Resubscription never restarts discovery.
}
const pending = {latest: null, available: false, url: '', mode: 'notify', checking: true};
const complete = {latest: 'v0.2.0', available: true, url: 'https://example.invalid', mode: 'notify', checking: false};
(async () => {
  await scenario([pending, complete], 2);
  await scenario([pending], 4);
  await scenario([complete], 1);
  await scenario([complete], 1, true);
  await scenario([{...pending, mode: 'off'}], 1);
  await scenario([Error('offline')], 1);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run([node, '-'], input=script, text=True, cwd=ROOT, timeout=30,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class ReleaseTests(unittest.TestCase):
    setUp = install_tests.InstallTests.setUp
    plan = install_tests.InstallTests.plan
    apply = install_tests.InstallTests.apply
    calls = install_tests.InstallTests.calls

    def prepare(self, mode='notify'):
        self.root = self.home / '.local/share/tether'
        self.releases = self.root / 'releases'
        self.old = self.releases / '0.1.0'
        self.old.mkdir(parents=True)
        (self.old / 'VERSION').write_text('0.1.0\n')
        (self.old / 'ui/dist').mkdir(parents=True)
        (self.old / 'ui/dist/index.html').write_text('old')
        (self.old / 'install.py').write_bytes((ROOT / 'install.py').read_bytes())
        self.link = self.root / 'current'
        self.link.symlink_to(self.old)
        plan = self.plan('--updates', mode)
        plan['repo'] = str(self.link)
        self.apply(plan)
        self.state = self.home / '.config/tether/install.json'
        patch = mock.patch.dict(os.environ, TETHER_INSTALL_STATE=str(self.state))
        patch.start()
        self.addCleanup(patch.stop)
        self.asset_dir = Path(self.temp.name) / 'assets'
        self.asset_dir.mkdir()

        class Quiet(SimpleHTTPRequestHandler):
            def __init__(handler, *args, **kwargs):
                super().__init__(*args, directory=str(self.asset_dir), **kwargs)

            def log_message(handler, *args):
                pass

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        self.worker = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_http)
        self.base = f'http://127.0.0.1:{self.http.server_port}'
        self.release = self.bundle()

    def stop_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.worker.join(3)
        self.assertFalse(self.worker.is_alive())

    def bundle(self, unsafe=None):
        archive = self.asset_dir / 'tether-0.2.0.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name, content in [('VERSION', b'0.2.0\n'), ('install.py', (ROOT / 'install.py').read_bytes()),
                                  ('ui/dist/index.html', b'new'), ('tether', (ROOT / 'tether').read_bytes()),
                                  ('server/updates.py', (ROOT / 'server/updates.py').read_bytes()),
                                  ('server/paths.py', (ROOT / 'server/paths.py').read_bytes()),
                                  ('integrations/claude/tether/skills/setup/bootstrap.py',
                                   (ROOT / 'integrations/claude/tether/skills/setup/bootstrap.py').read_bytes())]:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o755 if name == 'tether' else 0o644
                tar.addfile(info, io.BytesIO(content))
            if unsafe:
                info = tarfile.TarInfo(unsafe)
                info.size = 1
                tar.addfile(info, io.BytesIO(b'x'))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (self.asset_dir / 'SHA256SUMS').write_text(f'{digest}  {archive.name}\n')
        return dict(tag_name='v0.2.0', html_url='https://example.invalid/release', assets=[
            dict(name=archive.name, browser_download_url=self.base + '/' + archive.name),
            dict(name='SHA256SUMS', browser_download_url=self.base + '/SHA256SUMS')])

    def update(self, *args):
        with mock.patch.object(updates, 'fetch_release', return_value=self.release), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(['update', '--yes', *args])

    def test_checksum_mismatch_refused(self):
        self.prepare()
        (self.asset_dir / 'SHA256SUMS').write_text('0' * 64 + '  tether-0.2.0.tar.gz\n')
        with mock.patch.object(cli, 'run') as run:
            self.assertEqual(self.update(), 1)
            run.assert_not_called()
        self.assertEqual(self.link.resolve(), self.old)

    def test_tar_traversal_absolute_and_links_refused(self):
        self.prepare()
        for name in ('../escape', '/absolute', './nested/../../escape'):
            self.release = self.bundle(name)
            self.assertEqual(self.update(), 1)
            self.assertEqual(self.link.resolve(), self.old)
        archive = self.asset_dir / 'link.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            info = tarfile.TarInfo('link')
            info.type = tarfile.SYMTYPE
            info.linkname = '../escape'
            tar.addfile(info)
        with self.assertRaises(ValueError):
            cli.extract(archive, self.asset_dir / 'out')

    def test_successful_switch_saved_choices_and_two_releases(self):
        self.prepare('auto')
        (self.releases / '0.0.1').mkdir()
        (self.releases / '0.0.1/VERSION').write_text('0.0.1\n')
        with mock.patch.object(cli, 'healthy', return_value=True) as health:
            self.assertEqual(self.update(), 0)
            health.assert_called_once_with(self.port, host='127.0.0.1')
        self.assertEqual(self.link.resolve(), self.releases / '0.2.0')
        self.assertEqual(sorted(p.name for p in self.releases.iterdir()), ['0.1.0', '0.2.0'])
        state = json.loads(self.state.read_text())
        self.assertEqual(state['repo'], str(self.link))
        self.assertEqual(state['updates'], 'auto')
        unit = (self.home / '.config/systemd/user/tether.service').read_text()
        self.assertIn(str(self.link / 'server/server.py'), unit)
        self.assertIn(['systemctl', '--user', 'restart', 'tether.service'], self.calls())
        with mock.patch.object(cli, 'healthy', return_value=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['rollback']), 0)
        self.assertEqual(self.link.resolve(), self.old)

    def test_cleanup_deletes_only_owned_releases(self):
        self.prepare()
        keep = ['notes', '0.0.2', '0.0.3', '0.0.4', '0.0.5']
        for name in keep + ['0.0.1']:
            directory = self.releases / name
            directory.mkdir()
            if name != '0.0.2':
                (directory / 'VERSION').write_text(name if name != '0.0.3' else '0.9.0')
        (self.releases / '0.0.4/.git').mkdir()
        (self.releases / '0.0.5/.git').write_text('gitdir: elsewhere')
        outside = self.home / 'outside'
        outside.mkdir()
        (outside / 'VERSION').write_text('0.0.6')
        (self.releases / '0.0.6').symlink_to(outside)
        (self.releases / '0.0.7').write_text('unrelated file')
        with mock.patch.object(cli, 'healthy', return_value=True):
            self.assertEqual(self.update(), 0)
        self.assertFalse((self.releases / '0.0.1').exists())
        for name in keep + ['0.0.6', '0.0.7', '0.1.0', '0.2.0']:
            self.assertTrue((self.releases / name).exists(), name)
        self.assertTrue(outside.exists())

    def test_failed_copy_removes_partial_and_retry_succeeds(self):
        self.prepare()
        copytree = cli.shutil.copytree

        def fail_copy(source, target):
            self.assertEqual(target, self.releases / '.0.2.0.partial')
            target.mkdir()
            (target / 'VERSION').write_text('partial')
            raise OSError('copy failed')

        with mock.patch.object(cli.shutil, 'copytree', side_effect=fail_copy), mock.patch.object(cli, 'run') as run:
            self.assertEqual(self.update(), 1)
            run.assert_not_called()
        self.assertFalse((self.releases / '.0.2.0.partial').exists())
        self.assertFalse((self.releases / '0.2.0').exists())
        with mock.patch.object(cli.shutil, 'copytree', wraps=copytree), mock.patch.object(cli, 'healthy', return_value=True):
            self.assertEqual(self.update(), 0)
        self.assertFalse((self.releases / '.0.2.0.partial').exists())

    def test_failed_rename_removes_partial(self):
        self.prepare()
        with mock.patch.object(cli.Path, 'rename', side_effect=OSError('rename failed')), mock.patch.object(cli, 'run') as run:
            self.assertEqual(self.update(), 1)
            run.assert_not_called()
        self.assertFalse((self.releases / '.0.2.0.partial').exists())
        self.assertFalse((self.releases / '0.2.0').exists())

    def test_override_state_rolls_back_home_units(self):
        self.prepare()
        original = self.state
        self.state = self.home / 'custom/state/install.json'
        self.state.parent.mkdir(parents=True)
        self.state.write_bytes(original.read_bytes())
        original.unlink()
        units = self.home / '.config/systemd/user'
        before = {path.name: path.read_bytes() for path in units.iterdir()}
        state_before = self.state.read_bytes()
        with mock.patch.dict(os.environ, TETHER_INSTALL_STATE=str(self.state)), \
                mock.patch.object(cli, 'healthy', return_value=False):
            self.assertEqual(self.update(), 1)
        self.assertEqual(self.state.read_bytes(), state_before)
        self.assertEqual({path.name: path.read_bytes() for path in units.iterdir()}, before)
        self.assertFalse((self.home / 'custom/systemd/user').exists())
        self.assertEqual(self.link.resolve(), self.old)

    def test_failed_health_rolls_back_state_and_units(self):
        self.prepare()
        backup = cli.snapshot(self.state)
        with mock.patch.object(cli, 'healthy', return_value=False):
            self.assertEqual(self.update(), 1)
        self.assertEqual(self.link.resolve(), self.old)
        self.assertEqual(cli.snapshot(self.state), backup)
        self.assertFalse((self.releases / '0.2.0').exists())
        restarts = [call for call in self.calls() if call[:3] == ['systemctl', '--user', 'restart']]
        self.assertGreaterEqual(len(restarts), 3)

    def test_installer_failure_rolls_back(self):
        self.prepare()
        backup = cli.snapshot(self.state)
        real_run = cli.run

        def fail_install(command):
            if '--apply' in command:
                self.state.write_text('{}')
                raise RuntimeError('installer failed')
            real_run(command)

        with mock.patch.object(cli, 'run', side_effect=fail_install):
            self.assertEqual(self.update(), 1)
        self.assertEqual(cli.snapshot(self.state), backup)
        self.assertEqual(self.link.resolve(), self.old)

    def test_git_checkout_refusal(self):
        self.prepare()
        state = json.loads(self.state.read_text())
        state['repo'] = str(ROOT)
        self.state.write_text(json.dumps(state))
        with mock.patch.object(updates, 'fetch_release') as fetch, mock.patch.object(cli, 'run') as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['update', '--check']), 1)
            self.assertIn('this is a git checkout: run git pull, then python3 install.py --apply', output.getvalue())
            fetch.assert_not_called()
            run.assert_not_called()

    def test_git_marker_counts_when_git_refuses_the_folder(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / '.git').write_text('gitdir: /missing/worktree\n')
            with mock.patch.object(cli.subprocess, 'run', return_value=subprocess.CompletedProcess([], 128, '', 'fatal')):
                self.assertTrue(cli.git_checkout(Path(folder)))

    def test_release_inside_a_home_git_repo_is_not_a_checkout(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, PATH=REAL_PATH):  # setUp puts fake tools first
                subprocess.run(['git', 'init', '-q', home], check=True)
                release = Path(home) / '.local/share/tether/releases/0.1.0'
                release.mkdir(parents=True)
                self.assertFalse(cli.git_checkout(release))
                self.assertTrue(cli.git_checkout(Path(home)))

    def test_check_does_not_download_or_restart(self):
        self.prepare()
        with mock.patch.object(cli, 'download') as download, mock.patch.object(cli, 'run') as run:
            self.assertEqual(self.update('--check', '--to', 'v0.2.0'), 0)
            download.assert_not_called()
            run.assert_not_called()

    def test_auto_respects_saved_mode(self):
        self.prepare()
        with mock.patch.object(updates, 'fetch_release') as fetch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['update', '--auto']), 0)
            fetch.assert_not_called()

    def test_reconfigure_release_keeps_current_unit_paths(self):
        self.prepare()
        args = installer.parser().parse_args(['--plan', '--updates', 'auto', '--no-build-ui'])
        plan = installer.make_plan(args, repo=self.old, home=self.home)
        self.assertEqual(plan['repo'], str(self.link))
        self.assertIn(str(self.link / 'server/server.py'), plan['units']['tether.service'])
        self.assertIn(str(self.link / 'tether'), plan['units']['tether-update.service'])

    def test_timer_only_in_auto_mode(self):
        for mode in ('notify', 'off', 'auto'):
            plan = self.plan('--updates', mode)
            self.assertEqual('tether-update.timer' in plan['units'], mode == 'auto')
            self.assertEqual('tether-update.service' in plan['units'], mode == 'auto')
            self.assertEqual(plan['options']['updates'], mode)
            if mode == 'auto':
                self.assertIn('RandomizedDelaySec=1h', plan['units']['tether-update.timer'])
                command = next(line.removeprefix('ExecStart=') for line in
                               plan['units']['tether-update.service'].splitlines() if line.startswith('ExecStart='))
                self.assertEqual(shlex.split(command)[-2:], ['update', '--auto'])
                self.assertNotIn(['systemctl', '--user', 'enable', '--now', 'tether-update.service'], plan['commands'])
                self.apply(plan)
                self.assertEqual(self.plan()['options']['updates'], 'auto')
        plan = self.plan('--updates', 'off')
        self.assertIn('tether-update.timer', plan['remove_units'])
        self.apply(plan)
        self.assertFalse((self.home / '.config/systemd/user/tether-update.timer').exists())

    def test_health_on_random_port_and_timeout(self):
        self.prepare()
        (self.asset_dir / 'chat').mkdir()
        (self.asset_dir / 'chat/index.html').write_text('healthy')
        self.assertTrue(cli.healthy(self.http.server_port, seconds=1))
        with mock.patch.object(cli, 'urlopen', side_effect=OSError('offline')):
            self.assertFalse(cli.healthy(self.http.server_port, seconds=.01))

    def test_health_probes_configured_host(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        for host, address in [('0.0.0.0', '127.0.0.1'), ('localhost', '127.0.0.1'),
                              ('::', '127.0.0.1'), ('127.0.0.2', '127.0.0.2'),
                              ('2001:db8::1', '[2001:db8::1]')]:
            with self.subTest(host=host), mock.patch.object(cli, 'urlopen', return_value=response) as fetch:
                self.assertTrue(cli.healthy(12345, host=host))
                self.assertEqual(fetch.call_args.args[0], f'http://{address}:12345/chat/')
        self.prepare()
        state = json.loads(self.state.read_text())
        state['options']['host'] = '127.0.0.2'
        self.state.write_text(json.dumps(state))
        with mock.patch.object(cli, 'healthy', return_value=True) as health:
            self.assertEqual(self.update(), 0)
            health.assert_called_once_with(self.port, host='127.0.0.2')


if __name__ == '__main__':
    unittest.main()
