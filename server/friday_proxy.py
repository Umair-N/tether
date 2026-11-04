"""Proxy Friday signaling without importing the optional voice dependencies."""
import json
import os
import urllib.error
import urllib.request

URL = os.environ.get('TETHER_FRIDAY_URL', 'http://127.0.0.1:7686').rstrip('/')
OFFER = '/mobile/api/live/offer'
AGENT = '/mobile/api/live/agent'
LIMIT = 64 * 1024


def handles(path):
    return path.split('?', 1)[0] in (OFFER, AGENT)


def _request(method, path, body=None, timeout=15):
    request = urllib.request.Request(URL + path, data=body, method=method,
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except (ValueError, UnicodeError):
            raise RuntimeError('Friday is not running.') from exc
    except (urllib.error.URLError, OSError, ValueError, UnicodeError) as exc:
        raise RuntimeError('Friday is not running.') from exc


def get(handler):
    if handler.path.split('?', 1)[0] != AGENT:
        handler.respond(404, {'error': 'Not found'})
        return
    try:
        status, body = _request('GET', '/health', timeout=1)
        if status != 200 or not isinstance(body, dict):
            body = {'ok': False}
    except RuntimeError:
        body = {'ok': False}
    handler.respond(200, body)


def _offer(handler, method):
    if handler.path.split('?', 1)[0] != OFFER:
        handler.respond(404, {'error': 'Not found'})
        return
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= LIMIT:
            handler.close_connection = True
            handler.respond(413, {'error': 'Invalid request size.'})
            return
        if handler.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            handler.close_connection = True
            handler.respond(415, {'error': 'Send JSON.'})
            return
        raw = handler.rfile.read(size)
        if len(raw) != size or not isinstance(json.loads(raw), dict):
            raise ValueError('Invalid request.')
        status, body = _request(method, '/offer', raw)
        handler.respond(status, body)
    except (ValueError, UnicodeError):
        handler.respond(400, {'error': 'Invalid request.'})
    except RuntimeError as exc:
        handler.respond(503, {'error': str(exc)})


def post(handler):
    _offer(handler, 'POST')


def patch(handler):
    _offer(handler, 'PATCH')
