"""Optional, per-viewer JPEG to H.264 encoder. No shared or persistent processes."""
import shutil
import subprocess
import threading
import time


def jpeg_size(data):
    """Read dimensions from a JPEG SOF segment without decoding the picture."""
    if not data.startswith(b'\xff\xd8'):
        raise ValueError('Not a JPEG.')
    pos = 2
    while pos < len(data):
        if data[pos] != 0xff:
            break
        while pos < len(data) and data[pos] == 0xff:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker in (0xd9, 0xda, 0):
            break
        if marker == 0x01 or 0xd0 <= marker <= 0xd7:
            continue
        if pos + 2 > len(data):
            break
        length = int.from_bytes(data[pos:pos + 2], 'big')
        if length < 2 or pos + length > len(data):
            break
        if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
            if length < 8:
                break
            height = int.from_bytes(data[pos + 3:pos + 5], 'big')
            width = int.from_bytes(data[pos + 5:pos + 7], 'big')
            if width and height:
                return width, height
            break
        pos += length
    raise ValueError('JPEG has no valid size.')


def _element(ident, data):
    return ident + ((1 << 56) | len(data)).to_bytes(8, 'big') + data


def _uint(ident, value):
    return _element(ident, value.to_bytes(8, 'big'))


def matroska_header(width, height):
    ebml = _element(b'\x1a\x45\xdf\xa3', _element(b'\x42\x82', b'matroska')
                    + _uint(b'\x42\x87', 4) + _uint(b'\x42\x85', 2))
    info = _element(b'\x15\x49\xa9\x66', _uint(b'\x2a\xd7\xb1', 1_000_000))
    video = _element(b'\xe0', _uint(b'\xb0', width) + _uint(b'\xba', height))
    track = _element(b'\xae', _uint(b'\xd7', 1) + _uint(b'\x73\xc5', 1) + _uint(b'\x83', 1)
                     + _element(b'\x86', b'V_MJPEG') + video)
    return (ebml + b'\x18\x53\x80\x67\x01\xff\xff\xff\xff\xff\xff\xff'
            + info + _element(b'\x16\x54\xae\x6b', track))


def matroska_cluster(tick, jpeg):
    return _element(b'\x1f\x43\xb6\x75', _uint(b'\xe7', tick)
                    + _element(b'\xa3', b'\x81\x00\x00\x80' + jpeg))


def _exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def flv_tags(stream):
    """Yield (tag type, millisecond timestamp, body), even with split pipe reads."""
    head = _exact(stream, 9)
    if head[:3] != b'FLV' or int.from_bytes(head[5:9], 'big') < 9:
        raise ValueError('Invalid FLV header.')
    _exact(stream, int.from_bytes(head[5:9], 'big') - 9 + 4)
    while True:
        head = _exact(stream, 11)
        size = int.from_bytes(head[1:4], 'big')
        tick = int.from_bytes(head[4:7], 'big') | (head[7] << 24)
        body = _exact(stream, size)
        _exact(stream, 4)
        yield head[0], tick, body


class Encoder:
    def __init__(self, on_message, finder=shutil.which, popen=subprocess.Popen):
        self.on_message, self.finder, self.popen = on_message, finder, popen
        self.session = None
        self.closed = False
        self.failed = False
        self.lock = threading.Lock()

    def _fail(self, session, error):
        with self.lock:
            if self.closed or self.failed or (session is not None and session['stop'].is_set()):
                return
            self.failed = True
        # Unblock a pipe write as well as the reader if ffmpeg stalls.
        if session is not None and session['process'].poll() is None:
            try:
                session['process'].kill()
            except OSError:
                pass
        self.on_message('failed', {'error': error}, b'')

    def _start(self, size):
        executable = self.finder('ffmpeg')
        if not executable:
            self._fail(None, 'ffmpeg is unavailable.')
            return None
        command = [executable, '-loglevel', 'error', '-fflags', 'nobuffer', '-probesize', '32',
                   '-analyzeduration', '0', '-threads', '1', '-f', 'matroska', '-i', 'pipe:0',
                   '-fps_mode', 'passthrough', '-vf', 'crop=trunc(iw/2)*2:trunc(ih/2)*2',
                   '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-qp', '22',
                   # Constant QP: a repeated picture encodes as almost nothing (CRF re-sent it sharper, ~140 KB).
                   # A wide search finds scroll jumps of up to 512 device pixels (270 KB -> 95 KB per scroll frame).
                   '-x264-params', 'me=umh:merange=512:subme=0',
                   '-bf', '0', '-g', '600', '-pix_fmt', 'yuv420p', '-threads', '4',
                   '-flush_packets', '1', '-f', 'flv', 'pipe:1']
        process = self.popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        session = {'process': process, 'size': size, 'tick': 2, 'pending': {}, 'stop': threading.Event(),
                   'stderr': bytearray(), 'threads': []}
        self.session = session
        for name, target in (('reader', self._read), ('stderr', self._stderr), ('watchdog', self._watch)):
            thread = threading.Thread(target=target, args=(session,), daemon=True, name='h264-' + name)
            session['threads'].append(thread)
            thread.start()
        process.stdin.write(matroska_header(*size))
        return session

    def push(self, jpeg_bytes, metadata_header):
        size = jpeg_size(jpeg_bytes)
        if self.closed or self.failed:
            return
        try:
            if self.session is None or self.session['size'] != size:
                self._stop()
                self._start(size)
            session = self.session
            if session is None or self.failed:
                return
            tick = session['tick']
            session['tick'] += 2
            with self.lock:
                now = time.monotonic()
                session['pending'][tick] = (dict(metadata_header), False, now)
                session['pending'][tick + 1] = (dict(metadata_header), True, now)
                if tick == 2:
                    # If ffmpeg ever outputs the prime, it is the key frame: the client must get it.
                    session['pending'][0] = (dict(metadata_header), True, now)
            # nobuffer discards the packet used to probe MJPEG. Reserve tick 0 for that
            # startup packet, then number real frames n=1.. at 2n and duplicates at 2n+1.
            prime = matroska_cluster(0, jpeg_bytes) if tick == 2 else b''
            session['process'].stdin.write(prime + matroska_cluster(tick, jpeg_bytes)
                                             + matroska_cluster(tick + 1, jpeg_bytes))
            session['process'].stdin.flush()
        except (OSError, ValueError) as exc:
            self._fail(self.session, str(exc))

    def _read(self, session):
        try:
            for tag, tick, body in flv_tags(session['process'].stdout):
                if session['stop'].is_set():
                    return
                if tag != 9 or len(body) < 5 or body[0] & 0x0f != 7:
                    continue
                if body[1] == 0:
                    avcc = body[5:]
                    if len(avcc) < 4:
                        raise ValueError('Invalid AVC configuration.')
                    width, height = session['size']
                    self.on_message('config', {'codec': 'avc1.' + avcc[1:4].hex(),
                                              'width': width // 2 * 2, 'height': height // 2 * 2}, avcc)
                elif body[1] == 1:
                    with self.lock:
                        item = session['pending'].get(tick)
                        # The decoder holds at most the newest duplicate; prune consumed ticks.
                        session['pending'] = {t: value for t, value in session['pending'].items() if t > tick}
                    if item:
                        header, duplicate, started = item
                        self.on_message('video', dict(header, key=body[0] >> 4 == 1, dup=duplicate,
                                                      encodeMs=round((time.monotonic() - started) * 1000)), body[5:])
        except (EOFError, OSError, ValueError) as exc:
            self._fail(session, str(exc) or 'ffmpeg exited.')

    def _stderr(self, session):
        try:
            while not session['stop'].is_set():
                data = session['process'].stderr.read(1024)
                if not data:
                    return
                session['stderr'].extend(data)
                del session['stderr'][:-8192]
        except (OSError, ValueError):
            pass

    def _watch(self, session):
        while not session['stop'].wait(.05):
            with self.lock:
                overdue = any(not dup and time.monotonic() - started >= 2
                              for _, dup, started in session['pending'].values())
            if session['process'].poll() is not None or overdue:
                self._fail(session, 'ffmpeg exited or produced no video for 2 seconds.')
                return

    def _stop(self):
        session, self.session = self.session, None
        if session is None:
            return
        session['stop'].set()
        process = session['process']
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        for thread in session['threads']:
            if thread is not threading.current_thread():
                thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()

    def close(self):
        self.closed = True
        self._stop()
