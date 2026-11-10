"""Optional ONLYOFFICE bridge. No Docker calls or listener until explicitly enabled."""
import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPException
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

import files
import paths

LOG = logging.getLogger(__name__)
LIMIT = 100 * 1024 * 1024
CHUNK = 1024 * 1024
TYPES = {'.docx': 'word', '.xlsx': 'cell', '.pptx': 'slide'}
_config_cache = {'path': None, 'until': 0, 'value': None}
_health_cache = {'url': None, 'until': 0, 'value': False}
_cache_lock = threading.Lock()
_state_lock = threading.Lock()
_locks = {}
_saved = {}
_copies = {}


def config_path():
    return Path(os.environ.get('TETHER_OFFICE_CONFIG') or paths.CONFIG / 'office.json')


def _port(value):
    return type(value) is int and 0 < value < 65536


def _valid_config(value):
    if not isinstance(value, dict):
        return False
    strings = ('url', 'secret', 'listen_host', 'public_host', 'container', 'image')
    if any(not isinstance(value.get(key), str) or not value[key] for key in strings):
        return False
    if not _port(value.get('listen_port')) or len(value['secret'].encode()) < 32:
        return False
    match = re.fullmatch(r'http://(127\.0\.0\.1|localhost):([0-9]+)', value['url'])
    if not match or not _port(int(match[2])):
        return False
    try:
        ipaddress.IPv4Address(value['listen_host'])
    except ValueError:
        return False
    return bool(re.fullmatch(r'[a-zA-Z0-9.-]+', value['public_host']))


def load():
    """Read at most once per five seconds, including missing and invalid files."""
    path, now = config_path(), time.monotonic()
    with _cache_lock:
        if _config_cache['path'] == path and now < _config_cache['until']:
            return _config_cache['value']
        value = None
        try:
            candidate = _read_json(path.read_bytes())
            if not _valid_config(candidate):
                raise ValueError('invalid settings')
            value = candidate
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            LOG.warning('Office config refused: %s', type(exc).__name__)
        _config_cache.update(path=path, until=now + 5, value=value)
        return value


def enabled():
    return load() is not None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(url, timeout):
    # Neither proxy environment variables nor redirects may change the destination.
    return build_opener(ProxyHandler({}), _NoRedirect()).open(url, timeout=timeout)


def _healthy(config):
    try:
        with _fetch(config['url'] + '/healthcheck', 2) as response:
            return response.status == 200 and response.read(6).strip() == b'true'
    except (OSError, ValueError, HTTPException):
        return False


def status():
    config = load()
    if config is None:
        return {'enabled': False, 'healthy': False}
    with _cache_lock:
        now = time.monotonic()
        if _health_cache['url'] != config['url'] or now >= _health_cache['until']:
            _health_cache.update(url=config['url'], until=now + 30, value=_healthy(config))
        return {'enabled': True, 'healthy': _health_cache['value']}


def _encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _decode(text):
    if not isinstance(text, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', text):
        raise ValueError('Invalid base64.')
    return base64.b64decode(text + '=' * (-len(text) % 4), altchars=b'-_', validate=True)


def _json(value):
    return json.dumps(value, separators=(',', ':'), allow_nan=False).encode()


def _bad_constant(value):
    raise ValueError('Invalid JSON constant.')


def _read_json(data):
    try:
        return json.loads(data, parse_constant=_bad_constant)
    except RecursionError as exc:
        raise ValueError('Invalid JSON nesting.') from exc


def _expiry(value):
    if type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)) or value <= time.time():
        raise ValueError('Expired token.')


def sign(claims, secret):
    content = _encode(_json({'alg': 'HS256', 'typ': 'JWT'})) + '.' + _encode(_json(claims))
    return content + '.' + _encode(hmac.digest(secret.encode(), content.encode(), 'sha256'))


def verify(token, secret):
    if not isinstance(token, str) or len(token) > 65536 or token.count('.') != 2:
        raise ValueError('Invalid JWT.')
    head, body, signature = token.split('.')
    header = _read_json(_decode(head))
    if not isinstance(header, dict) or header.get('alg') != 'HS256':
        raise ValueError('Invalid JWT algorithm.')
    expected = hmac.digest(secret.encode(), (head + '.' + body).encode(), 'sha256')
    if not hmac.compare_digest(expected, _decode(signature)):
        raise ValueError('Invalid JWT signature.')
    claims = _read_json(_decode(body))
    if not isinstance(claims, dict):
        raise ValueError('Invalid JWT claims.')
    if 'exp' in claims:
        _expiry(claims['exp'])
    return claims


def _cap_key(secret):
    return hmac.digest(secret.encode(), b'tether-office-capability', 'sha256')


def capability(payload, secret):
    body = _encode(_json(payload))
    return body + '.' + _encode(hmac.digest(_cap_key(secret), body.encode(), 'sha256'))


def verify_capability(token, secret):
    if not isinstance(token, str) or len(token) > 16384 or token.count('.') != 1:
        raise ValueError('Invalid capability.')
    body, signature = token.split('.')
    if not hmac.compare_digest(hmac.digest(_cap_key(secret), body.encode(), 'sha256'), _decode(signature)):
        raise ValueError('Invalid capability signature.')
    value = _read_json(_decode(body))
    if (not isinstance(value, dict) or not isinstance(value.get('p'), str)
            or not isinstance(value.get('k'), str) or not value['k']
            or any(type(value.get(key)) is not int or value[key] < 0 for key in ('m', 's'))):
        raise ValueError('Invalid capability payload.')
    _expiry(value.get('e'))
    return value


def _item(path_text):
    item = files.write_item(path_text, files.files_root())
    try:
        if not stat.S_ISREG(item.st.st_mode):
            raise files.invalid('That is not a file.')
        if item.st.st_size > LIMIT:
            raise files.FilesError(413, 'large', 'Tether edits files up to 100 MB.')
        if item.real.suffix.lower() not in TYPES:
            raise files.invalid('Tether edits only docx, xlsx and pptx files.')
        return item
    except Exception:
        item.close()
        raise


def _state(st):
    return st.st_mtime_ns, st.st_size


def open_document(path_text, user_name='', mobile=False, lang='en', theme='dark'):
    config = load()
    if config is None:
        raise files.FilesError(404, 'office-off', 'The Office editor add-on is not installed.')
    item = _item(path_text)
    try:
        key = 't' + hashlib.sha256(f'{item.real}|{item.st.st_mtime_ns}|{item.st.st_size}'.encode()).hexdigest()[:32]
        cap = capability({'p': files.shown(item.lex, files.files_root()), 'k': key,
                          'm': item.st.st_mtime_ns, 's': item.st.st_size, 'e': int(time.time()) + 72 * 3600}, config['secret'])
        base = f"http://{config['public_host']}:{config['listen_port']}/office/"
        suffix = item.real.suffix.lower()
        editor = {
            'document': {'fileType': suffix[1:], 'key': key, 'title': item.name, 'url': base + 'file/' + cap,
                         'permissions': dict.fromkeys(('edit', 'download', 'print', 'comment', 'review'), True)},
            'documentType': TYPES[suffix],
            'editorConfig': {
                'callbackUrl': base + 'callback/' + cap, 'mode': 'edit',
                'lang': lang if isinstance(lang, str) and re.fullmatch(r'[a-z]{2}(-[A-Z]{2})?', lang) else 'en',
                'user': {'id': 'tether', 'name': user_name or 'Tether'},
                'customization': {'forcesave': True, 'autosave': True, 'macros': False, 'plugins': False,
                                  'help': False, 'feedback': False, 'goback': False, 'uiTheme': 'default-light' if theme == 'light' else 'default-dark'}},
            'type': 'mobile' if mobile else 'desktop', 'width': '100%', 'height': '100%'}
        editor['token'] = sign(editor, config['secret'])
        return {'api': '/office/web-apps/apps/api/documents/api.js', 'config': editor}
    finally:
        item.close()


def _key_lock(key):
    with _state_lock:
        return _locks.setdefault(key, threading.Lock())


def _expected(cap, st):
    return _state(st) in ((cap['m'], cap['s']), _saved.get(cap['k']))


def _replace(item, data):
    """Install bytes atomically through the checked folder descriptor."""
    name = '.' + item.name + '.tether-' + secrets.token_hex(8)
    fd = os.open(name, files.NEW_FILE_FLAGS, 0o600, dir_fd=item.folder)
    try:
        os.fchmod(fd, stat.S_IMODE(item.st.st_mode))
        files.write_all(fd, data)
        os.fsync(fd)
        # Catch a target swap during the write, before publishing the new file.
        current = os.stat(item.name, dir_fd=item.folder, follow_symlinks=False)
        if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (
                item.st.st_dev, item.st.st_ino, item.st.st_mtime_ns, item.st.st_size):
            raise files.changed()
        os.rename(name, item.name, src_dir_fd=item.folder, dst_dir_fd=item.folder)
        return os.fstat(fd)
    finally:
        os.close(fd)
        try:
            os.unlink(name, dir_fd=item.folder)
        except FileNotFoundError:
            pass


def save(cap, data):
    if len(data) > LIMIT:
        raise ValueError('Office download is too large.')
    key = cap['k']
    with _key_lock(key):
        # Always recheck the original, even when a previous conflict created a copy.
        original = _item(cap['p'])
        target = original
        try:
            if key in _copies:
                target = _item(_copies[key])
            if _expected(cap, target.st):
                try:
                    _saved[key] = _state(_replace(target, data))
                    return
                except files.FilesError as exc:
                    if exc.code != 'changed':
                        raise
                    # Recheck swaps before creating a conflict copy. A symlink is still refused.
                    checked = _item(_copies.get(key, cap['p']))
                    checked.close()
            # Claim a sibling exclusively. Never overwrite an external edit, including to a copy.
            stem, ext = os.path.splitext(original.name)
            name, fd = files.claim(stem + ' (Tether edit)' + ext, False,
                                   lambda candidate: os.open(candidate, files.NEW_FILE_FLAGS,
                                                             0o600, dir_fd=original.folder))
            try:
                os.fchmod(fd, stat.S_IMODE(original.st.st_mode))
                files.write_all(fd, data)
                os.fsync(fd)
                saved = os.fstat(fd)
            except Exception:
                os.unlink(name, dir_fd=original.folder)
                raise
            finally:
                os.close(fd)
            _copies[key] = files.shown(original.lex.parent / name, files.files_root())
            _saved[key] = _state(saved)
            LOG.warning('Office external edit preserved; saved a Tether edit copy.')
        finally:
            if target is not original:
                target.close()
            original.close()


def download(url, config):
    if not isinstance(url, str):
        raise ValueError('Missing Office result URL.')
    parsed = urlsplit(url)
    path = parsed.path
    if path.startswith('/office/'):
        path = path[len('/office'):]
    decoded = unquote(path)
    if (not path.startswith('/cache/files/') or '\\' in decoded
            or any(part in ('.', '..') for part in decoded.split('/'))):
        raise ValueError('Invalid Office result path.')
    url = config['url'] + path + ('?' + parsed.query if parsed.query else '')
    with _fetch(url, 60) as response:
        length = response.headers.get('Content-Length')
        if length is not None and not 0 <= int(length) <= LIMIT:
            raise ValueError('Office download is too large.')
        data = bytearray()
        while chunk := response.read(min(CHUNK, LIMIT + 1 - len(data))):
            data.extend(chunk)
            if len(data) > LIMIT:
                raise ValueError('Office download is too large.')
        if length is not None and len(data) != int(length):
            raise ValueError('Incomplete Office download.')
        return data


def _bearer(headers, secret):
    header = headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        raise ValueError('Missing JWT.')
    return verify(header[7:], secret)


class OfficeHandler(BaseHTTPRequestHandler):
    timeout = 60

    def log_message(self, *args):
        pass  # URLs contain capabilities; do not put them in access logs.

    def answer(self, status, body=None):
        data = _json(body) if body is not None else b''
        self.send_response(status)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        self.answer(404 if code == 501 else code)

    def credentials(self, prefix):
        config = load()
        if not self.path.startswith(prefix) or config is None:
            self.answer(404)
            return None
        try:
            cap = verify_capability(self.path[len(prefix):], config['secret'])
            return config, cap
        except ValueError:
            self.answer(403)
            return None

    def do_GET(self):
        credentials = self.credentials('/office/file/')
        if credentials is None:
            return
        config, cap = credentials
        try:
            _bearer(self.headers, config['secret'])
        except ValueError:
            self.answer(403)
            return
        try:
            with _key_lock(cap['k']):
                item = _item(cap['p'])
                try:
                    fd = os.open(item.name, files.READ_FLAGS, dir_fd=item.folder)
                finally:
                    item.close()
                with os.fdopen(fd, 'rb') as stream:
                    st = os.fstat(stream.fileno())
                    if not stat.S_ISREG(st.st_mode) or not _expected(cap, st):
                        self.answer(409)
                        return
                    self.send_response(200)
                    self.send_header('Content-Length', str(st.st_size))
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    left = st.st_size
                    while left and (chunk := stream.read(min(CHUNK, left))):
                        self.wfile.write(chunk)
                        left -= len(chunk)
        except files.FilesError as exc:
            self.answer(exc.status)
        except OSError:
            self.close_connection = True

    def do_POST(self):
        credentials = self.credentials('/office/callback/')
        if credentials is None:
            return
        config, cap = credentials
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= CHUNK or self.headers.get('Transfer-Encoding'):
                self.answer(413)
                return
            body = _read_json(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('Invalid callback.')
            claims = verify(body['token'], config['secret']) if 'token' in body else _bearer(self.headers, config['secret']).get('payload')
            if not isinstance(claims, dict) or claims.get('key') != cap['k']:
                raise ValueError('Invalid callback key.')
        except (ValueError, OSError):
            self.answer(403)
            return
        result = 0
        if claims.get('status') in (3, 7):
            LOG.warning('Office reported a save error (status %s).', claims['status'])
        elif claims.get('status') in (2, 6):
            try:
                save(cap, download(claims.get('url'), config))
            except (OSError, ValueError, HTTPException) as exc:
                LOG.warning('Office save failed: %s', type(exc).__name__)
                result = 1
        self.answer(200, {'error': result})

    def do_HEAD(self):
        self.answer(404)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD


def start_listener():
    config = load()
    if config is None:
        return None
    try:
        http = ThreadingHTTPServer((config['listen_host'], config['listen_port']), OfficeHandler)
    except OSError as exc:
        LOG.warning('Office listener did not start: %s', type(exc).__name__)
        return None
    threading.Thread(target=http.serve_forever, name='office-listener', daemon=True).start()
    return http


def office_upstream_head(method, path, headers, client_ip):
    """Pure request rewrite for the authenticated browser proxy."""
    items = list(headers.items())
    lower = {key.lower(): value for key, value in items}
    upgrading = 'upgrade' in lower.get('connection', '').lower()
    drop = {'cookie', 'connection', 'x-forwarded-host', 'x-forwarded-prefix', 'x-forwarded-proto'}
    lines = [f'{method} {path[len("/office"):]} HTTP/1.1']
    lines += [f'{key}: {value}' for key, value in items if key.lower() not in drop and not key.lower().startswith('tailscale-')]
    lines.append('Connection: Upgrade' if upgrading else 'Connection: close')
    lines.append('X-Forwarded-Host: ' + lower.get('host', '') + '/office')
    try:
        loopback = ipaddress.ip_address(client_ip).is_loopback
    except ValueError:
        loopback = False
    lines.append('X-Forwarded-Proto: ' + ('https' if loopback and lower.get('x-forwarded-proto') == 'https' else 'http'))
    return lines


def write_config(config):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.office-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(_json(config))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    with _cache_lock:
        _config_cache['until'] = 0


def _docker(*args, **kwargs):
    return subprocess.run(['docker', *args], check=True, capture_output=True, text=True, timeout=30, **kwargs)


def _free_port(host, start, exact=False):
    for port in range(start, min(start + (1 if exact else 1000), 65536)):
        try:
            with socket.socket() as sock:
                sock.bind((host, port))
            return port
        except OSError:
            if exact:
                raise ValueError(f'Port {port} is not available.') from None
    raise ValueError('No free Office port was found.')


def install(args):
    if config_path().exists():
        raise ValueError('Office config exists. Run office_addon.py remove first.')
    try:
        _docker('info')
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Docker is not ready. Install and start Docker, then try again.') from None
    host = '127.0.0.1' if sys.platform == 'darwin' else _docker(
        'network', 'inspect', 'bridge', '-f', '{{(index .IPAM.Config 0).Gateway}}').stdout.strip()
    ipaddress.IPv4Address(host)
    port = _free_port('127.0.0.1', args.port or 7695, bool(args.port))
    listen_port = _free_port(host, args.listen_port or port + 1, bool(args.listen_port))
    if port == listen_port:
        raise ValueError('Use separate Office and listener ports.')
    secret = secrets.token_urlsafe(32)
    config = {'url': f'http://127.0.0.1:{port}', 'secret': secret, 'listen_host': host,
              'listen_port': listen_port, 'public_host': 'host.docker.internal',
              'container': 'tether-office', 'image': args.image}
    _docker('run', '-d', '--name', config['container'], '--restart', 'unless-stopped',
            '-p', f'127.0.0.1:{port}:80', '--add-host', 'host.docker.internal:host-gateway',
            '-e', 'JWT_SECRET', '-e', 'ALLOW_PRIVATE_IP_ADDRESS=true', '-e', 'PLUGINS_ENABLED=false',
            args.image, env=dict(os.environ, JWT_SECRET=secret))
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if _healthy(config):
            write_config(config)
            print('The Office editor is ready. Restart Tether to turn it on:\nsystemctl --user restart tether.service')
            return
        time.sleep(2)
    raise ValueError('Office did not become healthy in 180 seconds. Check docker logs tether-office; remove the container before retrying.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('install')
    setup.add_argument('--image', default='onlyoffice/documentserver:10.0')
    setup.add_argument('--port', type=int)
    setup.add_argument('--listen-port', type=int)
    commands.add_parser('status')
    commands.add_parser('remove')
    args = parser.parse_args(argv)
    try:
        if args.command == 'install':
            if any(value is not None and not _port(value) for value in (args.port, args.listen_port)):
                raise ValueError('Use ports from 1 to 65535.')
            install(args)
        elif args.command == 'remove':
            config = load()
            container = config['container'] if config else 'tether-office'
            try:
                _docker('rm', '-f', container)
            except subprocess.CalledProcessError as exc:
                if 'No such container' not in (exc.stderr or ''):
                    raise
            config_path().unlink(missing_ok=True)
            with _cache_lock:
                _config_cache['until'] = 0
            print('The Office editor is removed. Restart Tether:\nsystemctl --user restart tether.service')
        else:
            config = load()
            print('Config: ' + ('present' if config_path().exists() else 'absent'))
            if config:
                try:
                    print('Container: ' + _docker('inspect', '-f', '{{.State.Status}}', config['container']).stdout.strip())
                except (OSError, subprocess.SubprocessError):
                    print('Container: unavailable')
            print('Healthy: ' + ('yes' if status()['healthy'] else 'no'))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # CalledProcessError can include environment or Docker output; print no secret.
        print(str(exc) if isinstance(exc, ValueError) else f'Office command failed ({type(exc).__name__}).', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
