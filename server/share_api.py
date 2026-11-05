"""Share HTTP contract; the host Handler supplies the existing authentication gate."""
import json
import os
import re
import sqlite3
import threading
from urllib.parse import parse_qs, quote, unquote, urlsplit

import auth
import paths
import share_previews
from share_store import CHUNK, OWNER, THUMB_LIMIT, ShareError, Store, authorize

PREFIX = '/mobile/api/share/'
_store = None
_store_lock = threading.Lock()


def get_store():
    global _store
    with _store_lock:
        if _store is None or _store.root != paths.SHARE.absolute():
            _store = Store()
        return _store


def security_headers(handler):
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Cache-Control', 'private, no-store')
    handler.send_header('Content-Security-Policy', 'sandbox')


def answer(handler, status, value):
    body = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    security_headers(handler)
    handler.end_headers()
    if handler.command != 'HEAD':
        handler.wfile.write(body)


def length(handler, maximum):
    value = handler.headers.get('Content-Length', '')
    if handler.headers.get('Transfer-Encoding') or not re.fullmatch(r'[0-9]+', value):
        raise ShareError(400, 'Send one Content-Length and an unframed body.')
    if hasattr(handler.headers, 'get_all') and len(handler.headers.get_all('Content-Length', [])) != 1:
        raise ShareError(400, 'Send one Content-Length.')
    if len(value.lstrip('0')) > 20:
        raise ShareError(413, 'That request exceeds the size limit.')
    size = int(value.lstrip('0') or '0')
    if size > maximum:
        raise ShareError(413, 'That request exceeds the size limit.')
    return size


def read_body(handler, maximum):
    size = length(handler, maximum)
    body = bytearray()
    while len(body) < size:
        chunk = handler.rfile.read(min(CHUNK, size - len(body)))
        if not chunk:
            raise ShareError(400, 'The request stopped before the declared length.')
        body.extend(chunk)
    return bytes(body)


def byte_range(header, size):
    if not header or not header.startswith('bytes=') or ',' in header:
        return None
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or size == 0:
        raise ShareError(416, 'That byte range is not available.')
    first, last = match.groups()
    if len(first) > 20 or len(last) > 20:
        raise ShareError(416, 'That byte range is not available.')
    if not first:
        if not last or int(last) == 0:
            raise ShareError(416, 'That byte range is not available.')
        return max(0, size - int(last)), size - 1
    first, last = int(first), int(last) if last else size - 1
    if first >= size or last < first:
        raise ShareError(416, 'That byte range is not available.')
    return first, min(last, size - 1)


def disposition(name):
    plain = re.sub(r'[^A-Za-z0-9._ ()-]', '_', name) or 'file'
    return 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (plain, quote(name, safe=''))


def send_file(handler, store, identifier, query, thumb=False):
    with store.connection() as db:
        row = store.row(db, identifier, OWNER)
        if thumb and not row['thumb_mime']:
            raise ShareError(404, 'That file has no thumbnail.')
        path = store.root / 'thumbs' / identifier if thumb else store.object_path(row)
        try:
            stream = store.open_private(path)
        except OSError as exc:
            raise ShareError(404, 'That file is not available.') from exc
    with stream:
        size = os.fstat(stream.fileno()).st_size
        text = not thumb and query.get('text', ['0'])[0] == '1'
        if text:
            if row['kind'] != 'text':
                raise ShareError(400, 'Text preview is available for text files only.')
            size = min(size, 256 * 1024)
        mime = row['thumb_mime'] if thumb else ('text/plain; charset=utf-8' if text else row['inline_mime'])
        attachment = query.get('download', ['0'])[0] == '1' or not mime
        if attachment:
            mime = 'application/octet-stream'
        try:
            span = byte_range(handler.headers.get('Range'), size)
        except ShareError as exc:
            body = json.dumps({'error': str(exc)}).encode()
            handler.send_response(416)
            handler.send_header('Content-Range', 'bytes */%d' % size)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(body)))
            security_headers(handler)
            handler.end_headers()
            if handler.command != 'HEAD':
                handler.wfile.write(body)
            return
        first, last = span or (0, size - 1)
        handler.send_response(206 if span else 200)
        handler.send_header('Content-Type', mime)
        handler.send_header('Content-Length', str(max(0, last - first + 1)))
        handler.send_header('Accept-Ranges', 'bytes')
        handler.send_header('Content-Disposition', disposition(row['name']) if attachment else 'inline')
        if span:
            handler.send_header('Content-Range', 'bytes %d-%d/%d' % (first, last, size))
        security_headers(handler)
        handler.end_headers()
        if handler.command == 'HEAD':
            return
        stream.seek(first)
        left = last - first + 1
        while left:
            chunk = stream.read(min(CHUNK, left))
            if not chunk:
                handler.close_connection = True
                break
            handler.wfile.write(chunk)
            left -= len(chunk)


def serve_get(handler, store, action, query):
    authorize(OWNER, {'owner_id': OWNER})
    if action == 'list' and handler.command == 'GET':
        one = lambda key, default='': query.get(key, [default])[0]
        if one('trash', '0') not in ('0', '1'):
            raise ShareError(400, 'Choose trash 0 or 1.')
        share_previews.backfill(store)
        answer(handler, 200, store.list(one('kind', 'all'), one('q'), one('trash', '0') == '1', one('cursor') or None,
                                        one('limit', '60'), source=one('source', 'all')))
    elif action == 'previews' and handler.command == 'GET':
        answer(handler, 200, {'tools': share_previews.tools()})
    elif action.startswith('raw/'):
        send_file(handler, store, action[4:], query)
    elif action.startswith('thumb/') and handler.command == 'GET':
        send_file(handler, store, action[6:], query, thumb=True)
    else:
        raise ShareError(404, 'That Share route is not available.')


def serve_post(handler, store, action):
    authorize(OWNER, {'owner_id': OWNER})
    if action == 'upload':
        size = length(handler, store.max_bytes)
        name = handler.headers.get('X-File-Name', '')
        if name.lower().startswith("utf-8''"):
            name = name[7:]
        if not name or re.search(r'%(?![0-9a-fA-F]{2})', name):
            raise ShareError(400, 'Send a percent-encoded UTF-8 file name.')
        try:
            name = unquote(name, encoding='utf-8', errors='strict')
        except UnicodeError as exc:
            raise ShareError(400, 'Send a percent-encoded UTF-8 file name.') from exc
        file = store.put(handler.rfile, size, name, handler.headers.get('X-File-Type', ''),
                         **{k: handler.headers.get('X-' + k.title()) for k in ('width', 'height', 'duration')})
        share_previews.after_upload(store, file['id'])
        answer(handler, 200, {'file': file})
        return
    if action.startswith('thumb/'):
        body = read_body(handler, THUMB_LIMIT)
        file = store.thumbnail(action[6:], body, handler.headers.get('Content-Type', '').split(';')[0])
        answer(handler, 200, {'file': file})
        return
    if action not in ('rename', 'delete', 'restore', 'purge', 'to-chat'):
        raise ShareError(404, 'That Share route is not available.')
    try:
        body = json.loads(read_body(handler, 64 * 1024))
    except (ValueError, UnicodeError) as exc:
        raise ShareError(400, 'Send a JSON object.') from exc
    if not isinstance(body, dict):
        raise ShareError(400, 'Send a JSON object.')
    if action == 'rename':
        result = {'ok': True, 'files': [store.rename(body.get('id'), body.get('name'))]}
    elif action == 'to-chat':
        result = {'path': store.export(body.get('id'))}
    else:
        result = store.change(action, body.get('ids'))
    answer(handler, 200, result)


def dispatch(handler):
    """Return False for another API. Authenticate before reading a Share request."""
    parsed = urlsplit(handler.path)
    if not parsed.path.startswith(PREFIX):
        return False
    if handler.command == 'POST':
        handler.close_connection = True  # rejected or partial bodies never become another request
    if handler.deny():
        return True
    if handler.command == 'POST' and (not auth.same_origin(handler.headers.get('Origin', ''), handler.headers.get('Host', ''))
                                     or handler.headers.get('X-Codex-Mobile') != '1'):
        answer(handler, 403, {'error': 'Reload the page and try again.'})
        return True
    try:
        store = get_store()
        action = parsed.path[len(PREFIX):]
        if handler.command == 'POST':
            serve_post(handler, store, action)
        else:
            serve_get(handler, store, action, parse_qs(parsed.query, keep_blank_values=True))
    except ShareError as exc:
        answer(handler, exc.status, {'error': str(exc)})
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True
    except (OSError, sqlite3.Error):
        answer(handler, 507, {'error': 'Share could not access its disk storage.'})
    return True
