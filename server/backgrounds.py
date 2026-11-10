"""Private Glass backdrops and one bounded Codex image job at a time."""
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

import paths

PATH = '/mobile/api/backgrounds'
LIMIT = 12 * 1024 * 1024
MAX_ITEMS = 30
TIMEOUT = 300
ID = re.compile(r'[a-f0-9]{32}')
FILE = re.compile(r'([a-f0-9]{32})\.(png|jpg|webp)')
KINDS = {'png': 'image/png', 'jpg': 'image/jpeg', 'webp': 'image/webp'}
_lock = threading.Lock()
_jobs = {}


class Bad(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def handles(path):
    path = path.split('?', 1)[0]
    return path == PATH or path.startswith(PATH + '/')


def _load():
    try:
        items = json.loads((paths.BACKGROUNDS / 'index.json').read_text())
        if not isinstance(items, list):
            raise ValueError()
        return items
    except FileNotFoundError:
        return []
    except ValueError:
        raise Bad('Could not read the backgrounds list.', 503) from None


def _write(items):
    target = paths.BACKGROUNDS / 'index.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(items))
    os.replace(temporary, target)


def _kind(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if len(data) >= 16 and data[:4] == b'RIFF' and data[8:12] == b'WEBP' and data[12:16] in (b'VP8 ', b'VP8L', b'VP8X'):
        return 'webp'
    raise Bad('Choose a PNG, JPEG, or WebP image.')


def _dimensions(data, ext):
    if ext == 'png' and len(data) >= 24 and data[12:16] == b'IHDR':
        width, height = struct.unpack('>II', data[16:24])
        if width and height:
            return {'width': width, 'height': height}
    return {}


def _room(items, reserve=False):
    active = any(job['state'] == 'running' for job in _jobs.values()) if reserve else False
    if len(items) + int(active) >= MAX_ITEMS:
        raise Bad('You can keep 30 backgrounds. Delete one before adding another.', 409)


def save(data, content_type=None, source='upload', prompt=None):
    if not 0 < len(data) <= LIMIT:
        raise Bad('That image is empty or too large (the limit is 12 MB).', 413)
    ext = _kind(data)
    if content_type is not None and content_type != KINDS[ext]:
        raise Bad('The image does not match its Content-Type.')
    with _lock:
        items = _load()
        # Reserve the active job's last slot while an upload runs alongside it.
        _room(items, reserve=source == 'upload')
        paths.BACKGROUNDS.mkdir(parents=True, exist_ok=True, mode=0o700)
        ident = secrets.token_hex(16)
        filename = ident + '.' + ext
        target = paths.BACKGROUNDS / filename
        temporary = target.with_suffix('.tmp')
        item = {'id': ident, 'url': PATH + '/' + filename, 'source': source, 'created': time.time(),
                **_dimensions(data, ext)}
        if prompt is not None:
            item['prompt'] = prompt
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
            _write([*items, item])
        except OSError:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        return item


def delete(ident):
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        raise Bad('Invalid background id.')
    with _lock:
        items = _load()
        item = next((item for item in items if item['id'] == ident), None)
        if item is None:
            raise Bad('No such background.', 404)
        filename = item['url'].rsplit('/', 1)[-1]
        if not FILE.fullmatch(filename):
            raise Bad('Invalid background file.', 503)
        _write([entry for entry in items if entry['id'] != ident])
        (paths.BACKGROUNDS / filename).unlink(missing_ok=True)


def find_generated_image(folder, started_ns, output):
    """Prefer the requested copy; shared-store fallbacks must appear in this run's output."""
    folder = folder.resolve()
    generated = (Path(os.environ.get('CODEX_HOME') or paths.CODEX) / 'generated_images').resolve()
    candidates = list(folder.rglob('*'))
    # JSONL escapes path separators and newlines. Plain CLI output is accepted too.
    output = output.replace('\\/', '/').replace('\\n', '\n')
    for name in re.findall(r'(?:/|~/)[^\s"\'`<>]+\.(?:png|jpe?g|webp)', output):
        path = Path(name).expanduser()
        if path.is_relative_to(generated):
            candidates.append(path)
    fresh = []
    for path in candidates:
        if path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp') or path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if not (resolved.is_relative_to(folder) or resolved.is_relative_to(generated)):
            continue
        stamp = path.stat().st_mtime_ns
        # The private folder starts empty; copied images may keep an older timestamp.
        if (resolved.is_relative_to(folder) or stamp >= started_ns) and path.stat().st_size <= LIMIT:
            fresh.append((path == folder / 'bg.png', stamp, path))
    if not fresh:
        raise Bad('Codex did not save an image. Please try again.')
    return max(fresh)[2]


def _prompt(prompt, tuned, size):
    if tuned:
        template = (Path(__file__).parent / 'prompts' / 'background_image.md').read_text()
        dimensions = '1:1, about 2048x2048' if size == 'square' else '16:9, about 2560x1440'
        prompt = template.replace('{{size}}', dimensions).replace('{{prompt}}', prompt)
    return prompt + '\n\nGenerate one image and save it as bg.png in the working folder. If the image tool saves elsewhere, copy the image to bg.png. Report the saved image path.'


def _run_job(ident, prompt, tuned, size):
    try:
        deadline = time.monotonic() + TIMEOUT
        codex = shutil.which('codex')
        if not codex:
            raise Bad('Codex is not installed on this server.')
        with tempfile.TemporaryDirectory(prefix='tether-background-') as temporary:
            folder = Path(temporary)
            features = subprocess.run([codex, 'features', 'list'], cwd=folder, stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, timeout=min(10, TIMEOUT))
            if features.returncode or not re.search(r'^image_generation\s+\S+\s+true\s*$', features.stdout, re.M):
                raise Bad('Codex image generation is off or unavailable.')
            started_ns = time.time_ns()
            command = [codex, 'exec', '--sandbox', 'workspace-write', '--skip-git-repo-check',
                       '--enable', 'image_generation', '--json', '--', _prompt(prompt, tuned, size)]
            with tempfile.TemporaryFile() as log:
                process = subprocess.Popen(command, cwd=folder, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    # Stop descendants as well, so a timed-out job cannot leave a worker behind.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise
                if code:
                    raise Bad('Codex could not make that image. Please try again.')
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 1024 * 1024))
                output = log.read().decode('utf-8', errors='replace')
            image = find_generated_image(folder, started_ns, output)
            item = save(image.read_bytes(), source='ai', prompt=prompt)
        result = {'state': 'done', 'item': item}
    except subprocess.TimeoutExpired:
        result = {'state': 'failed', 'error': 'The image took too long. Please try again.'}
    except Bad as error:
        result = {'state': 'failed', 'error': str(error)}
    except (OSError, ValueError):
        result = {'state': 'failed', 'error': 'Could not make or save that image. Please try again.'}
    with _lock:
        _jobs[ident] = result


def generate(body):
    prompt, tuned, size = body.get('prompt'), body.get('tuned'), body.get('size', 'landscape')
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= 1000 or not prompt.strip():
        raise Bad('Enter an image prompt of 1 to 1000 characters.')
    if not isinstance(tuned, bool) or size not in ('landscape', 'square'):
        raise Bad('Choose tuned true or false and landscape or square size.')
    with _lock:
        if any(job['state'] == 'running' for job in _jobs.values()):
            raise Bad('An image is already being made. Please wait.', 409)
        _room(_load())
        # Keep recent results for polling without growing server memory forever.
        while len(_jobs) >= MAX_ITEMS:
            del _jobs[next(iter(_jobs))]
        ident = secrets.token_hex(16)
        _jobs[ident] = {'state': 'running'}
    try:
        threading.Thread(target=_run_job, args=(ident, prompt, tuned, size), daemon=True,
                         name='background-' + ident).start()
    except RuntimeError:
        with _lock:
            _jobs[ident] = {'state': 'failed', 'error': 'Could not start the image job. Please try again.'}
    return {'job': ident}


def get(handler):
    try:
        url = urlsplit(handler.path)
        if url.path == PATH:
            with _lock:
                items = sorted(_load(), key=lambda item: item['created'], reverse=True)
            handler.respond(200, {'items': items})
        elif url.path == PATH + '/generate':
            ident = parse_qs(url.query).get('job', [''])[0]
            if not ID.fullmatch(ident):
                raise Bad('Invalid image job id.')
            with _lock:
                job = _jobs.get(ident)
            if job is None:
                raise Bad('No such image job. Please start again.', 404)
            handler.respond(200, job)
        else:
            match = FILE.fullmatch(url.path.removeprefix(PATH + '/'))
            if not match:
                raise Bad('Invalid background file.', 404)
            target = paths.BACKGROUNDS / match[0]
            if target.is_symlink() or not target.is_file():
                raise Bad('No such background.', 404)
            handler.respond(200, target.read_bytes(), KINDS[match[2]], 'private, max-age=31536000, immutable')
    except Bad as error:
        handler.respond(error.status, {'error': str(error)})
    except OSError:
        handler.respond(503, {'error': 'Could not read the background.'})


def post(handler):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        upload = handler.path == PATH + '/upload'
        if not 0 < size <= (LIMIT if upload else 65536):
            handler.close_connection = True
            raise Bad('That image is empty or too large (the limit is 12 MB).' if upload else 'Message is too large.', 413)
        data = handler.rfile.read(size)
        if len(data) != size:
            raise Bad('The upload did not finish. Please try again.')
        if upload:
            kind = handler.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if kind not in KINDS.values():
                raise Bad('Choose a PNG, JPEG, or WebP image.', 415)
            handler.respond(200, save(data, kind))
            return
        body = json.loads(data)
        if not isinstance(body, dict):
            raise Bad('Invalid request.')
        if handler.path == PATH + '/delete':
            delete(body.get('id'))
            handler.respond(200, {'ok': True})
        elif handler.path == PATH + '/generate':
            handler.respond(202, generate(body))
        else:
            raise Bad('Not found.', 404)
    except Bad as error:
        handler.respond(error.status, {'error': str(error)})
    except (ValueError, UnicodeError):
        handler.respond(400, {'error': 'Invalid request.'})
    except OSError:
        handler.respond(503, {'error': 'Could not save the background.'})
