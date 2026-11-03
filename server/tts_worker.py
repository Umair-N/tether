"""Optional Kokoro speech worker, served only on loopback."""
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
import threading

import paths

LIMIT = 8192
DEFAULT_VOICE = os.environ.get('TETHER_TTS_VOICE', 'af_heart')  # grade A in Kokoro VOICES.md


def inputs(body, voices, default=DEFAULT_VOICE):
    """Validate a request before synthesis; no model is needed."""
    if not isinstance(body, dict):
        raise ValueError('Invalid request.')
    text = body.get('text')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 600:
        raise ValueError('Send between 1 and 600 characters.')
    voice = body.get('voice', default)
    if not isinstance(voice, str) or voice not in voices:
        raise ValueError('Choose an available voice.')
    speed = body.get('speed', 1.0)
    if (isinstance(speed, bool) or not isinstance(speed, (int, float))
            or isinstance(speed, float) and not math.isfinite(speed)):
        raise ValueError('Choose a numeric speed.')
    return text.strip(), voice, max(0.7, min(1.4, speed))


def wav(audio, rate):
    import soundfile
    output = io.BytesIO()
    soundfile.write(output, audio, rate, format='WAV', subtype='PCM_16')
    return output.getvalue()


class Speech:
    def __init__(self, model, default=DEFAULT_VOICE, encode=wav):
        self.model, self.default, self.encode = model, default, encode
        self.voices = list(model.get_voices())
        if default not in self.voices:
            raise ValueError('TETHER_TTS_VOICE is not an available voice.')
        self.lock = threading.Lock()
        self.cache = OrderedDict()

    def speak(self, body):
        key = inputs(body, self.voices, self.default)
        text, voice, speed = key
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            audio, rate = self.model.create(text, voice=voice, speed=speed,
                                             lang='en-gb' if voice.startswith('b') else 'en-us')
            data = self.encode(audio, rate)
            self.cache[key] = data
            if len(self.cache) > 64:
                self.cache.popitem(last=False)
            return data


def load_model():
    """Load once with four CPU threads, apart from the stdlib server."""
    import onnxruntime
    from kokoro_onnx import Kokoro
    folder = paths.HOME / '.local/share/tether/tts-models'
    model = os.environ.get('TETHER_TTS_MODEL', str(folder / 'kokoro-v1.0.onnx'))
    voices = os.environ.get('TETHER_TTS_VOICES', str(folder / 'voices-v1.0.bin'))
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    session = onnxruntime.InferenceSession(model, sess_options=options, providers=['CPUExecutionProvider'])
    return Kokoro.from_session(session, voices)


class Handler(BaseHTTPRequestHandler):
    timeout = 60

    def respond(self, status, body, kind='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/health':
            self.respond(200, {'ok': True})
        elif self.path == '/voices':
            speech = self.server.speech
            self.respond(200, {'voices': speech.voices, 'default': speech.default})
        else:
            self.respond(404, {'error': 'Not found'})

    def do_POST(self):
        if self.path != '/speak':
            self.respond(404, {'error': 'Not found'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= LIMIT:
                self.respond(413, {'error': 'Invalid request size.'})
                return
            body = json.loads(self.rfile.read(size))
            data = self.server.speech.speak(body)
        except (ValueError, UnicodeError) as exc:
            self.respond(400, {'error': str(exc)})
            return
        except Exception:
            self.respond(500, {'error': 'Natural voice could not speak that text.'})
            return
        self.respond(200, data, 'audio/wav')

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    speech = Speech(load_model())
    http = ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('TETHER_TTS_PORT', '7685'))), Handler)
    http.speech = speech
    try:
        http.serve_forever()
    finally:
        http.server_close()
