"""Isolated device credentials, connection lifetime, routing and one-tab CDP contracts."""
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import browser
import browser_api
import browser_cdp as cdp
import mac_browser as mac
import signed_access as signed
from test_browser import FakeHandler
from test_browser_cdp import Capture
from test_browser_device import playwright_modules
from test_signed_access import wait_for

ORIGIN = 'chrome-extension://' + 'a' * 32


class PairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='p20-pair-')
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.broker = mac.Broker(Path(self.temp.name) / 'devices.json', clock=lambda: self.now)

    def test_once_expiry_hash_reload_origin_and_revoke(self):
        code = self.broker.pair()
        self.assertEqual(code['expires'], 1300)
        pair = self.broker.exchange(code['code'], ORIGIN, 'Test Chrome')
        content = self.broker.store.read_text()
        self.assertNotIn(pair['token'], content)
        self.assertIn('hash', content)
        self.assertEqual(self.broker.store.stat().st_mode & 0o777, 0o600)
        other = mac.Broker(self.broker.store)
        self.assertEqual(other.authenticate(pair['token'], ORIGIN), pair['id'])
        self.assertNotIn('hash', self.broker.rows()[0])
        for token, origin in ((pair['token'], 'https://example.test'), ('x' * 43, ORIGIN),
                              (pair['token'], 'chrome-extension://' + 'b' * 32)):
            with self.assertRaises(ValueError):
                other.authenticate(token, origin)
        with self.assertRaises(ValueError):
            self.broker.exchange(code['code'], ORIGIN, 'Again')
        expired = self.broker.pair()
        self.now += 301
        with self.assertRaises(ValueError):
            self.broker.exchange(expired['code'], ORIGIN, 'Late')
        self.broker.revoke(pair['id'])
        with self.assertRaises(ValueError):
            self.broker.authenticate(pair['token'], ORIGIN)
        self.assertEqual(self.broker.rows(), [])

    def test_invalid_origin_does_not_consume_code_and_routes_hide_credentials(self):
        code = self.broker.pair()['code']
        with self.assertRaises(ValueError):
            self.broker.exchange(code, 'null', 'Chrome')
        self.broker.exchange(code, ORIGIN, 'Chrome')
        with patch.object(mac, 'BROKER', self.broker):
            handler = FakeHandler(browser_api.PREFIX + 'extension/devices')
            browser_api.get(handler)
            self.assertEqual(handler.replies[0][0], 200)
            self.assertNotIn('hash', json.dumps(handler.replies))
            handler = FakeHandler(browser_api.PREFIX + 'extension/pair', {})
            browser_api.post(handler)
            self.assertIn('code', handler.replies[0][1])


class FakeTransport:
    closed = False
    def __init__(self):
        self.device = None
        self.sent = []

    def message(self, message):
        self.sent.append(message)
        if message['type'] == 'grant':
            self.device.handle({'type': 'granted', 'id': message['id'], 'tab': 7, 'target': 'T7',
                                'expires': (time.time() + 60) * 1000, 'epoch': self.device.epoch})
        elif message['type'] == 'command':
            self.device.handle({'type': 'result', 'id': message['id'], 'result': {}, 'epoch': self.device.epoch})

    def abort(self):
        self.closed = True


class ChromiumTransport(FakeTransport):
    """One real debugger session, shared by every synthetic Mac session."""
    def __init__(self):
        super().__init__()
        self.temp = tempfile.TemporaryDirectory(prefix='tether-mac-contexts-')
        self.events = []
        self.pid, read_fd, write_fd = browser.launch(browser.find_chrome(), Path(self.temp.name) / 'profile')
        self.conn = browser.Connection(read_fd, write_fd, self.event)
        try:
            targets = self.conn.call('Target.getTargets')['targetInfos']
            self.target = next(row['targetId'] for row in targets if row['type'] == 'page')
            self.session = self.conn.call('Target.attachToTarget',
                                          {'targetId': self.target, 'flatten': True})['sessionId']
        except Exception:
            self.abort()
            raise

    def event(self, method, params, session):
        if session == self.session and self.device:
            self.events.append((method, params))
            self.device.handle({'type': 'event', 'epoch': self.device.epoch, 'grant': self.grant,
                                'tab': 7, 'method': method, 'params': params})

    def message(self, message):
        self.sent.append(message)
        if message['type'] == 'grant':
            self.grant = message['id']
            self.device.handle({'type': 'granted', 'id': self.grant, 'tab': 7, 'target': self.target,
                                'expires': (time.time() + 60) * 1000, 'epoch': self.device.epoch})
        elif message['type'] == 'command':
            def reply(result):
                self.device.handle({'type': 'error' if 'error' in result else 'result', 'id': message['id'],
                                    'result': result.get('result', {}), 'epoch': self.device.epoch})
            self.conn.forward({'method': message['method'], 'params': message['params'],
                               'sessionId': self.session}, reply)

    def abort(self):
        if self.closed:
            return
        super().abort()
        try:
            os.killpg(self.pid, signal.SIGTERM)
            try:
                wait_for(lambda: os.waitpid(self.pid, os.WNOHANG)[0], seconds=5)
            except AssertionError:
                os.killpg(self.pid, signal.SIGKILL)
                wait_for(lambda: os.waitpid(self.pid, os.WNOHANG)[0], seconds=5)
        finally:
            self.conn._shut()
            self.conn.reader.join(timeout=2)
            self.temp.cleanup()


class GrantHarness:
    transport_type = FakeTransport

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='p20-grant-')
        self.addCleanup(self.temp.cleanup)
        self.broker = mac.Broker(Path(self.temp.name) / 'devices.json')
        pair = self.broker.exchange(self.broker.pair()['code'], ORIGIN, 'Test Chrome')
        self.transport = self.transport_type()
        self.device = self.broker.register(pair['id'], self.transport, 'Chrome/147.0')
        self.transport.device = self.device
        self.access = mac.MacAccess(self.broker, request_seconds=3)
        self.access._listen = lambda: None
        self.waiters = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.broker.stop()
        for waiter in self.waiters:
            waiter.join(timeout=4)
            self.assertFalse(waiter.is_alive())

    def grant(self):
        result = []
        thread = threading.Thread(target=lambda: result.append(self.access.ask('Test task', '%12', 1)))
        thread.start()
        self.waiters.append(thread)
        wait_for(lambda: self.access.state()['requests'])
        row = self.access.state()['requests'][0]
        self.assertEqual(row['browser'], 'mac')
        self.access.allow(row['id'])
        thread.join(timeout=3)
        self.assertEqual(result[0]['browser'], 'mac')
        self.assertTrue(result[0]['ok'])
        return self.access.grants[row['id']]

    def client(self, grant):
        capture = Capture()
        client = signed.Client(self.access, capture, grant)
        self.access.clients.add(client)
        client.open()
        self.addCleanup(client.cleanup)
        return client, capture


CONTEXT_CLIENT = r'''
const assert = require('node:assert/strict');
const {chromium} = require('playwright-core');
(async () => {
  const connections = [];
  const connect = async () => {
    const connection = await chromium.connectOverCDP(process.env.E2E_CDP_URL, {timeout:10000});
    connections.push(connection);
    assert.equal(connection.contexts()[0].pages().length, 1);
    return connection;
  };
  const exercise = async (connection, label) => {
    const page = connection.contexts()[0].pages()[0];
    page.setDefaultTimeout(5000);
    assert.equal(await page.evaluate(() => document.body.tagName), 'BODY');
    assert.match(await page.locator('h1').textContent(), /^Mac context/);
    await page.goto(process.env.E2E_PAGE_URL + label, {timeout:10000});
    assert.equal(await page.evaluate(() => document.querySelector('h1').textContent), 'Mac context /' + label);
    assert.equal(await page.locator('h1').textContent(), 'Mac context /' + label);
    const png = await page.screenshot({timeout:5000});
    assert.equal(png.subarray(1,4).toString(), 'PNG');
  };
  try {
    if (process.env.E2E_MODE === 'concurrent') {
      const [first, second] = await Promise.all([connect(), connect()]);
      await exercise(first, 'first');
      await exercise(second, 'second');
      await first.close();
      await exercise(second, 'survivor');
    } else {
      await exercise(await connect(), 'first');
      await connections[0].close();
      await exercise(await connect(), 'second');
    }
    console.log(JSON.stringify({ok:true, mode:process.env.E2E_MODE}));
  } finally {
    for (const connection of connections) await connection.close();
  }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
'''


@unittest.skipUnless(browser.find_chrome(), 'Headless Chromium is required')
class RealContextTests(GrantHarness, unittest.TestCase):
    transport_type = ChromiumTransport

    def command(self, client, capture, method, params=None, session=None):
        message = {'id': 1, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        client.handle(message)
        reply = capture.messages[-1]
        self.assertNotIn('error', reply)
        return reply['result']

    def attach(self, grant):
        client, capture = self.client(grant)
        session = self.command(client, capture, 'Target.attachToTarget',
                               {'targetId': grant.row['tab'], 'flatten': True})['sessionId']
        return client, capture, session

    def test_runtime_already_enabled_second_session_gets_main_world(self):
        grant = self.grant()
        first, out, session = self.attach(grant)
        self.command(first, out, 'Runtime.enable', session=session)
        self.assertTrue(any(event.get('method') == 'Runtime.executionContextCreated' for event in out.messages))
        second, out, session = self.attach(grant)
        self.command(second, out, 'Runtime.enable', session=session)
        contexts = [event['params']['context'] for event in out.messages
                    if event.get('method') == 'Runtime.executionContextCreated']
        self.assertTrue(contexts, 'Second synthetic session never receives a main-world execution context')
        main = next(context for context in contexts if context.get('auxData', {}).get('isDefault'))
        result = self.command(second, out, 'Runtime.evaluate',
                              {'expression': 'document.body.tagName', 'contextId': main['id']}, session)
        self.assertEqual(result['result']['value'], 'BODY')

    def serve_page(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Page(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                data = ('<title>Mac context test</title><h1>Mac context ' + self.path + '</h1>').encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name='mac-context-test-http')
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        return f'http://127.0.0.1:{server.server_port}/'

    def playwright(self, mode):
        url = self.serve_page()
        signed.Access._listen(self.access)
        grant = self.grant()
        panel = self.broker.browser.tab(grant.row['tab']).session
        self.broker.browser.conn.call('Runtime.enable', session=panel)
        self.broker.browser.conn.call('Page.navigate', {'url': url}, panel)
        wait_for(lambda: self.transport.conn.call('Runtime.evaluate',
                 {'expression': 'document.readyState'}, self.transport.session)['result'].get('value') == 'complete')
        run = subprocess.run(['timeout', '45', 'node', '-e', CONTEXT_CLIENT],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=50,
                             env=dict(os.environ, NODE_PATH=playwright_modules(), E2E_CDP_URL=self.access.url(grant),
                                      E2E_PAGE_URL=url, E2E_MODE=mode))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout), {'ok': True, 'mode': mode})

    @unittest.skipUnless(shutil.which('node') and playwright_modules(), 'Cached playwright-core is required')
    def test_two_sequential_playwright_clients(self):
        self.playwright('sequential')

    @unittest.skipUnless(shutil.which('node') and playwright_modules(), 'Cached playwright-core is required')
    def test_two_concurrent_playwright_clients_and_disconnect(self):
        self.playwright('concurrent')

    def test_navigation_replays_only_live_contexts(self):
        url = self.serve_page()
        grant = self.grant()
        first, out, session = self.attach(grant)
        self.command(first, out, 'Runtime.enable', session=session)
        original = {event['params']['context']['uniqueId'] for event in out.messages
                    if event.get('method') == 'Runtime.executionContextCreated'}
        # Disconnect the only subscriber, then navigate with no Runtime subscribers.
        first.cleanup()
        panel = self.broker.browser.tab(grant.row['tab']).session
        self.broker.browser.conn.call('Page.navigate', {'url': url}, panel)
        wait_for(lambda: any(method == 'Runtime.executionContextsCleared' for method, _ in self.transport.events))
        wait_for(lambda: self.transport.conn.call('Runtime.evaluate',
                 {'expression': 'document.readyState'}, self.transport.session)['result'].get('value') == 'complete')
        second, out, session = self.attach(grant)
        self.command(second, out, 'Runtime.enable', session=session)
        contexts = [event['params']['context'] for event in out.messages
                    if event.get('method') == 'Runtime.executionContextCreated']
        self.assertTrue(contexts)
        self.assertTrue(original.isdisjoint(context['uniqueId'] for context in contexts))
        main = next(context for context in contexts if context.get('auxData', {}).get('isDefault'))
        result = self.command(second, out, 'Runtime.evaluate',
                              {'expression': 'document.querySelector("h1").textContent', 'contextId': main['id']}, session)
        self.assertEqual(result['result']['value'], 'Mac context /')

    def test_removed_iframe_context_not_replayed(self):
        grant = self.grant()
        first, out, session = self.attach(grant)
        self.command(first, out, 'Runtime.enable', session=session)
        self.command(first, out, 'Runtime.evaluate', {'expression': '''new Promise(resolve => {
          const frame = document.createElement('iframe'); frame.id = 'child';
          frame.onload = () => resolve(true); document.body.append(frame);
        })''', 'awaitPromise': True}, session)
        contexts = [event['params']['context'] for event in out.messages
                    if event.get('method') == 'Runtime.executionContextCreated']
        child = next(context for context in contexts if context['auxData'].get('frameId') != self.transport.target)
        self.command(first, out, 'Runtime.evaluate', {'expression': 'document.getElementById("child").remove()'}, session)
        wait_for(lambda: any(method == 'Runtime.executionContextDestroyed'
                             and params.get('executionContextId') == child['id']
                             for method, params in self.transport.events))
        second, out, session = self.attach(grant)
        self.command(second, out, 'Runtime.enable', session=session)
        replayed = [event['params']['context']['uniqueId'] for event in out.messages
                    if event.get('method') == 'Runtime.executionContextCreated']
        self.assertTrue(replayed)
        self.assertNotIn(child['uniqueId'], replayed)


class GrantTests(GrantHarness, unittest.TestCase):
    def test_context_subscription_replay_disable_detach_and_removal(self):
        grant = self.grant()
        conn = self.broker.browser.conn
        target = grant.row['tab']
        first = conn.call('Target.attachToTarget', {'targetId': target})['sessionId']
        second = conn.call('Target.attachToTarget', {'targetId': target})['sessionId']
        a, b = [], []
        conn.register_sink(first, a.append)
        conn.register_sink(second, b.append)
        conn.call('Runtime.enable', session=first)
        def emit(method, params, ident=None):
            self.device.handle({'type': 'event', 'epoch': self.device.epoch, 'grant': ident or grant.row['id'],
                                'tab': 7, 'method': method, 'params': params})
        emit('Runtime.executionContextCreated', {'context': {'id': 1, 'uniqueId': 'main'}})
        emit('Runtime.executionContextCreated', {'context': {'id': 2, 'uniqueId': 'child'}})
        self.assertEqual(b, [])  # An attached but disabled session receives no Runtime events.
        emit('Runtime.executionContextDestroyed', {'executionContextUniqueId': 'child'})
        emit('Runtime.executionContextCreated', {'context': {'id': 3, 'uniqueId': 'old'}}, 'stale-grant')
        before = len(a)
        conn.call('Runtime.enable', session=second)
        self.assertEqual(len(a), before)
        self.assertEqual([row['params']['context']['id'] for row in b], [1])
        conn.call('Runtime.enable', session=second)
        self.assertEqual(len(b), 1)  # Same-session enable is idempotent, as in Chrome.
        conn.call('Runtime.disable', session=first)
        emit('Runtime.executionContextsCleared', {})
        self.assertEqual(len(a), before)
        self.assertEqual(b[-1]['method'], 'Runtime.executionContextsCleared')
        conn.call('Runtime.enable', session=first)
        self.assertEqual(len(a), before)  # The cleared context must not return.
        methods = [row.get('method') for row in self.transport.sent]
        self.assertEqual(methods.count('Runtime.enable'), 1)
        self.assertNotIn('Runtime.disable', methods)
        conn.send('Target.detachFromTarget', {'sessionId': first})
        self.assertNotIn(first, conn.sessions)
        self.assertNotIn(first, conn.enabled)
        self.assertNotIn(first, conn.sinks)
        with patch.object(self.broker.browser, '_event', side_effect=lambda *args: self.access.end(grant.row['id'])):
            emit('Runtime.executionContextCreated', {'context': {'id': 4}})
        self.assertEqual(conn.contexts, {})
        self.assertEqual(conn.enabled, {})
        self.assertEqual(conn.runtime_started, set())

    def test_shared_domains_disable_only_when_last_subscriber_disables(self):
        grant = self.grant()
        conn = self.broker.browser.conn
        sessions = [conn.call('Target.attachToTarget', {'targetId': grant.row['tab']})['sessionId'] for _ in range(2)]
        for domain in sorted(mac.SHARED_DOMAINS - {'Runtime'}):
            with self.subTest(domain=domain):
                for session in sessions:
                    conn.call(domain + '.enable', session=session)
                before = len(self.transport.sent)
                conn.call(domain + '.disable', session=sessions[0])
                self.assertEqual(len(self.transport.sent), before)
                conn.call(domain + '.disable', session=sessions[1])
                self.assertEqual(self.transport.sent[-1]['method'], domain + '.disable')
                self.assertEqual(len(self.transport.sent), before + 1)
        for session in sessions:
            conn.call('Page.setLifecycleEventsEnabled', {'enabled': True}, session)
        before = len(self.transport.sent)
        conn.call('Page.setLifecycleEventsEnabled', {'enabled': False}, sessions[0])
        self.assertEqual(len(self.transport.sent), before)
        conn.call('Page.setLifecycleEventsEnabled', {'enabled': False}, sessions[1])
        self.assertEqual(len(self.transport.sent), before + 1)

    def test_discovery_policy_identity_takeover_and_end(self):
        grant = self.grant()
        client, output = self.client(grant)
        def call(method, params=None, session=None):
            message = {'id': len(output.messages) + 1, 'method': method, 'params': params or {}}
            if session:
                message['sessionId'] = session
            client.handle(message)
            return output.messages[-1]
        result = call('Target.getTargets')['result']['targetInfos']
        self.assertEqual([row['targetId'] for row in result], [grant.row['tab']])
        session = call('Target.attachToTarget', {'targetId': grant.row['tab'], 'flatten': True})['result']['sessionId']
        before = len(self.transport.sent)
        for method in ('Network.getCookies', 'Network.getAllCookies', 'Storage.getCookies', 'DOM.setFileInputFiles',
                       'Browser.close', 'Target.createBrowserContext', 'Target.createTarget'):
            self.assertIn('error', call(method, session=session if not method.startswith(('Browser.', 'Target.')) else None), method)
        self.assertIn('error', call('Runtime.evaluate', {'expression': '1'}, 'wrong-session'))
        self.assertIn('error', call('Target.attachToTarget', {'targetId': 'other', 'flatten': True}))
        self.assertIn('error', call('Page.navigate', {'url': 'file:///etc/passwd'}, session))
        self.assertEqual(len(self.transport.sent), before)
        self.assertIn('result', call('Runtime.evaluate', {'expression': '1+1'}, session))
        tab = self.broker.browser.tab(grant.row['tab'])
        tab.controller = 'user'
        self.assertIn('error', call('Runtime.evaluate', {'expression': '1+1'}, session))
        self.assertIn('error', call('Input.insertText', {'text': 'no'}, session))
        tab.controller = 'agent'
        self.access.end(grant.row['id'])
        self.assertFalse(grant.active)
        self.assertTrue(client.closed)
        self.assertEqual(self.broker.browser.tabs, {})
        self.assertEqual(self.access.state()['grants'], [])
        self.assertEqual(self.transport.sent[-1]['type'], 'end')

    def test_disconnect_revoke_closes_clients_and_reconnect_never_restores(self):
        grant = self.grant()
        client, _ = self.client(grant)
        self.broker.revoke(self.device.ident)
        self.assertFalse(self.access.valid(grant))
        self.assertTrue(client.closed)
        wait_for(lambda: not self.broker.browser.tabs)
        self.assertFalse(self.broker.connected())
        with self.assertRaises(ValueError):
            self.broker.register(self.device.ident, FakeTransport(), '')

    def test_frames_ack_on_receipt_newest_only_and_isolation(self):
        grant = self.grant()
        tab = self.broker.browser.tab(grant.row['tab'])
        for i in range(3):
            self.device.handle({'type': 'event', 'epoch': self.device.epoch, 'grant': grant.row['id'], 'tab': 7,
                                'method': 'Page.screencastFrame', 'params': {'sessionId': 1, 'data': str(i),
                                'metadata': {'deviceWidth': 1280, 'deviceHeight': 800}}})
        self.assertEqual(tab.frame[2], '2')
        self.assertEqual(tab.seq, 3)
        acks = [m for m in self.transport.sent if m.get('method') == 'Page.screencastFrameAck']
        self.assertEqual(len(acks), 3)
        self.device.handle({'type': 'event', 'epoch': self.device.epoch, 'grant': 'old', 'tab': 7,
                            'method': 'Page.screencastFrame', 'params': {'sessionId': 1, 'data': 'wrong'}})
        self.assertEqual(tab.seq, 3)
        self.assertIn('"browser": "mac"', browser.frame_event(tab.frame, 'mac').decode())

    def test_auto_routing_is_pinned_and_explicit_mac_fails_offline(self):
        server = Mock()
        routed = mac.RoutedAccess(server, self.access)
        with patch.object(self.access, 'ask', return_value={'ok': True}) as ask:
            routed.ask('Task', '%12')
            ask.assert_called_once()
            routed.ask('Task', '%12', browser='server')
            server.ask.assert_called_once()
        self.device.close()
        routed.ask('Task', '%12')
        self.assertEqual(server.ask.call_count, 2)
        with self.assertRaises(ValueError):
            routed.ask('Task', '%12', browser='mac')
        with self.assertRaises(ValueError):
            routed.ask('Task', '%12', browser='bad')

    def test_bad_epoch_and_routing_fields(self):
        with self.assertRaises(ValueError):
            self.device.handle({'type': 'ping', 'epoch': 'old'})
        for method in ('Network.getCookies', 'Network.getAllCookies', 'Storage.getCookies', 'DOM.setFileInputFiles',
                       'Browser.getVersion', 'Target.setAutoAttach'):
            with self.assertRaises(browser.BrowserError):
                mac.check_command(method, {})
        for params in ({'sessionId': 'other'}, {'nested': [{'targetId': 'other'}]}, {'browserContextId': 'other'}):
            with self.assertRaises(browser.BrowserError):
                mac.check_command('Runtime.evaluate', params)
        mac.check_command('Page.screencastFrameAck', {'sessionId': 1})

    def test_pending_request_stays_bound_to_its_original_device_connection(self):
        result = []
        thread = threading.Thread(target=lambda: result.append(self.access.ask('Pinned task', '%12', 1)))
        self.waiters.append(thread)
        thread.start()
        wait_for(lambda: self.access.pending)
        ident = next(iter(self.access.pending))
        self.assertIs(self.access.pending[ident]['binding'], self.device)
        pair = self.broker.exchange(self.broker.pair()['code'], ORIGIN, 'Second Chrome')
        transport = FakeTransport()
        other = self.broker.register(pair['id'], transport, 'Test Chrome')
        transport.device = other
        self.assertIs(self.broker.connected(), other)
        self.access.allow(ident)
        thread.join(timeout=1)
        self.assertIs(self.access.grants[ident].device, self.device)
        self.assertFalse(transport.sent)

    def test_end_during_inflight_command_wakes_waiter_without_disconnect(self):
        grant = self.grant()
        sent = threading.Event()
        errors = []
        original = self.transport.message
        def hold(message):
            if message['type'] == 'command':
                self.transport.sent.append(message)
                sent.set()
            else:
                original(message)
        self.transport.message = hold
        def command():
            try:
                with self.broker.browser.lock:
                    self.broker.browser.conn.call('Runtime.evaluate', {'expression': '1'},
                                                  self.broker.browser.tab(grant.row['tab']).session)
            except browser.BrowserError as exc:
                errors.append(str(exc))
        waiter = threading.Thread(target=command)
        self.waiters.append(waiter)
        waiter.start()
        self.assertTrue(sent.wait(1))
        self.access.end(grant.row['id'])
        waiter.join(timeout=1)
        self.assertFalse(waiter.is_alive())
        self.assertTrue(errors)
        self.assertFalse(self.device.closed)

    def test_cross_browser_pending_replacement_and_global_grant_limit(self):
        server = signed.Access(browser.Browser(signed=True), request_seconds=3)
        self.addCleanup(server.stop)
        routed = mac.RoutedAccess(server, self.access)
        results = []
        first = threading.Thread(target=lambda: results.append(routed.ask('Mac task', '%12', browser='mac')))
        self.waiters.append(first)
        first.start()
        wait_for(lambda: self.access.pending)
        second = threading.Thread(target=lambda: results.append(routed.ask('Server task', '%12', browser='server')))
        self.waiters.append(second)
        second.start()
        wait_for(lambda: server.pending)
        first.join(timeout=1)
        self.assertFalse(first.is_alive())
        self.assertEqual(results, [{'ok': False, 'reason': 'denied'}])
        self.assertFalse(self.access.pending)
        server.grants = {'one': Mock(), 'two': Mock()}
        with self.assertRaisesRegex(ValueError, 'limit is 2'):
            routed.allow(next(iter(server.pending)))
        server.grants = {}
        with self.assertRaises(ValueError):
            routed.allow([])
        routed.deny(next(iter(server.pending)))
        second.join(timeout=1)
        self.assertFalse(second.is_alive())

    def test_js_python_policy_match_and_extension_identity_checks(self):
        script = '''import {ALLOWED,command} from './extension/policy.mjs';
const g={id:'g',tab:7,epoch:'e',expires:Date.now()+60000};
const c={type:'command',id:'c',grant:'g',tab:7,epoch:'e',method:'Runtime.evaluate',params:{expression:'1'}};
command(c,g,'e');
for(const d of [{tab:8},{epoch:'old'},{grant:'old'},{sessionId:'other'},{method:'Network.getCookies'},
 {method:'DOM.setFileInputFiles'},{params:{nested:[{targetId:'other'}]}}]) {
 let refused=false; try{command({...c,...d},g,'e')}catch{refused=true} if(!refused)throw Error(JSON.stringify(d));
}
console.log(JSON.stringify([...ALLOWED].sort()));'''
        run = subprocess.run(['node', '--input-type=module', '-e', script], cwd=Path(__file__).resolve().parents[1],
                             capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), sorted(mac.PAGE_ALLOWED))


if __name__ == '__main__':
    unittest.main()


class WebSocketAuthTests(unittest.TestCase):
    def setUp(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.temp = tempfile.TemporaryDirectory(prefix='p20-ws-')
        self.addCleanup(self.temp.cleanup)
        self.broker = mac.Broker(Path(self.temp.name) / 'devices.json')
        self.pair = self.broker.exchange(self.broker.pair()['code'], ORIGIN, 'Test Chrome')
        broker = self.broker
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):
                pass
            def respond(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                mac.upgrade(self, broker)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.broker.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def websocket(self, token, origin=ORIGIN):
        import socket
        from test_browser_cdp import masked
        sock = socket.create_connection(('127.0.0.1', self.httpd.server_port), timeout=2)
        self.addCleanup(sock.close)
        sock.sendall((f'GET {mac.PREFIX}connect HTTP/1.1\r\nHost: 127.0.0.1:{self.httpd.server_port}\r\n'
                      'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                      'Sec-WebSocket-Key: YWJjZGVmZ2hpamtsbW5vcA==\r\n' + f'Origin: {origin}\r\n\r\n').encode())
        stream = sock.makefile('rb')
        self.addCleanup(stream.close)
        status = stream.readline()
        while stream.readline() != b'\r\n':
            pass
        if b'101' not in status:
            return status, None, sock
        sock.sendall(masked(1, json.dumps({'type': 'hello', 'token': token, 'chrome': 'Test Chrome'}).encode()))
        head = stream.read(2)
        if not head:
            return status, None, sock
        size = head[1] & 0x7f
        if size == 126:
            size = int.from_bytes(stream.read(2), 'big')
        message = json.loads(stream.read(size))
        return status, message, sock

    def test_bad_origin_bad_token_ready_and_revoke(self):
        status, _, _ = self.websocket(self.pair['token'], 'https://evil.test')
        self.assertIn(b'403', status)
        _, result, _ = self.websocket('x' * 43)
        self.assertIsNone(result)
        self.assertIsNone(self.broker.connected())
        _, result, _ = self.websocket(self.pair['token'])
        self.assertEqual(result['type'], 'ready')
        self.assertTrue(self.broker.connected())
        self.broker.revoke(self.pair['id'])
        self.assertIsNone(self.broker.connected())
        _, result, _ = self.websocket(self.pair['token'])
        self.assertIsNone(result)
