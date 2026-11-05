import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class ShareCLITests(unittest.TestCase):
    def test_put_list_stat_path_and_errors(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            env = dict(os.environ, TETHER_HOME=str(root / 'home'), TETHER_SHARE=str(root / 'share'),
                       TETHER_SHARE_QUOTA_BYTES='100', TETHER_SHARE_MAX_BYTES='50')
            source = root / 'source.txt'
            source.write_bytes(b'from agent')
            def run(*args, code=0):
                result = subprocess.run([sys.executable, str(Path(__file__).with_name('agent_share_cli.py')), *args],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, code, result.stderr + result.stdout)
                self.assertEqual(result.stderr, '')
                self.assertEqual(len(result.stdout.splitlines()), 1)
                return json.loads(result.stdout)
            file = run('put', str(source), '--name', '../../renamed.txt')['file']
            self.assertEqual(file['name'], 'renamed.txt')
            self.assertEqual(run('list', '--kind', 'doc', '--limit', '1')['files'], [file])
            self.assertEqual(run('stat', file['id'])['file'], file)
            export = Path(run('path', file['id'])['path'])
            self.assertEqual(export.read_bytes(), b'from agent')
            self.assertEqual(export.stat().st_mode & 0o777, 0o600)
            self.assertIn('error', run('stat', 'missing', code=1))
            source.write_bytes(b'x' * 51)
            self.assertIn('error', run('put', str(source), code=1))
            self.assertIn('error', run('put', str(root), code=1))
