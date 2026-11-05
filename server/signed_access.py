"""In-memory, phone-approved capabilities for tabs in the signed-in default context.

The private-context proxy cannot isolate a default context. This proxy instead owns
an explicit target set and synthesizes discovery/auto-attach for that set only.
No browser-wide auto-attach or discovery command from an agent reaches Chrome.
"""
import hmac
from http.server import ThreadingHTTPServer
import re
import secrets
import threading
import time
from urllib.parse import quote, urlsplit

import browser
import browser_cdp as cdp


# Exact command allowlist. Unknown commands fail closed, including on child sessions.
PAGE_METHODS = {
    'Page': '''enable disable getFrameTree getNavigationHistory navigate navigateToHistoryEntry reload
        stopLoading bringToFront close captureScreenshot printToPDF getLayoutMetrics
        addScriptToEvaluateOnNewDocument removeScriptToEvaluateOnNewDocument createIsolatedWorld
        setLifecycleEventsEnabled handleJavaScriptDialog setInterceptFileChooserDialog
        getResourceTree getResourceContent setDocumentContent setWebLifecycleState''',
    'Runtime': '''enable disable evaluate callFunctionOn getProperties releaseObject releaseObjectGroup
        awaitPromise runIfWaitingForDebugger addBinding removeBinding getIsolateId compileScript runScript
        globalLexicalScopeNames discardConsoleEntries''',
    'DOM': '''enable disable getDocument getFlattenedDocument querySelector querySelectorAll describeNode
        resolveNode requestNode getBoxModel getContentQuads getNodeForLocation focus scrollIntoViewIfNeeded
        setFileInputFiles getOuterHTML setOuterHTML setAttributeValue removeAttribute
        collectClassNamesFromSubtree pushNodesByBackendIdsToFrontend requestChildNodes''',
    'Network': '''enable disable setCacheDisabled setBypassServiceWorker setExtraHTTPHeaders
        setUserAgentOverride getResponseBody getRequestPostData getCookies setBlockedURLs
        emulateNetworkConditions emulateNetworkConditionsByRule overrideNetworkState''',
    'Input': '''dispatchKeyEvent dispatchMouseEvent dispatchTouchEvent insertText imeSetComposition
        synthesizePinchGesture synthesizeScrollGesture synthesizeTapGesture setIgnoreInputEvents''',
    'Emulation': '''setDeviceMetricsOverride clearDeviceMetricsOverride setTouchEmulationEnabled
        setEmitTouchEventsForMouse setUserAgentOverride setFocusEmulationEnabled
        setEmulatedMedia setLocaleOverride setTimezoneOverride setGeolocationOverride
        clearGeolocationOverride setScriptExecutionDisabled setCPUThrottlingRate
        setDefaultBackgroundColorOverride setVirtualTimePolicy setAutomationOverride
        setIdleOverride clearIdleOverride setEmulatedVisionDeficiency setScrollbarsHidden''',
    'Fetch': '''enable disable continueRequest continueResponse fulfillRequest failRequest continueWithAuth
        getResponseBody takeResponseBodyAsStream''',
    'Log': 'enable disable clear startViolationsReport stopViolationsReport',
    'Performance': 'enable disable getMetrics',
    'Accessibility': 'enable disable getFullAXTree getPartialAXTree getRootAXNode getChildAXNodes queryAXTree',
}
PAGE_ALLOWED = {domain + '.' + method for domain, methods in PAGE_METHODS.items() for method in methods.split()}
REQUEST_SECONDS = 120


def minutes(value):
    if type(value) is not int or not 1 <= value <= 60:
        raise ValueError('Minutes must be an integer from 1 to 60.')
    return value


class Grant:
    def __init__(self, request, target, duration, now):
        self.row = {key: request[key] for key in ('id', 'pane', 'agent', 'reason')}
        self.row.update(tab=target, expires=now + duration * 60)
        self.token = secrets.token_urlsafe(32)
        self.targets = {target}
        self.extra = 0
        self.active = True
        self.timer = None
        self.emulation = {}  # Shared across reconnects and concurrent sockets; used by the p12 handoff.


class Access:
    def __init__(self, the_browser=None, notifier=lambda *a, **k: None, agent_finder=lambda pane: 'agent',
                 clock=time.time, request_seconds=REQUEST_SECONDS):
        self.browser = the_browser or browser.SIGNED_BROWSER
        if not self.browser.signed:
            raise ValueError('Signed access requires the signed-in browser.')
        self.notifier, self.agent_finder, self.clock = notifier, agent_finder, clock
        self.request_seconds = request_seconds
        self.lock = threading.RLock()
        self.pending, self.grants, self.reserved = {}, {}, set()
        self.clients = set()
        self.config = {'port': 0}
        self.httpd = self.thread = None
        self.stopped = False
        self.browser.agent_proxy = self

    def version_checked(self, product):
        pass

    def owns_context(self, context):
        return False

    def busy(self):
        with self.lock:
            return bool(self.grants)

    def availability(self):
        return (not self.stopped, 'Signed access stopped.' if self.stopped else '')

    def state(self):
        with self.lock:
            return {'requests': [dict(slot['row']) for slot in self.pending.values()],
                    'grants': [dict(grant.row) for grant in self.grants.values() if self.valid(grant)]}

    def valid(self, grant):
        return not self.stopped and grant.active and self.clock() < grant.row['expires']

    def ask(self, reason, pane, duration=15, url=None):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
            raise ValueError('Ask text must contain 1 to 300 characters.')
        if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
            raise ValueError('A current tmux pane is required.')
        duration = minutes(duration)
        if url is not None:
            if not isinstance(url, str) or len(url) > 8192:
                raise ValueError('Invalid URL.')
            url = browser.normalize_url(url)
        row = dict(id=secrets.token_urlsafe(16), pane=pane, agent=self.agent_finder(pane), reason=reason.strip(),
                   minutes=duration, url=url, created=self.clock())
        slot = {'row': row, 'event': threading.Event(), 'result': {'ok': False, 'reason': 'denied'},
                'deadline': time.monotonic() + self.request_seconds}
        with self.lock:
            if self.stopped:
                return slot['result']
            for old in list(self.pending.values()):
                if old['row']['pane'] == pane:
                    self._answer(old, {'ok': False, 'reason': 'denied'})
            self.pending[row['id']] = slot
            self.browser._bump()
        payload = {'title': 'Agent wants signed-in browser access',
                   'body': f"{row['agent']} wants to use your signed-in browser: {row['reason']}",
                   'tag': 'browser-ask-' + pane, 'session': pane, 'priority': 'urgent',
                   'url': '/mobile/#session=' + quote(pane, safe='')}
        try:
            self.notifier(payload, urgent=True)
            slot['event'].wait(max(0, slot['deadline'] - time.monotonic()))
        finally:
            with self.lock:
                if not slot['event'].is_set():
                    self._answer(slot, {'ok': False, 'reason': 'timeout'})
        return slot['result']

    def _answer(self, slot, result):
        self.pending.pop(slot['row']['id'], None)
        slot['result'] = result
        slot['event'].set()
        self.browser._bump()

    def _request(self, ident):
        slot = self.pending.get(ident) if isinstance(ident, str) else None
        if not slot or time.monotonic() >= slot['deadline'] or ident in self.reserved:
            raise ValueError('That request is no longer pending.')
        return slot

    def deny(self, ident):
        with self.lock:
            self._answer(self._request(ident), {'ok': False, 'reason': 'denied'})
        return self.state()

    def _listen(self):
        if self.httpd:
            return
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.proxy = self
        self.config['port'] = self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name='signed-access-http')
        self.thread.start()

    def allow(self, ident, duration=None):
        # Browser.lock serializes allow/end/handoffs, but is never taken on the CDP reader.
        with self.browser.lock:
            with self.lock:
                slot = self._request(ident)
                requested = slot['row']['minutes']
                duration = requested if duration is None else minutes(duration)
                if duration > requested:
                    raise ValueError('Allow may shorten the requested time only.')
                if len(self.grants) + len(self.reserved) >= 2:
                    raise ValueError('End a grant first (the limit is 2).')
                self.reserved.add(ident)
            try:
                self._listen()
                row = self.browser.new_tab(slot['row']['url'] or 'about:blank', 'saved')
                grant = Grant(slot['row'], row['id'], duration, self.clock())
                with self.lock:
                    if slot['event'].is_set() or time.monotonic() >= slot['deadline'] or self.stopped:
                        raise ValueError('That request is no longer pending.')
                    self.grants[ident] = grant
                    self._mark(grant, self.browser.tab(row['id']))
                    grant.timer = threading.Timer(max(0, grant.row['expires'] - self.clock()), self.end, args=(ident,))
                    grant.timer.daemon = True
                    grant.timer.start()
                    self._answer(slot, {'ok': True, 'cdp': self.url(grant), 'tab': row['id'], 'expires': grant.row['expires']})
            finally:
                with self.lock:
                    self.reserved.discard(ident)
        return self.state()

    def url(self, grant):
        return f"ws://127.0.0.1:{self.config['port']}/cdp-signed/{grant.token}"

    def _mark(self, grant, tab):
        tab.mode, tab.owner, tab.controller = 'agent', {'pane': grant.row['pane']}, 'agent'
        tab.grant = {'id': grant.row['id'], 'expires': grant.row['expires']}
        tab.agent_client = grant
        self.browser._bump()

    def end(self, ident):
        if not isinstance(ident, str):
            raise ValueError('Choose a grant.')
        # Revoke without Browser.lock so a slow in-flight CDP call cannot delay expiry.
        with self.lock:
            grant = self.grants.pop(ident, None)
            if grant:
                grant.active = False
                grant.timer.cancel()
            clients = [client for client in self.clients if client.grant is grant]
            self.browser._bump()
        for client in clients:
            client.revoke()
        with self.browser.lock:
            if grant:
                for target in list(grant.targets):
                    tab = self.browser.tabs.get(target)
                    if tab:
                        tab.mode, tab.owner, tab.controller = 'saved', None, 'user'
                        tab.grant = tab.agent_client = None
                self.browser._bump()
        return self.state()

    def browser_stopped(self):
        # Called on the pipe reader as well as Browser.stop: no CDP calls or Browser.lock here.
        with self.lock:
            grants, self.grants = list(self.grants.values()), {}
            for grant in grants:
                grant.active = False
                grant.timer.cancel()
            for slot in list(self.pending.values()):
                self._answer(slot, {'ok': False, 'reason': 'denied'})
            clients = list(self.clients)
        for client in clients:
            client.revoke()

    def target_event(self, method, params):
        info = params.get('targetInfo') or {}
        target = info.get('targetId') or params.get('targetId')
        with self.lock:
            grants = list(self.grants.values())
            for grant in grants:
                if not self.valid(grant):
                    continue
                if method == 'Target.targetCreated' and info.get('type') == 'page' and info.get('openerId') in grant.targets:
                    grant.targets.add(target)
                    threading.Thread(target=self.adopt, args=(grant, target, info), daemon=True,
                                     name='signed-popup-adopt').start()
                if method == 'Target.targetDestroyed' and target == grant.row['tab']:
                    grant.active = False
                    for client in list(self.clients):
                        if client.grant is grant:
                            client.revoke()
                    threading.Thread(target=self.end, args=(grant.row['id'],), daemon=True, name='signed-grant-end').start()
            clients = list(self.clients)
        for client in clients:
            client.target_event(method, params)
        if method == 'Target.targetDestroyed':
            with self.lock:
                for grant in grants:
                    if target != grant.row['tab']:
                        grant.targets.discard(target)

    def adopt(self, grant, target, info=None):
        with self.browser.lock:
            if not self.browser.running():
                return
            tab = self.browser.tabs.get(target)
            if not tab:
                try:
                    tab = self.browser._adopt(target, info)
                except browser.BrowserError:
                    return
            with self.lock:
                if self.valid(grant):
                    self._mark(grant, tab)

    def stop(self):
        self.stopped = True
        for ident in list(self.grants):
            self.end(ident)
        self.browser_stopped()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=2)


class Client(cdp.AgentClient):
    """Reuse errors and wire transport only; private-context routing never runs here."""
    def __init__(self, proxy, transport, grant):
        super().__init__(proxy, transport, grant.row['pane'])
        self.grant = grant
        self.emulation = grant.emulation
        self.discover = self.auto = False
        self.number = 0
        self.real_sessions = {}  # Public session ids must differ from Tether's panel sessions.

    def open(self):
        with self.proxy.lock:
            if not self.proxy.valid(self.grant):
                raise browser.BrowserError('That grant has ended.')
            self.conn = self.proxy.browser.conn

    def result(self, message, result=None):
        reply = {'id': message['id'], 'result': result or {}}
        if message.get('sessionId'):
            reply['sessionId'] = message['sessionId']
        if not self.closed and self.proxy.valid(self.grant):
            self.transport.message(reply)

    def info(self, target):
        info = self.conn.call('Target.getTargetInfo', {'targetId': target})['targetInfo']
        return self.clean_info(info)

    def clean_info(self, info):
        info = dict(info)
        # Chrome includes an opaque id even for the default context. Playwright requires it;
        # unknown context ids map to its persistent default context. Never expose Chrome's id.
        info['browserContextId'] = 'signed-default'
        if info.get('openerId') not in self.grant.targets:
            info.pop('openerId', None)
            info.pop('openerFrameId', None)
        return info

    def attach(self, target):
        with self.proxy.browser.lock:
            if self.closed or not self.proxy.valid(self.grant) or target not in self.grant.targets:
                raise browser.BrowserError('That grant has ended.')
            real = self.conn.call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
            self.number += 1
            public = f'signed-{self.number}'
            self.sessions[public], self.real_sessions[public] = target, real
            self.conn.register_sink(real, lambda event: self.event(dict(event, sessionId=public)))
            return public

    def auto_attach(self, target):
        with self.proxy.browser.lock:
            if target in self.sessions.values() or self.closed or not self.proxy.valid(self.grant):
                return
            try:
                info = self.info(target)
                session = self.attach(target)
                self.transport.message({'method': 'Target.attachedToTarget',
                                        'params': {'sessionId': session, 'targetInfo': info, 'waitingForDebugger': False}})
            except browser.BrowserError:
                pass

    def _references_allowed(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('targetId', 'openerId') and (not isinstance(item, str) or item not in self.grant.targets):
                    return False
                if key == 'browserContextId' or key == 'sessionId' and (not isinstance(item, str) or item not in self.sessions):
                    return False
                if not self._references_allowed(item):
                    return False
        elif isinstance(value, list):
            return all(self._references_allowed(item) for item in value)
        return True

    def handle(self, message):
        with self.proxy.browser.lock:
            self._handle(message)

    def _handle(self, message):
        if (not isinstance(message, dict) or type(message.get('id')) is not int
                or not isinstance(message.get('method'), str) or not isinstance(message.get('params', {}), dict)):
            self.error(message if isinstance(message, dict) else {}, 'Invalid CDP request.', -32600)
            return
        if self.closed or not self.proxy.valid(self.grant):
            self.revoke()
            return
        method, params, session = message['method'], dict(message.get('params') or {}), message.get('sessionId')
        if (session is not None and (not isinstance(session, str) or session not in self.sessions)) or not self._references_allowed(params):
            self.error(message, 'Outside this signed browser grant.')
            return
        try:
            if method.startswith('Target.'):
                self.target_command(message, method, params, session)
            elif not session and method == 'Browser.getVersion':
                self.result(message, self.conn.call(method))
            elif not session and method == 'Browser.setDownloadBehavior':
                # Playwright always sends this during connectOverCDP. Acknowledge without
                # changing Chrome's browser-wide deny policy or enabling download events.
                self.result(message)
            elif session and method in PAGE_ALLOWED:
                if method == 'Network.getCookies' and params.get('urls'):
                    raise ValueError('Cookie reads use this page only; omit urls.')
                if method == 'Page.navigate':
                    params['url'] = browser.normalize_url(params.get('url'))
                # Serialize emulation with the existing panel Take over / Give back path.
                with self.proxy.browser.lock:
                    if not self.proxy.valid(self.grant):
                        self.revoke()
                        return
                    target = self.sessions[session]
                    tab = self.proxy.browser.tabs.get(target)
                    emulation = (method.startswith(('Emulation.', 'Network.emulate'))
                                 or method in ('Network.setUserAgentOverride', 'Network.overrideNetworkState'))
                    if emulation and tab and tab.controller == 'user':
                        raise ValueError('The user controls this tab. Give back control first.')
                    result = self.conn.call(method, params, self.real_sessions[session])
                    if method in browser.AGENT_EMULATION:
                        self.emulation.setdefault(target, {})[browser.AGENT_EMULATION[method]] = (method, params)
                    elif method == 'Network.setUserAgentOverride':
                        self.emulation.setdefault(target, {})['ua'] = ('Emulation.setUserAgentOverride', params)
                self.result(message, result)
            else:
                self.error(message, 'Not allowed through signed browser access.', -32601)
        except (browser.BrowserError, ValueError, KeyError, TypeError) as exc:
            self.error(message, str(exc))

    def target_command(self, message, method, params, session):
        # Discovery is virtual, never Chrome's global discovery/auto-attach.
        if method == 'Target.setDiscoverTargets' and not session:
            self.discover = bool(params.get('discover'))
            if self.discover:
                for target in list(self.grant.targets):
                    self.transport.message({'method': 'Target.targetCreated', 'params': {'targetInfo': self.info(target)}})
            self.result(message)
        elif method == 'Target.setAutoAttach':
            if params.get('flatten') is not True:
                raise ValueError('Use flattened sessions.')
            if not session:
                self.auto = bool(params.get('autoAttach'))
                if self.auto:
                    for target in list(self.grant.targets):
                        self.auto_attach(target)
            self.result(message)
        elif method == 'Target.getTargets' and not session:
            self.result(message, {'targetInfos': [self.info(target) for target in list(self.grant.targets)]})
        elif method == 'Target.getBrowserContexts' and not session:
            self.result(message, {'browserContextIds': []})
        elif method == 'Target.getTargetInfo':
            target = params.get('targetId') or self.sessions.get(session) or self.grant.row['tab']
            self.result(message, {'targetInfo': self.info(target)})
        elif method == 'Target.attachToTarget' and not session and params.get('flatten') is True:
            self.result(message, {'sessionId': self.attach(params['targetId'])})
        elif method == 'Target.detachFromTarget' and not session:
            public = params['sessionId']
            real = self.real_sessions.pop(public)
            self.sessions.pop(public)
            self.conn.unregister_sink(real)
            self.conn.call(method, {'sessionId': real})
            self.result(message)
        elif method in ('Target.closeTarget', 'Target.activateTarget') and not session:
            self.result(message, self.conn.call(method, {'targetId': params['targetId']}))
        elif method == 'Target.createTarget' and not session:
            with self.proxy.browser.lock:
                if not self.proxy.valid(self.grant):
                    raise ValueError('That grant has ended.')
                if self.grant.extra >= 2:
                    raise ValueError('Extra tab limit (2) for this grant.')
                # Do not forward window/context/target switches supplied by the client.
                url = browser.normalize_url(params.get('url', 'about:blank'))
                result = self.conn.call(method, {'url': 'about:blank'})
                target = result['targetId']
                self.grant.extra += 1
                with self.proxy.lock:
                    self.grant.targets.add(target)
                self.proxy.adopt(self.grant, target)
                if url != 'about:blank':
                    self.proxy.browser.navigate(target, url)
                info = self.info(target)
                for client in list(self.proxy.clients):
                    if client.grant is self.grant:
                        client.target_event('Target.targetCreated', {'targetInfo': info})
                self.result(message, result)
        else:
            self.error(message, 'Not allowed through signed browser access.', -32601)

    def target_event(self, method, params):
        if self.closed or not self.proxy.valid(self.grant):
            return
        info = params.get('targetInfo') or {}
        target = info.get('targetId') or params.get('targetId')
        if target not in self.grant.targets:
            return
        if method in ('Target.targetCreated', 'Target.targetInfoChanged') and self.discover:
            self.transport.message({'method': method, 'params': {'targetInfo': self.clean_info(info)}})
        if method == 'Target.targetCreated' and self.auto:
            threading.Thread(target=self.auto_attach, args=(target,), daemon=True, name='signed-auto-attach').start()
        if method == 'Target.targetDestroyed':
            if self.discover:
                self.transport.message({'method': method, 'params': {'targetId': target}})
            for public, attached in list(self.sessions.items()):
                if attached == target:
                    self.conn.unregister_sink(self.real_sessions.pop(public))
                    self.sessions.pop(public)
                    self.transport.message({'method': 'Target.detachedFromTarget', 'params': {'sessionId': public, 'targetId': target}})

    def event(self, message):
        if self.closed or not self.proxy.valid(self.grant):
            return
        # No nested target sessions or global events can escape this target set.
        if message.get('sessionId') in self.sessions and not message['method'].startswith('Target.'):
            self.transport.message(message)

    def revoke(self):
        self.closed = True
        if self.transport:
            self.transport.close(1000)
            abort = getattr(self.transport, 'abort', None)
            if abort:
                abort()  # Do not let a slow reader delay revocation behind queued frames.

    def cleanup(self):
        self.closed = True
        if self.conn:
            for real in list(self.real_sessions.values()):
                self.conn.unregister_sink(real)
                self.conn.send('Target.detachFromTarget', {'sessionId': real})
        with self.proxy.lock:
            self.proxy.clients.discard(self)


class Handler(cdp.Handler):
    def route(self):
        proxy = self.server.proxy
        parts = urlsplit(self.path)
        pieces = parts.path.split('/')
        if (parts.query or len(pieces) != 3 or pieces[1] != 'cdp-signed'
                or self.headers.get_all('Origin') is not None
                or self.headers.get_all('Host', []) != [f"127.0.0.1:{proxy.config['port']}"]):
            self.respond(403, {})
            return None
        with proxy.lock:
            self.grant = next((grant for grant in proxy.grants.values()
                               if proxy.valid(grant) and hmac.compare_digest(grant.token.encode(), pieces[2].encode())), None)
        if self.grant is None:
            self.respond(404, {})
            return None
        return '/cdp'

    def create_client(self, proxy):
        return Client(proxy, None, self.grant)

    def do_POST(self):
        self.respond(405, {})
