"""Optional, bounded server previews. Missing tools never block Share uploads."""
import json
import os
import queue
import resource
import selectors
import shutil
import signal
import sqlite3
import subprocess
import threading
import time

from share_store import OWNER, THUMB_LIMIT, ShareError, metadata, raster_type

TIMEOUT = 30
_queue = queue.Queue(maxsize=64)
_lock = threading.Lock()
_thread = None
_pending = set()
_failed = set()
_backfills = {}
_COMMON = ['-nostdin', '-hide_banner', '-loglevel', 'error', '-protocol_whitelist', 'file']
_SCALE = "scale=w='min(480,iw)':h='min(480,ih)':force_original_aspect_ratio=decrease"
_UNSUPPORTED_IMAGES = {
    'image/heic', 'image/heif', 'image/heic-sequence', 'image/heif-sequence',
    'image/tiff', 'image/bmp', 'image/x-bmp', 'image/x-ms-bmp',
}


def _paths():
    return {name: shutil.which(os.environ.get('TETHER_' + name.upper(), name))
            for name in ('ffmpeg', 'ffprobe', 'pdftoppm')}


def tools():
    return {name: bool(path) for name, path in _paths().items()}


def _limits():
    os.nice(10)
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def _run(arguments):
    """Drain only bounded stdout; kill the whole session on failure or timeout."""
    with subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, preexec_fn=_limits, start_new_session=True) as process:
        deadline = time.monotonic() + TIMEOUT
        output = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise subprocess.TimeoutExpired(arguments, TIMEOUT)
                    chunk = os.read(process.stdout.fileno(), min(65536, THUMB_LIMIT + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > THUMB_LIMIT:
                        raise ValueError('Preview output exceeds 512 KiB.')
            if process.wait(timeout=max(0, deadline - time.monotonic())):
                raise ValueError('Preview tool failed.')
            return bytes(output)
        finally:
            # Also stop descendants that outlive a tool or keep its stdout open.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=TIMEOUT)


def _image_thumbnail(row):
    return row['size'] > 2 * 1024**2 or row['mime'] in _UNSUPPORTED_IMAGES


def _needs(row):
    if row['kind'] == 'video':
        return not row['thumb_mime'] or any(row[k] is None for k in ('width', 'height', 'duration'))
    if row['kind'] == 'audio':
        return row['duration'] is None
    if row['kind'] == 'pdf':
        return not row['thumb_mime']
    if row['kind'] == 'image':
        return (not row['thumb_mime'] and _image_thumbnail(row)) or any(row[k] is None for k in ('width', 'height'))
    return False


def _preview(store, identifier):
    available = _paths()
    with store.connection() as db:
        row = dict(store.row(db, identifier))
    if not _needs(row):
        return
    path = store.object_path(row)
    with store.open_private(path):
        pass
    kind = row['kind']
    values = {}
    fields = ('duration',) if kind == 'audio' else (('width', 'height') if kind == 'image' else ('width', 'height', 'duration'))
    if kind in ('video', 'audio', 'image') and available['ffprobe'] and any(row[k] is None for k in fields):
        probe = json.loads(_run([available['ffprobe'], *_COMMON, '-show_entries',
                                'format=duration:stream=codec_type,width,height,duration', '-of', 'json', 'file:' + str(path)]))
        if not isinstance(probe, dict) or not isinstance(probe.get('format', {}), dict):
            raise ValueError('Invalid preview metadata.')
        streams = probe.get('streams', [])
        if not isinstance(streams, list) or any(not isinstance(item, dict) for item in streams):
            raise ValueError('Invalid preview streams.')
        stream = next((item for item in streams if item.get('codec_type') == ('audio' if kind == 'audio' else 'video')), {})
        raw = {'duration': probe.get('format', {}).get('duration', stream.get('duration')),
               'width': stream.get('width'), 'height': stream.get('height')}
        values = metadata({key: raw[key] for key in fields if raw[key] not in (None, 'N/A')})
        store.fill_metadata(identifier, **values)
    body = None
    if not row['thumb_mime']:
        if kind == 'pdf' and available['pdftoppm']:
            body = _run([available['pdftoppm'], '-f', '1', '-l', '1', '-scale-to', '480',
                         '-jpeg', '-jpegopt', 'quality=80', str(path)])
        elif available['ffmpeg'] and (kind == 'video' or (kind == 'image' and _image_thumbnail(row))):
            seek = []
            if kind == 'video':
                duration = row['duration'] if row['duration'] is not None else values.get('duration', 0)
                seek = ['-ss', str(min(1, duration / 3))]
            body = _run([available['ffmpeg'], *_COMMON, *seek, '-i', 'file:' + str(path),
                         '-frames:v', '1', '-vf', _SCALE, '-c:v', 'mjpeg', '-q:v', '5', '-f', 'image2pipe', 'pipe:1'])
    if body is not None:
        if len(body) > THUMB_LIMIT or raster_type(body) != 'image/jpeg':
            raise ValueError('Preview is not a bounded JPEG.')
        store.thumbnail_if_missing(identifier, body, 'image/jpeg')


def _worker():
    while True:
        job = _queue.get()
        try:
            if job is None:
                return
            store, identifier = job
            key = (str(store.root), identifier)
            try:
                _preview(store, identifier)
            except (OSError, ValueError, TypeError, KeyError, ShareError, sqlite3.Error, subprocess.SubprocessError):
                with _lock:
                    _failed.add(key)
            finally:
                with _lock:
                    _pending.discard(key)
        finally:
            _queue.task_done()


def enqueue(store, file_id):
    global _thread
    if not any(tools().values()):
        return
    key = (str(store.root), file_id)
    with _lock:
        if key in _failed or key in _pending:
            return
        try:
            _queue.put_nowait((store, file_id))
        except queue.Full:
            return
        _pending.add(key)
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name='share-previews', daemon=True)
            _thread.start()


def after_upload(store, file_id):
    enqueue(store, file_id)


def backfill(store):
    if not any(tools().values()):
        return
    root, now = str(store.root), time.monotonic()
    with _lock:
        if root in _backfills and now - _backfills[root] < 60:
            return
        _backfills[root] = now
        failed = _failed.copy()
    with store.connection() as db:
        rows = db.execute("SELECT * FROM files WHERE owner_id=? AND deleted_at IS NULL AND kind IN ('video','audio','pdf','image') ORDER BY created DESC, id DESC", (OWNER,))
        count = 0
        for row in rows:
            if (root, row['id']) not in failed and _needs(row):
                enqueue(store, row['id'])
                count += 1
                if count == 100:
                    break
