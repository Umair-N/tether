"""Optional voice-to-text worker for the phone's mic button.

Runs apart from server.py because it needs faster-whisper (server.py stays stdlib-only).
Listens on 127.0.0.1 only; server.py forwards signed-in uploads from /mobile/api/transcribe.

    uv venv ~/.local/share/mobile-tmux/whisper-venv
    VIRTUAL_ENV=~/.local/share/mobile-tmux/whisper-venv uv pip install faster-whisper 'av>=16,<17'
    (faster-whisper 1.x passes av.open(metadata_errors=...), which PyAV 17+ no longer accepts.)
    ~/.local/share/mobile-tmux/whisper-venv/bin/python transcribe_worker.py

Settings (environment): MOBILE_TMUX_WHISPER_MODEL (small), MOBILE_TMUX_WHISPER_LANGUAGE (en, empty = detect),
MOBILE_TMUX_WHISPER_THREADS (8), MOBILE_TMUX_TRANSCRIBE_PORT (7684), MOBILE_TMUX_WHISPER_PROMPT (word hints),
MOBILE_TMUX_WHISPER_LIVE_MODEL (base.en: the fast model for text shown while the user still speaks),
MOBILE_TMUX_WHISPER_BEAM (1: the final pass's beam size; 5 was 1.7x slower with the same text on test clips).

Long recordings: the page tags each recording with X-Recording. While the user speaks, each live request may start a
background pass of the accurate model over the finished sentences. On stop, the final pass converts only the rest,
so the wait after stop stays short for any length (a 33 s clip took 9.8 s with one whole pass).
"""
import io
import json
import re
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from faster_whisper import WhisperModel, decode_audio

LIMIT = 10 * 1024 * 1024
PORT = int(os.environ.get('MOBILE_TMUX_TRANSCRIBE_PORT', '7684'))
LANGUAGE = os.environ.get('MOBILE_TMUX_WHISPER_LANGUAGE', 'en') or None
# Words that Whisper often mishears in coding talk.
PROMPT = os.environ.get('TETHER_WHISPER_PROMPT') or os.environ.get('MOBILE_TMUX_WHISPER_PROMPT', 'tmux, Tailscale, Claude, Codex, Tether, split the pane, git push, version, release, npm, repo, PWA, API, JSON.')
THREADS = int(os.environ.get('MOBILE_TMUX_WHISPER_THREADS', '8'))
BEAM = int(os.environ.get('MOBILE_TMUX_WHISPER_BEAM', '1'))
MODEL = WhisperModel(os.environ.get('MOBILE_TMUX_WHISPER_MODEL', 'small'), device='cpu', compute_type='int8', cpu_threads=THREADS)
LIVE_MODEL = WhisperModel(os.environ.get('MOBILE_TMUX_WHISPER_LIVE_MODEL', 'base.en'), device='cpu', compute_type='int8', cpu_threads=THREADS)
FINAL_LOCK = threading.Lock()
LIVE_LOCK = threading.Lock()
RATE = 16000
COMMIT_AFTER = 6.0  # seconds of audio not yet converted by the accurate model before a background pass starts
KEEP_TAIL = 2.5  # never commit a segment that ends in the last seconds: the user may be mid-word
RECORDING_ID = re.compile(r'[A-Za-z0-9-]{8,64}')


class Recording:
    def __init__(self):
        self.done = (0.0, '')  # (seconds converted by the accurate model, their text); replaced as one value
        self.tried = 0.0  # length at the last pass that found nothing to commit
        self.lock = threading.Lock()  # held by a background pass; the final pass waits for it
        self.used = time.monotonic()


RECORDINGS = {}
RECORDINGS_LOCK = threading.Lock()


def recording(key, create=True):
    with RECORDINGS_LOCK:
        now = time.monotonic()
        for old in [name for name, item in RECORDINGS.items() if now - item.used > 600]:
            del RECORDINGS[old]
        item = RECORDINGS.get(key)
        if item is None and create:
            item = RECORDINGS[key] = Recording()
        if item:
            item.used = now
        return item


def join(*parts):
    return ' '.join(part.strip() for part in parts if part.strip())


def accurate(samples, before=''):
    """The accurate model's segments. before: the text so far, so the model keeps the same style across parts."""
    prompt = join(PROMPT, before[-200:])
    with FINAL_LOCK:
        segments, _ = MODEL.transcribe(samples, language=LANGUAGE, initial_prompt=prompt, vad_filter=True, beam_size=BEAM)
        return list(segments)


def commit(item, samples):
    """Background pass: convert the audio after item.done and keep the segments that end before the last KEEP_TAIL seconds."""
    if not item.lock.acquire(blocking=False):
        return
    try:
        start, text = item.done
        length = len(samples) / RATE
        segments = accurate(samples[int(start * RATE):], text)
        keep = [segment for segment in segments if start + segment.end <= length - KEEP_TAIL]
        if keep:
            item.done = (start + keep[-1].end, join(text, *(segment.text for segment in keep)))
            print('commit audio=%.1fs upto=%.1fs' % (length, item.done[0]), file=sys.stderr, flush=True)
        else:
            item.tried = length
    except Exception as exc:  # a bad partial upload: the final pass still converts everything
        print('commit failed: %s' % type(exc).__name__, file=sys.stderr, flush=True)
    finally:
        item.lock.release()


def transcribe(audio, live=False, key=None):
    """live=True: quick rough text while the user speaks (fast model, greedy). Otherwise the accurate final text.
    Live passes use one temperature and a token cap: without them, mic noise can send the model into retry loops (34 s seen).
    key: the recording's X-Recording id. With it, the accurate text of finished sentences is reused (see the docstring at the top)."""
    if key:
        samples = decode_audio(io.BytesIO(audio), sampling_rate=RATE)
        length = len(samples) / RATE
        item = recording(key, create=live)
        if live:
            start, text = item.done
            if length - start - KEEP_TAIL >= COMMIT_AFTER and length - item.tried >= 5 and not item.lock.locked():
                threading.Thread(target=commit, args=(item, samples), daemon=True).start()
            with LIVE_LOCK:
                segments, _ = LIVE_MODEL.transcribe(samples[int(start * RATE):], initial_prompt=PROMPT, vad_filter=True, beam_size=1, temperature=0.0,
                                                    condition_on_previous_text=False, without_timestamps=True, max_new_tokens=128)
                return join(text, *(segment.text for segment in segments)), length
        start, text = 0.0, ''
        if item:
            with item.lock:  # wait for a background pass that is still running
                start, text = item.done
            with RECORDINGS_LOCK:
                RECORDINGS.pop(key, None)
        return join(text, *(segment.text for segment in accurate(samples[int(start * RATE):], text))), length
    if live:
        with LIVE_LOCK:
            segments, info = LIVE_MODEL.transcribe(io.BytesIO(audio), initial_prompt=PROMPT, vad_filter=True, beam_size=1, temperature=0.0,
                                                   condition_on_previous_text=False, without_timestamps=True, max_new_tokens=128)
            return ' '.join(segment.text.strip() for segment in segments).strip(), info.duration
    with FINAL_LOCK:
        segments, info = MODEL.transcribe(io.BytesIO(audio), language=LANGUAGE, initial_prompt=PROMPT, vad_filter=True, beam_size=BEAM)
        return ' '.join(segment.text.strip() for segment in segments).strip(), info.duration


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path not in ('/transcribe', '/transcribe?live=1'):
            self.respond(404, {'error': 'Not found'})
            return
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= LIMIT:
            self.respond(413, {'error': 'The recording is empty or too long.'})
            return
        started = time.monotonic()
        key = self.headers.get('X-Recording', '')
        key = key if RECORDING_ID.fullmatch(key) else None
        try:
            text, duration = transcribe(self.rfile.read(size), live=self.path.endswith('?live=1'), key=key)
        except Exception as exc:  # bad or unsupported audio
            self.respond(400, {'error': 'Could not read that recording (%s).' % type(exc).__name__})
            return
        took = round(time.monotonic() - started, 2)
        print('%s audio=%.1fs took=%.2fs words=%d' % ('live' if self.path.endswith('?live=1') else 'final', duration, took, len(text.split())), file=sys.stderr, flush=True)
        try:
            self.respond(200, {'text': text, 'audio_seconds': round(duration, 1), 'seconds': took})
        except BrokenPipeError:  # the phone or server.py gave up waiting
            pass

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
