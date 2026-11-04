"""HTTP routes for the browser panel (/mobile/api/browser/...). server.py checks sign-in, Origin and the
X-Codex-Mobile header before it calls these."""
import json
import time
from urllib.parse import parse_qs, urlsplit

import browser
import browser_cdp
import agent_browser_setup

PREFIX = '/mobile/api/browser/'
FRAME_INTERVAL = 1 / 15  # at most 15 frames a second per viewer
STREAM_LIFETIME = 600
KEEPALIVE = 15
POST_NAMES = {'new', 'close', 'navigate', 'history', 'input', 'device', 'agents', 'agents/setup', 'ask/dismiss'}


def handles(path):
    return path.startswith(PREFIX)


def get(handler, the_browser=None, the_proxy=None, the_setup=None):
    the_browser = the_browser or browser.BROWSER
    parts = urlsplit(handler.path)
    query = parse_qs(parts.query)
    name = parts.path[len(PREFIX):]
    try:
        if name == 'agents':
            handler.respond(200, _agent_state(_proxy(the_proxy), the_setup))
        elif name == 'tabs':
            handler.respond(200, the_browser.list_tabs())
        elif name == 'stream':
            stream(handler, the_browser, query.get('tab', [''])[0], _int(query, 'w', 1280), _int(query, 'h', 800))
        else:
            handler.respond(404, {'error': 'Not found'})
    except browser.BrowserError as exc:
        handler.respond(503, {'error': str(exc)})
    except ValueError as exc:
        handler.respond(400, {'error': str(exc)})


def post(handler, the_browser=None, the_proxy=None, the_setup=None):
    the_browser = the_browser or browser.BROWSER
    the_setup = the_setup or agent_browser_setup
    name = handler.path.split('?', 1)[0][len(PREFIX):]
    if name not in POST_NAMES:
        handler.respond(404, {'error': 'Not found'})
        return
    try:
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= 262144:
            handler.respond(413, {'error': 'Invalid request.'})
            return
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError('Invalid request.')
        tab = body.get('tab')
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
            result = the_browser.new_tab(body.get('url') or 'about:blank', body.get('mode') or 'private')
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
            handler.respond(404, {'error': 'Not found'})
            return
        handler.respond(200, result)
    except browser.BrowserError as exc:
        handler.respond(503, {'error': str(exc)})
    except (ValueError, UnicodeError) as exc:
        handler.respond(400, {'error': str(exc) or 'Invalid request.'})


def _proxy(the_proxy):
    proxy = the_proxy or browser_cdp.PROXY
    if proxy is None:
        raise browser.BrowserError('Agent browser is unavailable.')
    return proxy


def _agent_state(proxy, the_setup=None, state=None):
    the_setup = the_setup or agent_browser_setup
    result = dict(proxy.state() if state is None else state)
    result['setup'] = the_setup.status()
    result['stale'] = the_setup.stale_panes(proxy.url) if result['enabled'] and result['available'] else []
    return result


def stream(handler, the_browser, target, width, height):
    """Server-sent events for one tab: `frame` (JPEG), `tabs` (tab list and back/forward state), `closed`."""
    tab = the_browser.watch(target, width, height)
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
        while time.monotonic() - started < STREAM_LIFETIME:
            frame = the_browser.first_frame(tab) if first else the_browser.wait_frame(tab, seen, 1.0)
            first = False
            if time.monotonic() - checked >= 1.0:
                checked = time.monotonic()
                the_browser.refresh(tab)
            if tab.target not in the_browser.tabs:
                handler.wfile.write(b'event: closed\ndata: {}\n\n')
                handler.wfile.flush()
                return
            if the_browser.version != version:
                version = the_browser.version
                state = the_browser.state()
                try:
                    state['nav'] = the_browser.nav_state(tab.target)
                except browser.BrowserError:
                    state['nav'] = {'back': False, 'forward': False}
                handler.wfile.write(('event: tabs\ndata: ' + json.dumps(state) + '\n\n').encode())
                wrote = time.monotonic()
            if frame is not None and not the_browser.heal(tab, frame):
                seen = frame[0]  # a frame at the wrong size would map clicks wrongly; the next one fits
                the_browser.ack(tab, frame)
            elif frame is not None:
                sent = time.monotonic()
                handler.wfile.write(browser.frame_event(frame))
                handler.wfile.flush()
                seen, wrote = frame[0], time.monotonic()
                time.sleep(max(0.0, FRAME_INTERVAL - (wrote - sent)))
                the_browser.ack(tab, frame)
            elif time.monotonic() - wrote >= KEEPALIVE:
                handler.wfile.write(b'event: idle\ndata: {}\n\n')
                wrote = time.monotonic()
            handler.wfile.flush()
            the_browser.touch()
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        return
    finally:
        the_browser.unwatch(tab)


def _int(query, key, default):
    try:
        return int(query.get(key, [default])[0])
    except (TypeError, ValueError):
        return default
