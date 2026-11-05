"""Signed grant lifecycle and fail-closed CDP scope. No live browser, profile, or tmux writes."""
import io
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import browser
import browser_api
import browser_cdp
import signed_access as signed
from test_browser import FakeHandler
from test_browser_cdp import Chrome, Capture
from test_browser_device import playwright_modules


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for isolated test state')


class SignedChrome(Chrome):
    def __init__(self):
        self.session_number = 0
        super().__init__()

    def answer(self, message):
        if message['method'] == 'Target.attachToTarget':
            self.session_number += 1
            return {'sessionId': 'S%d' % self.session_number}
        return super().answer(message)


class Requests:
    def ask(self, pane='%12', duration=15, **kwargs):
        results = []
        before = {row['id'] for row in self.access.state()['requests']}
        thread = threading.Thread(target=lambda: results.append(self.access.ask('Read the requested account', pane,
                                                                               duration, **kwargs)), daemon=True)
        thread.start()
        self.waiters.append(thread)
        wait_for(lambda: any(row['id'] not in before for row in self.access.state()['requests']))
        row = next(row for row in self.access.state()['requests'] if row['id'] not in before)
        return row['id'], thread, results

    def grant(self, pane='%12', duration=15, **kwargs):
        ident, thread, results = self.ask(pane, duration, **kwargs)
        self.access.allow(ident)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0]['ok'])
        return self.access.grants[ident], results[0]


class AccessTests(Requests, unittest.TestCase):
    def setUp(self):
        self.fake = SignedChrome()
        self.display = Mock()
        self.real = browser.Browser(signed=True, profile=Path('/nonexistent/signed-grant-test'), display=self.display,
                                    chrome_finder=lambda: '/fake/chrome',
                                    launcher=lambda chrome, profile, **kw: self.fake.launcher(chrome, profile))
        self.notifier = Mock()
        self.access = signed.Access(self.real, self.notifier, agent_finder=lambda pane: 'codex', request_seconds=3)
        self.waiters = []
        self.addCleanup(self.cleanup)
        self.ident = 0

    def cleanup(self):
        self.access.stop()
        self.real.stop()
        self.fake.close()
        for thread in self.waiters:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        if self.access.thread:
            self.assertFalse(self.access.thread.is_alive())
        if self.real.watcher:
            self.real.watcher.join(timeout=2)

    def client(self, grant):
        client = signed.Client(self.access, Capture(), grant)
        self.access.clients.add(client)
        client.open()
        self.addCleanup(client.cleanup)
        return client

    def command(self, client, method, params=None, session=None):
        self.ident += 1
        message = {'id': self.ident, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        client.handle(message)
        return client.transport.reply(self.ident)

    def test_deny_timeout_replacement_and_notification(self):
        ident, first, result = self.ask()
        request = self.access.state()['requests'][0]
        self.assertEqual(set(request), {'id', 'pane', 'agent', 'reason', 'minutes', 'url', 'created'})
        self.assertEqual(request['agent'], 'codex')
        payload = self.notifier.call_args.args[0]
        self.assertEqual(payload['body'], 'codex wants to use your signed-in browser: Read the requested account')
        self.assertTrue(self.notifier.call_args.kwargs['urgent'])
        next_id, second, next_result = self.ask()
        first.join(timeout=2)
        self.assertEqual(result, [{'ok': False, 'reason': 'denied'}])
        with self.assertRaises(ValueError):
            self.access.allow(ident)
        self.access.deny(next_id)
        second.join(timeout=2)
        self.assertEqual(next_result, [{'ok': False, 'reason': 'denied'}])
        self.access.request_seconds = .05
        self.assertEqual(self.access.ask('Timeout', '%1'), {'ok': False, 'reason': 'timeout'})
        self.assertEqual(self.access.state(), {'requests': [], 'grants': []})

    def test_validation_shortening_limit_and_exact_contract(self):
        for duration in (0, 61, True, 1.2, '15'):
            with self.assertRaises(ValueError):
                self.access.ask('Task', '%1', duration)
        for pane in (None, '', '%１２', '%1\n'):
            with self.assertRaises(ValueError):
                self.access.ask('Task', pane)
        with self.assertRaises(ValueError):
            self.access.ask('Task', '%1', url='chrome://settings')
        ident, thread, results = self.ask(duration=10, url='example.com')
        with self.assertRaises(ValueError):
            self.access.allow(ident, 11)
        self.access.allow(ident, 5)
        thread.join(timeout=2)
        result = results[0]
        self.assertEqual(set(result), {'ok', 'cdp', 'tab', 'expires'})
        self.assertAlmostEqual(result['expires'], time.time() + 300, delta=3)
        self.assertRegex(result['cdp'], r'^ws://127\.0\.0\.1:\d+/cdp-signed/[A-Za-z0-9_-]{43}$')
        grant = self.access.grants[ident]
        row = self.real.tab(result['tab']).row()
        self.assertEqual((row['mode'], row['owner'], row['controller'], row['grant']),
                         ('agent', {'pane': '%12'}, 'agent', {'id': ident, 'expires': result['expires']}))
        self.assertEqual(row['url'], 'https://example.com')
        self.grant('%13')
        third, waiter, _ = self.ask('%14')
        with self.assertRaisesRegex(ValueError, 'limit is 2'):
            self.access.allow(third)
        self.assertEqual(len(self.access.state()['grants']), 2)
        self.access.deny(third)
        waiter.join(timeout=2)
        self.assertNotIn(grant.token, json.dumps(self.real.state()))

    def test_targets_sessions_events_and_every_forbidden_command(self):
        grant, result = self.grant()
        foreign = self.real.new_tab()['id']
        other_grant, _ = self.grant('%13')
        client = self.client(grant)
        targets = self.command(client, 'Target.getTargets')['result']['targetInfos']
        self.assertEqual([info['targetId'] for info in targets], [result['tab']])
        for target in ('T0', foreign, other_grant.row['tab']):
            for method in ('Target.attachToTarget', 'Target.closeTarget', 'Target.getTargetInfo', 'Target.activateTarget'):
                self.assertIn('error', self.command(client, method, {'targetId': target, 'flatten': True}))
        session = self.command(client, 'Target.attachToTarget', {'targetId': result['tab'], 'flatten': True})['result']['sessionId']
        for method in ('Runtime.enable', 'Page.enable', 'Network.enable', 'Input.insertText', 'DOM.getDocument',
                       'Fetch.enable', 'Emulation.setTouchEmulationEnabled'):
            self.assertIn('result', self.command(client, method, session=session), method)
        forbidden = ['Storage.getCookies', 'Storage.clearDataForOrigin', 'Storage.clearCookies', 'Network.getAllCookies',
                     'Network.clearBrowserCookies', 'Network.clearBrowserCache', 'Network.setCookies', 'Browser.close',
                     'Browser.crash', 'Page.setDownloadBehavior', 'Browser.setPermission',
                     'Browser.grantPermissions', 'Browser.getBrowserCommandLine', 'SystemInfo.getInfo', 'SystemInfo.getProcessInfo',
                     'Target.attachToBrowserTarget', 'Target.createBrowserContext', 'Target.disposeBrowserContext',
                     'Target.sendMessageToTarget', 'Target.autoAttachRelated', 'Future.dangerousCommand']
        for method in forbidden:
            for at in (None, session):
                before = len(self.fake.commands)
                self.assertIn('error', self.command(client, method, session=at), (method, at))
                self.assertEqual(len(self.fake.commands), before, method)
        before = len(self.fake.commands)
        self.assertIn('result', self.command(client, 'Browser.setDownloadBehavior', {'behavior': 'allowAndName'}))
        self.assertEqual(len(self.fake.commands), before)
        self.assertIn('error', self.command(client, 'Browser.setDownloadBehavior', {'behavior': 'allow'}, session))
        self.assertIn('error', self.command(client, 'Network.getCookies', {'urls': ['https://other.test']}, session))
        self.assertIn('error', self.command(client, 'Page.navigate', {'url': 'file:///etc/passwd'}, session))
        self.assertIn('error', self.command(client, 'Page.enable', session='S-panel'))
        for params in ({'targetId': foreign}, {'browserContextId': 'default'}, {'nested': [{'targetId': foreign}]}):
            self.assertIn('error', self.command(client, 'Runtime.evaluate', params, session))
        self.command(client, 'Target.setDiscoverTargets', {'discover': True})
        self.command(client, 'Target.setAutoAttach', {'autoAttach': True, 'flatten': True, 'waitForDebuggerOnStart': True})
        # The real browser receives only its own startup discovery, never agent auto-attach.
        self.assertEqual(len(self.fake.sent('Target.setDiscoverTargets')), 1)
        self.assertEqual(self.fake.sent('Target.setAutoAttach'), [])
        before = len(client.transport.messages)
        for method in ('Target.targetCreated', 'Target.targetInfoChanged', 'Target.targetDestroyed'):
            client.target_event(method, {'targetInfo': {'targetId': foreign, 'type': 'page'}, 'targetId': foreign})
        self.assertEqual(len(client.transport.messages), before)

    def test_popup_extra_tabs_disconnect_and_end_keep_saved_tabs(self):
        grant, result = self.grant()
        client = self.client(grant)
        second = self.client(grant)
        self.command(client, 'Target.setAutoAttach', {'autoAttach': True, 'flatten': True})
        self.command(second, 'Target.setAutoAttach', {'autoAttach': True, 'flatten': True})
        popup = {'targetId': 'POPUP', 'type': 'page', 'openerId': result['tab'], 'url': 'about:blank'}
        self.fake.infos['POPUP'] = popup
        self.access.target_event('Target.targetCreated', {'targetInfo': popup})
        wait_for(lambda: 'POPUP' in self.real.tabs and 'POPUP' in client.sessions.values() and 'POPUP' in second.sessions.values())
        self.assertEqual(self.real.tab('POPUP').row()['grant']['id'], grant.row['id'])
        for _ in range(2):
            self.assertIn('result', self.command(client, 'Target.createTarget', {'url': 'https://example.test'}))
        self.assertIn('error', self.command(second, 'Target.createTarget'))
        client.cleanup()
        self.assertTrue(grant.active)
        self.assertEqual(self.access.state()['grants'][0]['id'], grant.row['id'])
        third = self.client(grant)  # Reconnect does not reset the extra-tab allowance.
        self.assertIn('error', self.command(third, 'Target.createTarget'))
        self.access.end(grant.row['id'])
        self.assertTrue(second.transport.closes)
        self.assertTrue(third.transport.closes)
        for target in grant.targets:
            tab = self.real.tab(target)
            self.assertEqual((tab.mode, tab.controller, tab.grant, tab.agent_client), ('saved', 'user', None, None))
        with self.assertRaises(browser.BrowserError):
            self.client(grant)
        with self.assertRaises(HTTPError) as error:
            urlopen(self.access.url(grant).replace('ws:', 'http:'), timeout=2)
        self.assertEqual(error.exception.code, 404)

    def test_timer_tab_close_browser_stop_and_handoff(self):
        grant, result = self.grant()
        client = self.client(grant)
        session = self.command(client, 'Target.attachToTarget', {'targetId': result['tab'], 'flatten': True})['result']['sessionId']
        self.real.set_device(result['tab'], 'phone')
        for method in ('Emulation.setDeviceMetricsOverride', 'Emulation.setLocaleOverride', 'Network.setUserAgentOverride'):
            self.assertIn('error', self.command(client, method, session=session))
        self.real.set_device(result['tab'], 'agent')
        self.assertIn('result', self.command(client, 'Emulation.setDeviceMetricsOverride', {'width': 800, 'height': 600}, session))
        self.assertIn('metrics', grant.emulation[result['tab']])
        self.access.target_event('Target.targetDestroyed', {'targetId': result['tab']})
        wait_for(lambda: not self.access.state()['grants'])
        self.assertTrue(client.closed)
        grant2, _ = self.grant()
        client2 = self.client(grant2)
        grant2.timer.cancel()
        grant2.row['expires'] = time.time() - 1
        before = len(self.fake.commands)
        client2.handle({'id': 888, 'method': 'Browser.getVersion'})
        self.assertEqual(len(self.fake.commands), before)
        self.assertTrue(client2.closed)
        # Exercise the same callback that the real expiry timer invokes.
        grant2.timer.function(*grant2.timer.args)
        self.assertEqual(self.real.tab(grant2.row['tab']).mode, 'saved')
        grant3, _ = self.grant()
        self.real.stop()
        self.assertFalse(grant3.active)
        self.assertEqual(self.access.state()['grants'], [])

    def test_timer_revokes_sockets_before_waiting_for_inflight_commands(self):
        # The production timer must revoke even while another worker holds Browser.lock.
        with patch.object(signed, 'minutes', return_value=1), \
                patch.object(signed.threading, 'Timer', wraps=threading.Timer) as timer:
            grant, _ = self.grant()
        self.assertAlmostEqual(timer.call_args.args[0], 60, delta=1)
        client = self.client(grant)
        grant.timer.cancel()
        with self.real.lock:
            timer_thread = threading.Timer(.01, self.access.end, args=(grant.row['id'],))
            timer_thread.start()
            wait_for(lambda: client.closed)
            self.assertFalse(grant.active)
            self.assertEqual(self.access.state()['grants'], [])
        timer_thread.join(timeout=2)
        self.assertFalse(timer_thread.is_alive())
        self.assertEqual(self.real.tab(grant.row['tab']).mode, 'saved')

    def test_popup_close_reaches_all_clients_and_browser_tab_session_is_hidden(self):
        grant, _ = self.grant()
        clients = [self.client(grant), self.client(grant)]
        popup = {'targetId': 'POPUP', 'type': 'page', 'openerId': grant.row['tab'], 'url': 'about:blank'}
        self.fake.infos['POPUP'] = popup
        self.access.target_event('Target.targetCreated', {'targetInfo': popup})
        wait_for(lambda: 'POPUP' in self.real.tabs)
        for client in clients:
            self.command(client, 'Target.setDiscoverTargets', {'discover': True})
            attached = self.command(client, 'Target.attachToTarget', {'targetId': 'POPUP', 'flatten': True})
            self.assertNotEqual(attached['result']['sessionId'], self.real.tab('POPUP').session)
        self.access.target_event('Target.targetDestroyed', {'targetId': 'POPUP'})
        for client in clients:
            self.assertNotIn('POPUP', client.sessions.values())
            self.assertTrue(any(event.get('method') == 'Target.detachedFromTarget' for event in client.transport.messages))
        self.assertNotIn('POPUP', grant.targets)
        for params in ({'targetId': []}, {'sessionId': {}}, {'nested': {'browserContextId': None}}):
            self.assertIn('error', self.command(clients[0], 'Browser.getVersion', params))
        for client in clients:
            before = len(client.transport.messages)
            client.event({'method': 'Target.attachedToTarget', 'sessionId': 'other', 'params': {'targetInfo': {'targetId': 'T0'}}})
            self.assertEqual(len(client.transport.messages), before)

    def test_panel_routes_and_stream_lists(self):
        ident, thread, _ = self.ask()
        handler = FakeHandler(browser_api.PREFIX + 'signed/access')
        browser_api.get(handler, signed_browser=self.real)
        self.assertEqual(handler.replies[0][1]['requests'][0]['id'], ident)
        handler = FakeHandler(browser_api.PREFIX + 'signed/allow', {'id': ident, 'minutes': 2})
        browser_api.post(handler, signed_browser=self.real)
        self.assertEqual(handler.replies[0][0], 200)
        thread.join(timeout=2)
        handler = FakeHandler(browser_api.PREFIX + 'tabs?browser=signed')
        browser_api.get(handler, signed_browser=self.real)
        state = handler.replies[0][1]
        self.assertEqual(state['requests'], [])
        self.assertEqual(state['grants'][0]['id'], ident)
        target = state['grants'][0]['tab']
        handler = FakeHandler('unused')
        handler.wfile = io.BytesIO()
        handler.send_response = handler.send_header = handler.end_headers = Mock()
        frame = (1, {'deviceWidth': 1280, 'deviceHeight': 800}, 'jpeg', 0)
        def changed(*args):
            self.access.end(ident)
            return frame
        def closed(*args):
            self.real.tabs.pop(target)
            return None
        with patch.object(self.real, 'first_frame', return_value=frame), \
                patch.object(self.real, 'wait_frame', side_effect=lambda *args: changed() if self.access.grants else closed()), \
                patch.object(browser_api.time, 'sleep'):
            browser_api.stream(handler, self.real, target, 640, 480, 'signed')
        self.assertIn('"id": "' + ident + '"', handler.wfile.getvalue().decode())
        self.assertIn('"requests": []', handler.wfile.getvalue().decode())
        self.assertIn('"grants": []', handler.wfile.getvalue().decode())
        handler = FakeHandler(browser_api.PREFIX + 'signed/end', {'id': ident})
        browser_api.post(handler, signed_browser=self.real)
        self.assertEqual(handler.replies[0][0], 200)
        denied, waiter, results = self.ask()
        handler = FakeHandler(browser_api.PREFIX + 'signed/deny', {'id': denied})
        browser_api.post(handler, signed_browser=self.real)
        waiter.join(timeout=2)
        self.assertEqual(results, [{'ok': False, 'reason': 'denied'}])

    def test_loopback_host_origin_and_cli_intake(self):
        # Start only a private bridge. No real tmux environment update or socket lookup occurs.
        with tempfile.TemporaryDirectory() as folder:
            proxy = browser_cdp.Proxy(config_path=Path(folder) / 'config.json', env_runner=Mock(), pane_finder=lambda *a: '%12')
            proxy.signed_access = self.access
            proxy.start()
            self.addCleanup(proxy.stop)
            url = proxy.url.replace('ws:', 'http:').replace('/cdp/', '/ask-signed/')
            results = []
            def request():
                with urlopen(Request(url, data=json.dumps({'text': 'Requested task'}).encode()), timeout=5) as response:
                    results.append(json.load(response))
            thread = threading.Thread(target=request, daemon=True)
            self.waiters.append(thread)
            thread.start()
            wait_for(lambda: self.access.state()['requests'])
            ident = self.access.state()['requests'][0]['id']
            self.access.deny(ident)
            thread.join(timeout=3)
            self.assertEqual(results, [{'ok': False, 'reason': 'denied'}])
        grant, _ = self.grant()
        endpoint = self.access.url(grant).replace('ws:', 'http:')
        for headers in ({'Origin': 'http://evil.test'}, {'Host': 'evil.test'}, {'Host': 'localhost:1'}):
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(endpoint, headers=headers), timeout=2)
            self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.access.httpd.server_address[0], '127.0.0.1')


REAL_CLIENT = r'''
process.on('uncaughtException', error => {console.log(JSON.stringify({error:error.stack})); process.exit(1)});
const {chromium} = require('playwright-core');
(async () => {
  const connection = await chromium.connectOverCDP(process.env.E2E_CDP_URL, {timeout:15000});
  const pages = connection.contexts()[0].pages();
  if (pages.length !== 1) throw Error('Expected exactly one granted page: ' + pages.length);
  await pages[0].goto(process.env.E2E_PAGE_URL);
  const title = await pages[0].title();
  console.log(JSON.stringify({ready:true, pages:pages.length, title}));
  await new Promise(resolve => connection.on('disconnected', resolve));
  let refused = false;
  try { await chromium.connectOverCDP(process.env.E2E_CDP_URL, {timeout:3000}); }
  catch { refused = true; }
  console.log(JSON.stringify({disconnected:true, refused}));
})().catch(error => {console.log(JSON.stringify({error:error.stack})); process.exit(1)});
'''


@unittest.skipUnless(browser.find_signed_chrome() and shutil.which('Xvfb') and shutil.which('node') and playwright_modules(),
                     'Google Chrome, Xvfb, node, and cached playwright-core are required')
class RealSignedAccessTests(Requests, unittest.TestCase):
    def test_allow_playwright_navigation_end_and_token_refusal(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Page(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                data = b'<title>Signed grant test</title><h1>Only this tab</h1>'
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        with tempfile.TemporaryDirectory(prefix='tether-signed-grant-real-') as folder:
            self.real = browser.Browser(signed=True, profile=Path(folder) / 'signed/profile')
            self.access = signed.Access(self.real)
            self.waiters = []
            page_server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
            page_thread = threading.Thread(target=page_server.serve_forever, daemon=True)
            page_thread.start()
            node = None
            try:
                self.real.new_tab('about:blank')  # Mak's unrelated saved tabs must remain invisible.
                grant, result = self.grant()
                print(json.dumps({'chrome_pid': self.real.pid, 'xvfb_pid': self.real.display.process.pid}), flush=True)
                node = subprocess.Popen(['timeout', '45', 'node', '-e', REAL_CLIENT], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
                                        env=dict(os.environ, NODE_PATH=playwright_modules(), E2E_CDP_URL=result['cdp'],
                                                 E2E_PAGE_URL=f'http://127.0.0.1:{page_server.server_port}/'))
                print(json.dumps({'node_timeout_pid': node.pid}), flush=True)
                lines = queue.Queue()
                def read():
                    for line in node.stdout:
                        lines.put(line)
                reader = threading.Thread(target=read, daemon=True)
                reader.start()
                first = lines.get(timeout=25)
                print(first, end='', flush=True)
                self.assertEqual(json.loads(first), {'ready': True, 'pages': 1, 'title': 'Signed grant test'})
                self.access.end(grant.row['id'])
                last = lines.get(timeout=10)
                print(last, end='', flush=True)
                self.assertEqual(json.loads(last), {'disconnected': True, 'refused': True})
                self.assertEqual(node.wait(timeout=5), 0)
                self.assertEqual(self.real.tab(grant.row['tab']).mode, 'saved')
            finally:
                if node:
                    if node.poll() is None:
                        os.killpg(node.pid, signal.SIGTERM)
                        node.wait(timeout=5)
                    reader.join(timeout=2)
                    node.stdout.close()
                self.access.stop()
                self.real.stop()
                if self.real.watcher:
                    self.real.watcher.join(timeout=2)
                for waiter in self.waiters:
                    waiter.join(timeout=2)
                page_server.shutdown()
                page_server.server_close()
                page_thread.join(timeout=2)
