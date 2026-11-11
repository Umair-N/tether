"""A bounded, atomic log of answers to agent questions."""
from datetime import datetime
import os
import tempfile
import threading
import unicodedata

import paths

HEADER = '# Past decisions (Tether records your answers to agent questions)\n\n'
LIMIT = 200
_LOCK = threading.Lock()


def _one_line(text, limit):
    text = ''.join(char for char in str(text)
                   if char.isspace() or unicodedata.category(char) not in ('Cc', 'Cf', 'Cs'))
    return ' '.join(text.split())[:limit]


def record(question, answer, now=None):
    now = datetime.now() if now is None else now
    entry = f'- {now:%Y-%m-%d}: Q: {_one_line(question, 200)} -> A: {_one_line(answer, 300)}'
    path = paths.CONFIG / 'decisions.md'
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            entries = [line for line in path.read_text(encoding='utf-8').splitlines() if line.startswith('- ')]
        except FileNotFoundError:
            entries = []
        entries = (entries + [entry])[-LIMIT:]
        fd, temp = tempfile.mkstemp(prefix='.decisions-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(HEADER + '\n'.join(entries) + '\n')
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
