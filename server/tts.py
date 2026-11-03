"""Forward speech requests to the optional local Kokoro worker."""
import json
import os
import urllib.error
import urllib.request

URL = os.environ.get('MOBILE_TMUX_TTS_URL', 'http://127.0.0.1:7685').rstrip('/')
PATH = '/mobile/api/tts'
LIMIT = 8192


def _request(path, body=None):
    request = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read(), response.headers.get('Content-Type', 'audio/wav')
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get('error')
        except (ValueError, AttributeError):
            message = None
        error = ValueError if exc.code == 400 else RuntimeError
        raise error(message or 'Natural voice failed.') from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError('Natural voice is not running.') from exc


def speak(text, voice=None, speed=None):
    """Return WAV bytes and their content type."""
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 600:
        raise ValueError('Send between 1 and 600 characters.')
    body = {'text': text.strip()}
    if voice is not None:
        body['voice'] = voice
    if speed is not None:
        body['speed'] = speed
    return _request('/speak', body)


def voices():
    return json.loads(_request('/voices')[0])


def handles(path):
    return path.split('?', 1)[0] in (PATH, PATH + '/voices')


def get(handler):
    if handler.path.split('?', 1)[0] != PATH + '/voices':
        handler.respond(404, {'error': 'Not found'})
        return
    try:
        handler.respond(200, voices())
    except RuntimeError as exc:
        handler.respond(200, {'voices': [], 'error': str(exc)})


def post(handler):
    if handler.path.split('?', 1)[0] != PATH:
        handler.respond(404, {'error': 'Not found'})
        return
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= LIMIT:
            handler.respond(413, {'error': 'Invalid request size.'})
            return
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError('Invalid request.')
        audio, kind = speak(body.get('text'), body.get('voice'), body.get('speed'))
        handler.respond(200, audio, kind, 'no-store')
    except (ValueError, UnicodeError) as exc:
        handler.respond(400, {'error': str(exc)})
    except RuntimeError as exc:
        handler.respond(503, {'error': str(exc)})
