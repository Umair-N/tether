"""A private Xvfb for the signed-in browser. No shared display or TCP listener."""
import os
from pathlib import Path
import secrets
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import time


class VirtualDisplay:
    def __init__(self, finder=lambda: shutil.which('Xvfb')):
        self.finder = finder
        self.process = None
        self.folder = None
        self.environment = None

    @staticmethod
    def _authority(path, number, cookie):
        # FamilyWild avoids hostname differences. The server reads the cookie before displayfd selects a number;
        # the client file then gets that exact display number. No xauth dependency or access-control bypass.
        fields = (b'', number.encode(), b'MIT-MAGIC-COOKIE-1', cookie)
        path.write_bytes(struct.pack('!H', 65535) + b''.join(struct.pack('!H', len(f)) + f for f in fields))
        path.chmod(0o600)

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return self.environment
        self.stop()
        executable = self.finder()
        if not executable:
            raise RuntimeError('Xvfb is not installed.')
        self.folder = tempfile.TemporaryDirectory(prefix='tether-signed-display-')
        authority = Path(self.folder.name) / 'Xauthority'
        cookie = secrets.token_bytes(16)
        self._authority(authority, '', cookie)
        read_fd, write_fd = os.pipe()
        try:
            self.process = subprocess.Popen(
                [executable, '-displayfd', str(write_fd), '-screen', '0', '2560x1600x24',
                 '-nolisten', 'tcp', '-noreset', '-auth', str(authority)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(write_fd,), start_new_session=True)
            os.close(write_fd)
            write_fd = None
            deadline = time.monotonic() + 10
            number = b''
            while b'\n' not in number and time.monotonic() < deadline:
                ready, _, _ = select.select([read_fd], [], [], max(0, deadline - time.monotonic()))
                if not ready:
                    break
                chunk = os.read(read_fd, 32)
                if not chunk:
                    break
                number += chunk
            if not number.strip().isdigit() or b'\n' not in number or self.process.poll() is not None:
                raise RuntimeError('The signed-in browser display did not start.')
            number = number.strip().decode('ascii')
            self._authority(authority, number, cookie)
            self.environment = {'DISPLAY': ':' + number, 'XAUTHORITY': str(authority)}
            return self.environment
        except (OSError, RuntimeError):
            self.stop()
            raise
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

    def stop(self):
        process, self.process = self.process, None
        try:
            if process is not None:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
        except ProcessLookupError:
            process.wait(timeout=3)
        finally:
            if self.folder:
                self.folder.cleanup()
                self.folder = None
            self.environment = None
