"""HTTP routes for the browser panel (/mobile/api/browser/...). server.py checks sign-in, Origin and the
X-Codex-Mobile header before it calls these."""
import json
import time
from urllib.parse import parse_qs, urlsplit

import browser
import browser_cdp
import agent_browser_setup
import mac_browser

PREFIX = '/mobile/api/browser/'
FRAME_INTERVAL = 1 / 30  # at most 30 frames a second per viewer
STREAM_LIFETIME = 600
KEEPALIVE = 15
POST_NAMES = {'new', 'close', 'navigate', 'history', 'input', 'device', 'agents', 'agents/setup', 'ask/dismiss',
              'signed/allow', 'signed/deny', 'signed/end'}


def handles(path):
    return path.startswith(PREFIX)


def get(handler, the_browser=None, the_proxy=None, the_setup=None, signed_browser=None):
    parts = urlsplit(handler.path)
    query = parse_qs(parts.query)
    name = parts.path[len(PREFIX):]
    browser_id = query.get('browser', ['test'])[0]
    if name == 'extension/devices':
        handler.respond(200, mac_browser.BROKER.rows())
        return
    if name == 'signed/access':
        browser_id = 'signed'
    try:
        the_browser = _select(browser_id, the_browser, signed_browser)
        _user_only(browser_id, name)
        if name == 'signed/access':
            proxy = the_proxy or browser_cdp.PROXY
            access = proxy.signed_access if proxy else _signed_access(the_browser, the_proxy)
            handler.respond(200, _tag(access.state(), browser_id))
        elif name == 'agents':
            handler.respond(200, _agent_state(_proxy(the_proxy), the_setup))
        elif name == 'tabs':
            state = the_browser.list_tabs()
            state['browsers'] = _browsers(the_browser if browser_id == 'test' else None,
                                        the_browser if browser_id == 'signed' else signed_browser)
            handler.respond(200, _tag(state, browser_id))
        elif name == 'stream':
            css = {'css_size': (_int(query, 'cssw', 0), _int(query, 'cssh', 0))} if 'cssw' in query or 'cssh' in query else {}
            stream(handler, the_browser, query.get('tab', [''])[0], _int(query, 'w', 1280), _int(query, 'h', 800), browser_id, **css)
        else:
            handler.respond(404, _tag({'error': 'Not found'}, browser_id))
    except browser.BrowserError as exc:
        handler.respond(503, _tag({'error': str(exc)}, browser_id))
    except ValueError as exc:
        handler.respond(400, _tag({'error': str(exc)}, browser_id))


def post(handler, the_browser=None, the_proxy=None, the_setup=None, signed_browser=None):
    the_setup = the_setup or agent_browser_setup
    name = handler.path.split('?', 1)[0][len(PREFIX):]
    browser_id = parse_qs(urlsplit(handler.path).query).get('browser', ['test'])[0]
    try:
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= 262144:
            handler.respond(413, _tag({'error': 'Invalid request.'}, browser_id))
            return
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError('Invalid request.')
        browser_id = body.get('browser', browser_id)
        if name in ('extension/pair', 'extension/revoke'):
            result = mac_browser.BROKER.pair() if name.endswith('/pair') else mac_browser.BROKER.revoke(body.get('id'))
            handler.respond(200, result)
            return
        if name.startswith('signed/'):
            browser_id = 'signed'
        the_browser = _select(browser_id, the_browser, signed_browser)
        if name not in POST_NAMES:
            handler.respond(404, _tag({'error': 'Not found'}, browser_id))
            return
        _user_only(browser_id, name)
        tab = body.get('tab')
        if name.startswith('signed/'):
            proxy = the_proxy or browser_cdp.PROXY
            access = proxy.signed_access if proxy else _signed_access(the_browser, the_proxy)
            if name == 'signed/allow':
                result = access.allow(body.get('id'), body.get('minutes'))
            elif name == 'signed/deny':
                result = access.deny(body.get('id'))
            else:
                result = access.end(body.get('id'))
            handler.respond(200, _tag(result, browser_id))
            return
        if name not in ('new', 'agents', 'agents/setup', 'ask/dismiss') and not isinstance(tab, str):
            raise ValueError('Choose a tab.')
        if name == 'agents':
            proxy = _proxy(the_proxy)
            result = _agent_state(proxy, the_setup, proxy.set_enabled(body.get('enabled')))
        elif name == 'agents/setup':
            agent = body.get('agent')
            if agent not in ('claude', 'codex'):
                raise ValueError('Choose Claude or Codex.')
            proxy = _proxy(the_proxy)
            try:
                (the_setup.fix_claude if agent == 'claude' else the_setup.fix_codex)()
            except (OSError, UnicodeError):
                raise ValueError('Could not update the agent settings.') from None
            result = _agent_state(proxy, the_setup)
        elif name == 'ask/dismiss':
            proxy = _proxy(the_proxy)
            result = _agent_state(proxy, the_setup, proxy.dismiss(body.get('id')))
        elif name == 'new':
            result = the_browser.new_tab(body.get('url') or 'about:blank', body.get('mode') or None)
        elif name == 'close':
            result = the_browser.close_tab(tab)
        elif name == 'navigate':
            result = the_browser.navigate(tab, body.get('url'))
        elif name == 'history':
            result = the_browser.history(tab, body.get('action'))
        elif name == 'input':
            result = the_browser.input(tab, body.get('events'))
        elif name == 'device':
            result = the_browser.set_device(tab, body.get('preset'), body.get('width'), body.get('height'))
        else:
            handler.respond(404, _tag({'error': 'Not found'}, browser_id))
            return
        handler.respond(200, _tag(result, browser_id))
    except browser.BrowserError as exc:
        handler.respond(503, _tag({'error': str(exc)}, browser_id))
    except (ValueError, UnicodeError) as exc:
        handler.respond(400, _tag({'error': str(exc) or 'Invalid request.'}, browser_id))


def _select(browser_id, test=None, signed=None):
    if browser_id == 'test':
        return test or browser.BROWSER
    if browser_id == 'signed':
        return signed or browser.SIGNED_BROWSER
    if browser_id == 'mac':
        return mac_browser.BROKER.browser
    raise ValueError('Choose the test, signed, or Mac browser.')


def _tag(result, browser_id):
    return dict(result, browser=browser_id) if browser_id in ('signed', 'mac') else result


def _user_only(browser_id, name):
    if browser_id in ('signed', 'mac') and name in ('agents', 'agents/setup', 'ask/dismiss'):
        raise ValueError('Agents cannot access the signed-in browser.')


def _browsers(test=None, signed=None):
    rows = []
    for ident, label, instance in (('test', 'Test browser', test or browser.BROWSER),
                                   ('signed', 'Signed-in browser', signed or browser.SIGNED_BROWSER)):
        reason = ''
        if not instance.chrome_finder():
            reason = 'Google Chrome is not installed.' if ident == 'signed' else 'No Chromium found.'
        elif ident == 'signed' and not instance.display.finder():
            reason = 'Xvfb is not installed.'
        rows.append({'id': ident, 'label': label, 'running': instance.running(), 'available': not reason, 'reason': reason})
    connected = mac_browser.BROKER.connected() is not None
    rows.append({'id': 'mac', 'label': 'Mac Chrome', 'running': connected, 'available': connected,
                 'reason': '' if connected else 'Mac Chrome is disconnected.'})
    return rows


def _proxy(the_proxy):
    proxy = the_proxy or browser_cdp.PROXY
    if proxy is None:
        raise browser.BrowserError('Agent browser is unavailable.')
    return proxy


def _signed_access(the_browser, the_proxy=None):
    access = the_browser.agent_proxy
    if access is None:
        proxy = the_proxy or browser_cdp.PROXY
        access = proxy.signed_access if proxy else None
    if access is None or access.browser is not the_browser:
        raise browser.BrowserError('Signed browser access is unavailable.')
    return access


def _agent_state(proxy, the_setup=None, state=None):
    the_setup = the_setup or agent_browser_setup
    result = dict(proxy.state() if state is None else state)
    result['setup'] = the_setup.status()
    result['stale'] = the_setup.stale_panes(proxy.url) if result['enabled'] and result['available'] else []
    return result


def stream(handler, the_browser, target, width, height, browser_id='test', css_size=None):
    """Server-sent events for one tab: `frame` (JPEG), `tabs` (tab list and back/forward state), `closed`."""
    tab = the_browser.watch(target, width, height, css_size) if css_size is not None else the_browser.watch(target, width, height)
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Accel-Buffering', 'no')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        handler.close_connection = True
        handler.wfile.write(b'retry: 1500\n\n')
        started = wrote = time.monotonic()
        seen, version = 0, None
        first = True
        checked = started
        next_frame = started
        while time.monotonic() - started < STREAM_LIFETIME:
            # Pace before taking the slot, so a slow write or a sleep never leaves an old frame queued for output.
            time.sleep(max(0.0, next_frame - time.monotonic()))
            if time.monotonic() - checked >= 1.0:
                checked = time.monotonic()
                the_browser.refresh(tab)
            if tab.target not in the_browser.tabs:
                handler.wfile.write(('event: closed\ndata: ' + json.dumps(_tag({}, browser_id)) + '\n\n').encode())
                handler.wfile.flush()
                return
            if the_browser.version != version:
                version = the_browser.version
                state = the_browser.state()
                try:
                    state['nav'] = the_browser.nav_state(tab.target)
                except browser.BrowserError:
                    state['nav'] = {'back': False, 'forward': False}
                handler.wfile.write(('event: tabs\ndata: ' + json.dumps(_tag(state, browser_id)) + '\n\n').encode())
                wrote = time.monotonic()
            frame = the_browser.first_frame(tab) if first else the_browser.wait_frame(tab, seen, 1.0)
            first = False
            if frame is not None and not the_browser.heal(tab, frame):
                seen = frame[0]  # a frame at the wrong size would map clicks wrongly; the next one fits
            elif frame is not None:
                next_frame = time.monotonic() + FRAME_INTERVAL
                handler.wfile.write(browser.frame_event(frame, browser_id))
                handler.wfile.flush()
                seen, wrote = frame[0], time.monotonic()
            elif time.monotonic() - wrote >= KEEPALIVE:
                handler.wfile.write(('event: idle\ndata: ' + json.dumps(_tag({}, browser_id)) + '\n\n').encode())
                wrote = time.monotonic()
            handler.wfile.flush()
            the_browser.touch()
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        return
    finally:
        the_browser.unwatch(tab, (width, height), css_size)


def _int(query, key, default):
    try:
        return int(query.get(key, [default])[0])
    except (TypeError, ValueError):
        return default
