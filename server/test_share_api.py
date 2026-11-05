"""Real private-port Handler tests. The live server and tmux are never used."""
import http.client
import json
from pathlib import Path
import socket
import threading
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import auth
import paths
import server
import share_api


class ShareAPItests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'share'
        for item in (patch.object(auth, 'MODE', 'proxy'), patch.object(paths, 'SHARE', self.root), patch.object(share_api, '_store', None)):
            item.start()
            self.addCleanup(item.stop)
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def request(self, method, action, body=None, headers=None):
        sent = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1'}
        sent.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_address[1], timeout=5)
        try:
            connection.request(method, share_api.PREFIX + action, body, sent)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def upload(self, data=b'hello world', name='hello.txt', mime='text/plain'):
        status, _, body = self.request('POST', 'upload', data, {'X-File-Name': name, 'X-File-Type': mime})
        self.assertEqual(status, 200, body)
        return json.loads(body)['file']

    def post_json(self, action, body):
        return self.request('POST', action, json.dumps(body).encode())

    def test_upload_list_raw_range_and_head(self):
        file = self.upload()
        status, _, body = self.request('GET', 'list')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['files'], [file])
        status, headers, body = self.request('GET', 'raw/' + file['id'], headers={'Range': 'bytes=1-4'})
        self.assertEqual((status, body), (206, b'ello'))
        self.assertEqual(headers['Content-Range'], 'bytes 1-4/11')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        for range_header, expected in [('bytes=-3', b'rld'), ('bytes=6-', b'world')]:
            status, _, body = self.request('GET', 'raw/' + file['id'], headers={'Range': range_header})
            self.assertEqual((status, body), (206, expected))
        for header in ('bytes=99-', 'bytes=5-2', 'bytes=-0', 'bytes=bad', 'bytes=' + '9' * 5000 + '-'):
            status, headers, body = self.request('GET', 'raw/' + file['id'], headers={'Range': header})
            self.assertEqual(status, 416)
            self.assertEqual(headers['Content-Range'], 'bytes */11')
            self.assertIn('error', json.loads(body))
        status, headers, body = self.request('HEAD', 'raw/' + file['id'])
        self.assertEqual((status, body, headers['Content-Length']), (200, b'', '11'))

    def test_html_svg_and_claimed_images_are_attachments(self):
        for name, mime, data in [('attack.html', 'text/html', b'<script>bad</script>'),
                                  ('attack.svg', 'image/svg+xml', b'<svg onload="bad"/>'),
                                  ('fake.png', 'image/png', b'<html>bad</html>')]:
            file = self.upload(data, name, mime)
            status, headers, body = self.request('GET', 'raw/' + file['id'])
            self.assertEqual((status, body), (200, data))
            self.assertEqual(headers['Content-Type'], 'application/octet-stream')
            self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(headers['Content-Security-Policy'], 'sandbox')
            self.assertIn('private', headers['Cache-Control'])
        status, headers, body = self.request('GET', 'raw/' + file['id'] + '?text=1')
        self.assertEqual(status, 400)

    def test_safe_media_inline_download_and_bounded_text(self):
        for name, mime, data in [('pic.png', 'image/png', b'\x89PNG\r\n\x1a\ncontent'),
                                  ('a.pdf', 'application/pdf', b'%PDF-1.7\n'),
                                  ('a.mp4', 'video/mp4', b'\x00\x00\x00\x18ftypisom0000'),
                                  ('a.wav', 'audio/wav', b'RIFF0000WAVE0000')]:
            file = self.upload(data, name, mime)
            status, headers, _ = self.request('GET', 'raw/' + file['id'])
            self.assertEqual((status, headers['Content-Type'], headers['Content-Disposition']), (200, mime, 'inline'))
            status, headers, _ = self.request('GET', 'raw/' + file['id'] + '?download=1')
            self.assertEqual(headers['Content-Type'], 'application/octet-stream')
            self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
        file = self.upload(b'x' * (256 * 1024 + 1))
        status, headers, body = self.request('GET', 'raw/' + file['id'] + '?text=1')
        self.assertEqual((status, len(body)), (200, 256 * 1024))
        self.assertTrue(headers['Content-Type'].startswith('text/plain'))
        empty = self.upload(b'')
        status, _, _ = self.request('GET', 'raw/' + empty['id'], headers={'Range': 'bytes=-1'})
        self.assertEqual(status, 416)

    def test_mutations_thumbnails_traversal_names_and_chat_export(self):
        file = self.upload(name="UTF-8''..%2F..%2Fr%C3%A9sum%C3%A9.txt")
        self.assertEqual(file['name'], 'résumé.txt')
        status, _, body = self.post_json('rename', {'id': file['id'], 'name': '../../new.txt'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['files'][0]['name'], 'new.txt')
        status, _, body = self.post_json('to-chat', {'id': file['id']})
        self.assertEqual(status, 200)
        export = Path(json.loads(body)['path'])
        self.assertEqual(export, self.root / 'exports' / file['id'] / 'new.txt')
        self.assertEqual(export.read_bytes(), b'hello world')
        jpeg = b'\xff\xd8\xffthumbnail'
        status, _, body = self.request('POST', 'thumb/' + file['id'], jpeg, {'Content-Type': 'image/jpeg'})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['file']['has_thumb'])
        status, headers, body = self.request('GET', 'thumb/' + file['id'])
        self.assertEqual((status, headers['Content-Type'], body), (200, 'image/jpeg', jpeg))
        for action in ('delete', 'restore', 'delete', 'purge'):
            self.assertEqual(self.post_json(action, {'ids': [file['id']]})[0], 200)
            if action == 'delete':
                self.assertEqual(self.request('GET', 'raw/' + file['id'])[0], 404)
                self.assertEqual(len(json.loads(self.request('GET', 'list?trash=1')[2])['files']), 1)
        self.assertFalse(export.exists())
        self.assertEqual(self.request('GET', 'thumb/' + file['id'])[0], 404)

    def test_unauthorized_get_head_post_and_csrf(self):
        with patch.object(auth, 'signed_in', return_value=False), patch.object(auth, 'tailscale_signed_in', return_value=False):
            for method, route in [('GET', 'list'), ('GET', 'raw/missing'), ('HEAD', 'raw/missing'), ('POST', 'upload'), ('POST', 'delete')]:
                self.assertEqual(self.request(method, route, b'x' if method == 'POST' else None)[0], 401)
        for headers in ({'X-Codex-Mobile': ''}, {'Origin': 'http://evil.test'}, {'Origin': ''}):
            self.assertEqual(self.request('POST', 'upload', b'x', headers)[0], 403)
        # http.client does not inject the application header.
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_address[1], timeout=5)
        try:
            connection.request('POST', share_api.PREFIX + 'delete', b'{}', {'Host': 'phone.test', 'Origin': 'http://phone.test'})
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()

    def test_bad_body_length_framing_limits_and_metadata(self):
        for headers, status in [({'Content-Length': '-1'}, 400), ({'Content-Length': '9' * 5000}, 413), ({'Transfer-Encoding': 'chunked'}, 400),
                                 ({'Content-Length': str(2 * 1024**3 + 1)}, 413),
                                 ({'X-Width': 'nan'}, 400), ({'X-File-Name': '%zz'}, 400)]:
            sent = {'X-File-Name': 'file.txt'}
            sent.update(headers)
            self.assertEqual(self.request('POST', 'upload', b'x', sent)[0], status)
        self.assertEqual(self.request('POST', 'rename', b'[]')[0], 400)
        self.assertEqual(self.post_json('delete', {'ids': [{}]})[0], 400)
        self.assertEqual(self.post_json('rename', {'id': 'missing', 'name': '\ud800'})[0], 400)
        self.assertEqual(self.request('POST', 'thumb/missing', b'x', {'Content-Type': 'image/jpeg', 'Content-Length': str(512 * 1024 + 1)})[0], 413)
        # Half-close the request so the server sees EOF rather than a client timeout.
        for with_length in (True, False):
            sock = socket.create_connection(self.http.server_address, timeout=5)
            try:
                headers = ('Content-Length: 10\r\n' if with_length else '')
                raw = ('POST /mobile/api/share/upload HTTP/1.0\r\nHost: phone.test\r\nOrigin: http://phone.test\r\n'
                       'X-Codex-Mobile: 1\r\nX-File-Name: short.txt\r\n' + headers + '\r\nshort').encode()
                sock.sendall(raw)
                sock.shutdown(socket.SHUT_WR)
                response = http.client.HTTPResponse(sock)
                response.begin()
                self.assertEqual(response.status, 400)
                self.assertIn('error', json.loads(response.read()))
            finally:
                sock.close()
