"""Agent CDP contract tests. Terminal writes and browser transport use isolated fakes."""
import io
import errno
import json
from email.message import Message
from pathlib import Path
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import browser
import browser_api
import browser_cdp as cdp
from test_browser import FakeChrome, FakeHandler


class Chrome(FakeChrome):
    def __init__(self):
        self.context_number = self.browser_number = 0
        self.infos = {'T0': {'targetId': 'T0', 'type': 'page', 'url': 'about:blank'}}
        super().__init__()

    def answer(self, message):
        method, params = message['method'], message.get('params', {})
        if method == 'Browser.getVersion':
            return {'product': 'Chrome/147.0.1.2', 'userAgent': 'HeadlessChrome/147.0.1.2'}
        if method == 'Target.attachToBrowserTarget':
            self.browser_number += 1
            return {'sessionId': 'B%d' % self.browser_number}
        if method == 'Target.createBrowserContext':
            self.context_number += 1
            return {'browserContextId': 'C%d' % self.context_number}
        if method == 'Target.getBrowserContexts':
            return {'browserContextIds': ['C%d' % n for n in range(1, self.context_number + 1)]}
        if method == 'Target.createTarget':
            result = super().answer(message)
            info = {'targetId': result['targetId'], 'type': 'page', 'browserContextId': params.get('browserContextId'),
                    'url': params.get('url', 'about:blank'), 'title': ''}
            self.infos[info['targetId']] = info
            self.emit({'method': 'Target.targetCreated', 'params': {'targetInfo': info}})
            return result
        if method == 'Target.getTargets':
            return {'targetInfos': list(self.infos.values())}
        if method == 'Target.getTargetInfo':
            return {'targetInfo': self.infos.get(params.get('targetId'), {'targetId': 'browser', 'type': 'browser'})}
        if method == 'Page.getLayoutMetrics':
            return {'cssVisualViewport': {'clientWidth': 777, 'clientHeight': 555}}
        return super().answer(message)


class Capture:
    def __init__(self):
        self.messages, self.closes = [], []
        self.changed = threading.Condition()

    def message(self, message):
        with self.changed:
            self.messages.append(message)
            self.changed.notify_all()

    def close(self, code=1000):
        self.closes.append(code)

    def reply(self, ident):
        with self.changed:
            deadline = time.monotonic() + 3
            while True:
                for message in self.messages:
                    if message.get('id') == ident:
                        return message
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError('No reply to %d: %r' % (ident, self.messages))
                self.changed.wait(left)


def masked(opcode, payload=b'', final=True):
    key = b'abcd'
    data = cdp.frame(opcode, payload)
    if not final:
        data = bytes([data[0] & 0x7f]) + data[1:]
    header_size = 2 if len(payload) < 126 else (4 if len(payload) < 65536 else 10)
    header = bytes([data[0], data[1] | 0x80]) + data[2:header_size]
    return header + key + bytes(value ^ key[index % 4] for index, value in enumerate(payload))


class FramingTests(unittest.TestCase):
    def read(self, data, limit=cdp.MAX_MESSAGE):
        controls = []
        messages = list(cdp.websocket_messages(io.BytesIO(data), lambda *args: controls.append(args), limit))
        return messages, controls

    def test_masked_text_fragments_ping_pong_close(self):
        data = (masked(1, b'{"x":', False) + masked(9, b'ping') + masked(10, b'pong')
                + masked(0, b'1}') + masked(8, struct.pack('!H', 1000)))
        messages, controls = self.read(data)
        self.assertEqual(messages, ['{"x":1}'])
        self.assertEqual(controls, [(10, b'ping'), (8, struct.pack('!H', 1000))])

    def test_extended_lengths_and_unlimited_server_frames(self):
        for size in (126, 65536, cdp.MAX_MESSAGE + 1):
            payload = b'a' * size
            output = cdp.frame(1, payload)
            self.assertEqual(output[-size:], payload)
            if size <= 65536:
                self.assertEqual(self.read(masked(1, payload) + masked(8))[0], [payload.decode()])

    def test_size_limit_checks_header_and_combined_fragments(self):
        for data in (masked(1, b'12345'), masked(1, b'123', False) + masked(0, b'45')):
            with self.assertRaises(cdp.WebSocketError) as error:
                self.read(data, limit=4)
            self.assertEqual(error.exception.code, 1009)
        # No payload allocation/read when a 32 MiB header exceeds the cap.
        with self.assertRaises(cdp.WebSocketError) as error:
            self.read(b'\x81\xff' + struct.pack('!Q', cdp.MAX_MESSAGE + 1))
        self.assertEqual(error.exception.code, 1009)

    def test_bad_mask_opcode_fragment_control_utf8_and_close(self):
        cases = [(cdp.frame(1, b'x'), 1002), (masked(0, b'x'), 1002),
                 (masked(1, b'x', False) + masked(1, b'y'), 1002), (masked(9, b'x', False), 1002),
                 (masked(8, b'x'), 1002), (masked(8, struct.pack('!H', 1005)), 1002),
                 (masked(1, b'\xff'), 1007), (masked(2, b'x'), 1003)]
        for data, code in cases:
            with self.subTest(code=code, data=data):
                with self.assertRaises(cdp.WebSocketError) as error:
                    self.read(data)
                self.assertEqual(error.exception.code, code)

    def test_reader_replies_and_sink_registration_do_not_wait_on_writer_lock(self):
        connection = object.__new__(browser.Connection)
        connection.lock = threading.Lock()
        connection.waiting = {7: Mock()}
        connection.sinks = {}
        connection.on_event = Mock()
        callback = connection.waiting[7]
        with connection.lock:
            connection._dispatch(b'{"id":7,"result":{}}')
            connection.register_sink('child', Mock())
            connection._dispatch(b'{"method":"Runtime.ready","sessionId":"child"}')
            connection.unregister_sink('child')
        callback.assert_called_once_with({'id': 7, 'result': {}})


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.fake = Chrome()
        self.browser = browser.Browser(launcher=self.fake.launcher, chrome_finder=lambda: '/fake/chrome')
        self.env, self.notify = Mock(), Mock()
        self.proxy = cdp.Proxy(self.browser, Path(self.folder.name) / 'config/agent-browser.json',
                               notifier=self.notify, env_runner=self.env)
        self.client = self.connect('%12')
        self.ident = 0

    def tearDown(self):
        for client in list(self.proxy.clients):
            client.cleanup()
        self.fake.close()
        # Synchronize with the shared reader before temporary files vanish.
        self.browser.conn.reader.join(timeout=2) if self.browser.conn else None
        self.folder.cleanup()

    def connect(self, pane):
        client = cdp.AgentClient(self.proxy, Capture(), pane)
        self.proxy.clients.add(client)
        client.open()
        return client

    def request(self, method, params=None, session=None, client=None):
        self.ident += 1
        client = client or self.client
        message = {'id': self.ident, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        client.handle(message)
        return client.transport.reply(self.ident)

    def test_global_ids_and_root_child_remapping(self):
        second = self.connect(None)
        self.client.handle({'id': 900, 'method': 'Browser.getVersion'})
        second.handle({'id': 900, 'method': 'Browser.getVersion'})
        self.assertEqual(self.client.transport.reply(900)['id'], 900)
        self.assertEqual(second.transport.reply(900)['id'], 900)
        commands = self.fake.sent('Browser.getVersion')[-2:]
        self.assertNotEqual(commands[0]['id'], commands[1]['id'])
        self.assertEqual({cmd['sessionId'] for cmd in commands}, {self.client.root, second.root})
        self.assertNotIn('sessionId', self.client.transport.reply(900))
        self.client.sessions['child'] = 'owned'
        self.browser.conn.register_sink('child', self.client.event)
        reply = self.request('Runtime.evaluate', {'expression': '1'}, 'child')
        self.assertEqual(reply['sessionId'], 'child')
        self.assertIn('error', self.request('Runtime.evaluate', session='foreign'))

    def test_targets_contexts_default_rewrite_and_user_startup_hidden(self):
        result = self.request('Target.createTarget', {'url': 'about:blank'})['result']
        target = result['targetId']
        self.assertEqual(self.fake.sent('Target.createTarget')[-1]['params']['browserContextId'], self.client.default)
        targets = self.request('Target.getTargets')['result']['targetInfos']
        self.assertEqual([info['targetId'] for info in targets], [target])
        self.assertEqual(self.request('Target.getBrowserContexts')['result'], {'browserContextIds': []})
        context = self.request('Target.createBrowserContext')['result']['browserContextId']
        self.assertEqual(self.request('Target.getBrowserContexts')['result'], {'browserContextIds': [context]})
        for method in cdp.SCOPED:
            self.assertNotIn('error', self.request(method))
            self.assertEqual(self.fake.sent(method)[-1]['params']['browserContextId'], self.client.default)
            self.assertIn('error', self.request(method, {'browserContextId': 'foreign'}))
        for method in cdp.TARGET_COMMANDS:
            self.assertIn('error', self.request(method, {'targetId': 'T0'}))
        self.assertEqual(self.request('Target.getTargetInfo')['result']['targetInfo']['type'], 'browser')
        self.assertIn('error', self.request('Target.disposeBrowserContext', {'browserContextId': 'foreign'}))
        self.assertIn('error', self.request('Target.createTarget', {'browserContextId': 'foreign'}))

    def test_foreign_attachment_is_resumed_detached_and_not_forwarded(self):
        before = len(self.client.transport.messages)
        event = {'method': 'Target.attachedToTarget', 'sessionId': self.client.root,
                 'params': {'sessionId': 'foreign-child', 'waitingForDebugger': True,
                            'targetInfo': {'targetId': 'T0', 'browserContextId': 'foreign'}}}
        self.browser.conn._dispatch(json.dumps(event).encode())
        deadline = time.monotonic() + 2
        while not self.fake.sent('Target.detachFromTarget') and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(self.client.transport.messages), before)
        detach = self.fake.sent('Target.detachFromTarget')[-1]
        self.assertEqual(detach['params'], {'sessionId': 'foreign-child'})
        self.assertEqual(detach['sessionId'], self.client.root)
        self.assertTrue(self.fake.sent('Runtime.runIfWaitingForDebugger'))
        self.assertNotIn('foreign-child', self.client.sessions)

    def test_owned_nested_sessions_events_and_foreign_lifecycle_filter(self):
        info = {'targetId': 'owned', 'browserContextId': self.client.default, 'type': 'worker'}
        self.client.event({'method': 'Target.attachedToTarget', 'sessionId': self.client.root,
                           'params': {'sessionId': 'child', 'targetInfo': info}})
        self.assertNotIn('sessionId', self.client.transport.messages[-1])
        info2 = dict(info, targetId='worker2')
        self.browser.conn._dispatch(json.dumps({'method': 'Target.attachedToTarget', 'sessionId': 'child',
                                              'params': {'sessionId': 'nested', 'targetInfo': info2}}).encode())
        self.assertEqual(self.client.transport.messages[-1]['sessionId'], 'child')
        self.assertIn('nested', self.client.sessions)
        before = len(self.client.transport.messages)
        for method, params in [('Target.targetCreated', {'targetInfo': {'targetId': 'foreign', 'browserContextId': 'foreign'}}),
                               ('Target.targetInfoChanged', {'targetInfo': {'targetId': 'foreign', 'browserContextId': 'foreign'}}),
                               ('Target.targetDestroyed', {'targetId': 'T0'}),
                               ('Target.detachedFromTarget', {'sessionId': 'foreign'})]:
            self.client.event({'method': method, 'params': params, 'sessionId': self.client.root})
        self.assertEqual(len(self.client.transport.messages), before)
        self.client.event({'method': 'Target.detachedFromTarget', 'sessionId': self.client.root,
                           'params': {'sessionId': 'child'}})
        self.assertNotIn('child', self.browser.conn.sinks)

    def test_tab_limit_and_popup_closure(self):
        for _ in range(3):
            self.assertNotIn('error', self.request('Target.createTarget'))
        count = len(self.fake.sent('Target.createTarget'))
        self.assertEqual(self.request('Target.createTarget')['error']['message'], cdp.TAB_ERROR)
        self.assertEqual(len(self.fake.sent('Target.createTarget')), count)
        popup = {'targetId': 'popup', 'type': 'page', 'browserContextId': self.client.default, 'openerId': 'T2'}
        self.client.target_event('Target.targetCreated', {'targetInfo': popup})
        deadline = time.monotonic() + 2
        while not self.fake.sent('Target.closeTarget') and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.fake.sent('Target.closeTarget')[-1]['params'], {'targetId': 'popup'})
        self.assertNotIn('popup', self.client.targets)
        target = next(iter(self.client.targets))
        self.client.target_event('Target.targetDestroyed', {'targetId': target})
        self.assertNotIn('error', self.request('Target.createTarget'))

    def test_close_crash_swallowed_and_unknown_logged_once(self):
        for method in cdp.SWALLOWED:
            self.assertEqual(self.request(method)['result'], {})
            self.assertFalse(self.fake.sent(method))
        with self.assertLogs(cdp.LOG, level='WARNING') as logs:
            for _ in range(2):
                reply = self.request('Browser.executeBrowserCommand')
                self.assertEqual(reply['error'], {'code': -32601, 'message': 'Not allowed through Tether.'})
        self.assertEqual(len(logs.output), 1)
        self.assertIn('error', self.request('Target.attachToBrowserTarget'))

    def test_cleanup_disposes_all_contexts_and_detaches_root(self):
        context = self.request('Target.createBrowserContext')['result']['browserContextId']
        self.client.cleanup()
        disposed = {cmd['params']['browserContextId'] for cmd in self.fake.sent('Target.disposeBrowserContext')}
        self.assertEqual(disposed, {self.client.default, context})
        self.assertEqual(self.fake.sent('Target.detachFromTarget')[-1]['params'], {'sessionId': self.client.root})
        self.assertFalse(self.proxy.busy())
        self.assertNotIn(self.client.root, self.browser.conn.sinks)

    def test_browser_stop_closes_clients_with_1011_and_idle_watcher_sees_busy(self):
        self.proxy.browser_stopped()
        self.assertEqual(self.client.transport.closes, [1011])
        self.assertTrue(self.proxy.busy())
        self.browser.used = -browser.IDLE_SECONDS * 2
        with patch.object(browser.time, 'sleep', side_effect=[None, RuntimeError('end watch')]), \
             patch.object(self.browser, 'stop') as stop:
            with self.assertRaisesRegex(RuntimeError, 'end watch'):
                self.browser._watch()
            stop.assert_not_called()
        self.assertGreater(self.browser.used, 0)

    def test_panel_adoption_size_no_emulation_and_panel_close_keeps_context(self):
        target = self.request('Target.createTarget')['result']['targetId']
        deadline = time.monotonic() + 2
        while target not in self.browser.tabs and time.monotonic() < deadline:
            time.sleep(.01)
        with self.browser.lock:
            tab = self.browser.tabs[target]
            row = tab.row()
            self.assertEqual((row['mode'], row['owner'], row['width'], row['height']), ('agent', {'pane': '%12'}, 777, 555))
        self.assertFalse([cmd for cmd in self.fake.sent('Emulation.setDeviceMetricsOverride') if cmd.get('sessionId') == tab.session])
        self.browser._apply_preset(tab, 'pixel')
        self.assertTrue(self.browser.heal(tab, (1, {'deviceWidth': 321, 'deviceHeight': 456}, '', 4)))
        self.assertEqual(tab.size, (321, 456))
        handler = FakeHandler('/mobile/api/browser/device', {'tab': target, 'preset': 'desktop'})
        browser_api.post(handler, self.browser, self.proxy)
        self.assertEqual(handler.replies, [(400, {'error': "The agent controls this tab's size."})])
        self.browser.close_tab(target)
        self.assertFalse(self.fake.sent('Target.disposeBrowserContext'))

    def test_agent_tabs_do_not_use_user_tab_limit(self):
        self.request('Target.createTarget')
        with patch.object(browser, 'MAX_TABS', 2):
            self.assertEqual(self.browser.new_tab()['mode'], 'private')
            with self.assertRaises(browser.BrowserError):
                self.browser.new_tab()

    def test_unknown_pane_agent_owner_and_screencast_uses_real_viewport(self):
        second = self.connect(None)
        target = self.request('Target.createTarget', client=second)['result']['targetId']
        deadline = time.monotonic() + 2
        while target not in self.browser.tabs and time.monotonic() < deadline:
            time.sleep(.01)
        with self.browser.lock:
            tab = self.browser.tabs[target]
            self.assertEqual(tab.row()['owner'], {'pane': None})
        self.browser.watch(target, 400, 300)
        self.assertEqual(tab.size, (777, 555))
        self.assertTrue(self.browser.heal(tab, (1, {'deviceWidth': 777, 'deviceHeight': 555}, '', 2)))
        self.browser.unwatch(tab)

    def test_start_fallback_port_and_shutdown_hooks_without_real_sockets_or_tmux(self):
        port = self.proxy.config['port']
        httpd = Mock(server_port=port + 1)
        saved_handler = []
        previous = Mock()
        with patch.object(cdp, 'ThreadingHTTPServer', side_effect=[OSError(errno.EADDRINUSE, 'occupied'), httpd]) as factory, \
             patch.object(cdp.atexit, 'register') as register, \
             patch.object(cdp.signal, 'getsignal', return_value=previous), \
             patch.object(cdp.signal, 'signal', side_effect=lambda sig, handler: saved_handler.append(handler)):
            self.proxy.start(shutdown_hooks=True)
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(cdp.load_config(self.proxy.config_path)['port'], port + 1)
            register.assert_called_once_with(self.proxy.stop)
            saved_handler[0](cdp.signal.SIGTERM, None)
            previous.assert_called_once_with(cdp.signal.SIGTERM, None)
            self.assertEqual(self.env.call_args_list[-1].args[0][-2:], ['-gu', 'TETHER_CDP_URL'])
            httpd.shutdown.assert_called_once()
        with patch.object(cdp, 'ThreadingHTTPServer', side_effect=PermissionError(errno.EPERM, 'blocked')) as factory:
            with self.assertRaises(PermissionError):
                self.proxy.start()
            self.assertEqual(factory.call_count, 1)

    def test_binary_check_does_not_start_chromium(self):
        self.client.cleanup()
        quiet = browser.Browser(chrome_finder=Mock(return_value='/fake/chrome'))
        proxy = cdp.Proxy(quiet, self.proxy.config_path, env_runner=Mock())
        self.assertTrue(proxy.availability()[0])
        self.assertIsNone(quiet.conn)
        quiet.chrome_finder.return_value = None
        self.assertEqual(proxy.availability(), (False, 'No Chromium found.'))
        self.assertIsNone(quiet.conn)

    def test_availability_config_env_asks_and_panel_routes(self):
        config = self.proxy.config_path
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(config.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(len(self.proxy.config['token']), 43)
        self.assertEqual(cdp.load_config(config), self.proxy.config)
        self.proxy.sync_env()
        command = self.env.call_args_list[-1].args[0]
        self.assertEqual(command, [cdp.paths.TMUX, 'set-environment', '-g', 'TETHER_CDP_URL', self.proxy.url])
        handler = FakeHandler('/mobile/api/browser/agents', {'enabled': False})
        browser_api.post(handler, self.browser, self.proxy)
        self.assertFalse(handler.replies[0][1]['available'])
        self.assertEqual(self.env.call_args_list[-1].args[0][-2:], ['-gu', 'TETHER_CDP_URL'])
        self.assertEqual(self.client.transport.closes, [1011])
        self.proxy.set_enabled(True)
        self.proxy.version_checked('Chrome/119.0.0.0')
        self.assertFalse(self.proxy.state()['available'])
        self.proxy.version_checked('Chrome/120.0.0.0')
        self.assertTrue(self.proxy.state()['available'])
        ask = self.proxy.ask('Please sign in.', '%12')
        self.proxy.ask('Use this tab.', '%12')
        self.assertEqual(len(self.proxy.asks), 1)
        for n in range(25):
            self.proxy.ask('Help', '%%%d' % (100 + n))
        self.assertEqual(len(self.proxy.asks), 20)
        self.assertEqual(self.notify.call_args.kwargs, {'urgent': True})
        self.assertEqual(self.notify.call_args.args[0]['title'], 'Agent needs you in the browser')
        self.assertEqual(self.notify.call_args.args[0]['url'], '/mobile/#session=%25124')
        handler = FakeHandler('/mobile/api/browser/ask/dismiss', {'id': self.proxy.asks[-1]['id']})
        browser_api.post(handler, self.browser, self.proxy)
        self.assertEqual(len(handler.replies[0][1]['asks']), 19)
        handler = FakeHandler('/mobile/api/browser/agents')
        browser_api.get(handler, self.browser, self.proxy)
        self.assertEqual(set(handler.replies[0][1]), {'enabled', 'available', 'reason', 'agents', 'asks'})
        for text in ('', 'a' * 301):
            with self.assertRaises(ValueError):
                self.proxy.ask(text, None)
        self.proxy.dismiss(ask['id'])


class HTTPTests(unittest.TestCase):
    def handler(self, path, headers=None, proxy=None):
        handler = object.__new__(cdp.Handler)
        handler.path = path
        handler.headers = Message()
        for key, value in (headers or {'Host': '127.0.0.1:47123'}).items():
            handler.headers[key] = value
        handler.server = SimpleNamespace(proxy=proxy or SimpleNamespace(config={'port': 47123, 'token': 'secret'}))
        handler.respond = Mock()
        return handler

    def test_token_origin_and_host_on_all_routes(self):
        for prefix in ('cdp', 'health', 'ask'):
            for token in ('wrong', ''):
                handler = self.handler('/%s/%s' % (prefix, token))
                self.assertIsNone(handler.route())
                handler.respond.assert_called_once_with(404, {})
            for headers in ({'Host': 'evil.test:47123'}, {'Host': '127.0.0.1:80'},
                            {'Host': 'localhost:47123', 'Origin': ''},
                            {'Host': '127.0.0.1:47123', 'Origin': 'http://localhost'}):
                handler = self.handler('/%s/secret' % prefix, headers)
                self.assertIsNone(handler.route())
                handler.respond.assert_called_once_with(403, {})
            for host in ('127.0.0.1:47123', 'localhost:47123'):
                self.assertEqual(self.handler('/%s/secret' % prefix, {'Host': host}).route(), '/' + prefix)
        handler = self.handler('/health/secret')
        handler.headers['Host'] = 'evil.test'
        self.assertIsNone(handler.route())

    def test_unsupported_methods_still_require_token_and_safe_headers(self):
        for path, expected in (('/health/wrong', 404), ('/health/secret', 403)):
            handler = self.handler(path, {'Host': 'evil.test'})
            handler.raw_requestline = ('OPTIONS %s HTTP/1.1\r\n' % path).encode()
            handler.rfile = io.BytesIO(b'Host: evil.test\r\n\r\n')
            self.assertFalse(handler.parse_request())
            handler.respond.assert_called_once_with(expected, {})

    def test_health_discovery_and_max_clients_before_upgrade(self):
        proxy = SimpleNamespace(config={'port': 47123, 'token': 'secret'}, url='ws://127.0.0.1:47123/cdp/secret',
                                browser=SimpleNamespace(product='Chrome/147.0.0.0'), availability=lambda: (True, ''),
                                lock=threading.RLock(), clients=set(range(8)))
        for suffix in ('json/version', 'json/version/'):
            handler = self.handler('/cdp/secret/' + suffix, proxy=proxy)
            handler.do_GET()
            handler.respond.assert_called_once_with(200, {'Browser': 'Chrome/147.0.0.0', 'webSocketDebuggerUrl': proxy.url})
        handler = self.handler('/health/secret', proxy=proxy)
        handler.do_GET()
        handler.respond.assert_called_once_with(200, {'ok': True})
        handler = self.handler('/cdp/secret', {'Host': '127.0.0.1:47123', 'Upgrade': 'websocket', 'Connection': 'Upgrade',
                                             'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': 'YWJjZGVmZ2hpamtsbW5vcA=='}, proxy)
        handler.do_GET()
        handler.respond.assert_called_once_with(503, {})
        proxy.availability = lambda: (False, 'disabled')
        handler = self.handler('/health/secret', proxy=proxy)
        handler.do_GET()
        handler.respond.assert_called_once_with(503, {'ok': False, 'reason': 'disabled'})

    def test_ask_infers_pane_and_checks_body(self):
        proxy = SimpleNamespace(config={'port': 47123, 'token': 'secret'}, pane_finder=Mock(return_value='%12'),
                                ask=Mock(return_value={'id': 'a'}))
        handler = self.handler('/ask/secret', proxy=proxy)
        handler.connection = Mock()
        handler.client_address = ('127.0.0.1', 55001)
        body = json.dumps({'text': 'Click sign in.'}).encode()
        handler.headers['Content-Length'] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler.do_POST()
        proxy.pane_finder.assert_called_once_with(55001, 47123)
        proxy.ask.assert_called_once_with('Click sign in.', '%12')
        handler.respond.assert_called_once_with(200, {'id': 'a'})


class PeerTests(unittest.TestCase):
    def fixture(self, tcp6=False):
        tcp = 'header\n 0: 0100007F:D6D9 0100007F:B813 01 0:0 00:0 0 1001 0 12345\n'
        files = {'/proc/net/tcp': 'header\n' if tcp6 else tcp, '/proc/net/tcp6': tcp if tcp6 else 'header\n',
                 '/proc/42/stat': '42 (node (script)) S 41 0 0', '/proc/41/stat': '41 (shell) S 40 0 0',
                 '/proc/40/stat': '40 (tmux pane) S 1 0 0'}
        return dict(read_text=lambda path: files[path], list_dir=lambda path: ['1', '42'] if path == '/proc' else ['0', '3'],
                    read_link=lambda path: 'socket:[12345]' if path.endswith('/3') else '/dev/null',
                    uid_of=lambda path: 0 if path.endswith('/1') else 1001,
                    pane_reader=lambda timeout: '%12 40\n%14 50\n', uid=1001)

    def test_inode_pid_ancestor_pane_ipv4_ipv6_and_other_user(self):
        for tcp6 in (False, True):
            self.assertEqual(cdp.peer_pane(55001, 47123, **self.fixture(tcp6)), '%12')
        fixture = self.fixture()
        fixture['uid_of'] = lambda path: 0
        self.assertIsNone(cdp.peer_pane(55001, 47123, **fixture))
        self.assertIsNone(cdp.peer_pane(55002, 47123, **self.fixture()))

    def test_missing_process_no_matching_pane_and_two_second_cap(self):
        fixture = self.fixture()
        fixture['pane_reader'] = lambda timeout: ''
        fixture['read_text'] = lambda path: '42 (gone) S 0 0 0' if path.startswith('/proc/42/') else self.fixture()['read_text'](path)
        self.assertIsNone(cdp.peer_pane(55001, 47123, **fixture))
        fixture = self.fixture()
        ticks = iter([0, 0, 0, 3])
        fixture['clock'] = lambda: next(ticks, 3)
        self.assertIsNone(cdp.peer_pane(55001, 47123, **fixture))


if __name__ == '__main__':
    unittest.main()
