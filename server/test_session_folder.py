"""New session routes honor the shared folder without changing resume or split routes."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import prefs_sync
import server


class SessionFolderRoutes(unittest.TestCase):
    def request(self, route):
        handler = object.__new__(server.Handler)
        handler.path = route
        body = json.dumps({'name': 'test-session'}).encode()
        handler.headers = {'Origin': 'http://localhost', 'Host': 'localhost',
                           'X-Codex-Mobile': '1', 'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.deny = Mock(return_value=False)
        handler.respond = Mock()
        handler.do_POST()
        return handler.respond.call_args.args

    def test_both_new_session_routes_use_the_saved_folder(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(prefs_sync, 'FILE', Path(folder) / 'prefs.json'), \
                patch.object(server, 'create_session', return_value='%99') as create, \
                patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '$99\n')):
            prefs_sync.save({'sessionFolder': folder})
            for route, status in (('/mobile/api/session', 200), ('/mobile/api/tmux-sessions', 201)):
                with self.subTest(route=route):
                    self.assertEqual(self.request(route)[0], status)
                    create.assert_called_with('test-session', folder)

    def test_missing_saved_folder_refuses_creation(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(prefs_sync, 'FILE', Path(folder) / 'prefs.json'), \
                patch.object(server, 'create_session') as create:
            target = Path(folder) / 'project'
            target.mkdir()
            prefs_sync.save({'sessionFolder': str(target)})
            target.rmdir()
            for route in ('/mobile/api/session', '/mobile/api/tmux-sessions'):
                status, body = self.request(route)
                self.assertEqual(status, 400)
                self.assertIn('default session folder', body['error'])
            create.assert_not_called()
