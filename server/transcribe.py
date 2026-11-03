"""Forward a phone recording to the optional local voice-to-text worker (transcribe_worker.py)."""
import json
import os
import re
import urllib.error
import urllib.request

LIMIT = 10 * 1024 * 1024
# Spoken paths: "src slash App.tsx" -> "src/App.tsx". Whisper writes "dot" as "." itself, but keeps "slash" as a word.
SLASH = re.compile(r'(?<=\S) slash (?=\S)', re.IGNORECASE)


def tidy(text):
    return SLASH.sub('/', text)
URL = os.environ.get('MOBILE_TMUX_TRANSCRIBE_URL', 'http://127.0.0.1:7684/transcribe')


def transcribe(audio, content_type, live=False, recording=''):
    """live=True asks for quick rough text while the user still speaks. Return the worker's reply ({'text': ...}). Raises RuntimeError with a message for the page."""
    if not content_type.startswith('audio/'):
        raise ValueError('Send an audio recording.')
    headers = {'Content-Type': content_type}
    if re.fullmatch(r'[A-Za-z0-9-]{8,64}', recording or ''):
        headers['X-Recording'] = recording  # lets the worker reuse text it already converted (transcribe_worker.py)
    request = urllib.request.Request(URL + ('?live=1' if live else ''), data=audio, method='POST', headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10 if live else 120) as response:
            reply = json.loads(response.read())
            if isinstance(reply.get('text'), str):
                reply['text'] = tidy(reply['text'])
            return reply
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get('error')
        except ValueError:
            message = None
        raise RuntimeError(message or 'Voice-to-text failed.') from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError('Voice-to-text is not running on the server.') from exc
