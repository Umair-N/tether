"""Conservative, ephemeral Claude text from a read-only pane capture."""
import re
import subprocess
import threading
import time

import paths

_samples = {}
_lock = threading.Lock()
SPINNER = re.compile(r'^\s*[^\w\s]\s+[A-Z][^\n]*…\s*\([^\n]*\)\s*$', re.M)
CHROME = re.compile(r'[\u2500-\u257f⎿❯\x1b]|esc to interrupt|ctrl\+[a-z]|(?m:^\s*\|.*\|)', re.I)
TOOL = re.compile(r'^(?:[\w.-]+\(|(?:Read|Ran|Running|Searched|Background command|Task|Tool)\b)')


def sample(entry, now=None):
    """Share captures across watchers; never capture one pane more than once per 300ms."""
    with _lock:
        supplied_now = now
        now = time.monotonic() if now is None else now
        old = _samples.get(entry['id'])
        if old and now - old[0] < .3:
            return old[1:]
        width = int(subprocess.run([paths.TMUX, 'display-message', '-p', '-t', entry['id'], '#{pane_width}'],
                                  check=True, capture_output=True, text=True, timeout=5).stdout.strip())
        screen = subprocess.run([paths.TMUX, 'capture-pane', '-p', '-t', entry['id']],
                                check=True, capture_output=True, text=True, timeout=5).stdout
        _samples[entry['id']] = (time.monotonic() if supplied_now is None else now, screen, width)
        if len(_samples) > 128:
            del _samples[min(_samples, key=lambda key: _samples[key][0])]
        return screen, width


def normalized(text):
    text = re.sub(r'!?\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'(?m)^\s*[-*•]\s+', '', text)
    return ' '.join(re.sub(r'[*_`#]', '', text).split())


def extract(screen, width, messages):
    lines = screen.splitlines()
    prompts = [i for i, line in enumerate(lines) if line.startswith('❯')]
    if not prompts:
        return None
    stops = [i for i, line in enumerate(lines[:prompts[-1]]) if SPINNER.match(line)]
    if not stops:
        return None
    stop = stops[-1]
    starts = [i for i, line in enumerate(lines[:stop]) if line.startswith('● ')]
    if not starts:
        return None
    start = starts[-1]
    raw = [lines[start][2:]] + lines[start + 1:stop]
    while raw and not raw[-1].strip():
        raw.pop()
    if not raw or not raw[0].strip() or TOOL.match(raw[0]) or CHROME.search('\n'.join(raw)):
        return None
    # A clipped block or tool output must never become assistant prose.
    if any(line.strip() and not line.startswith('  ') for line in raw[1:]):
        return None
    result = []
    previous_width = 0
    for index, line in enumerate(raw):
        text = line[2:] if index else line
        if result and text.strip() and result[-1] and previous_width + len(text.strip().split()[0]) + 1 > width and not re.match(r'\s*(?:[-*•]|\d+[.)])\s', text):
            result[-1] += ' ' + text.strip()
        else:
            result.append(text.rstrip())
        previous_width = len(line) + (2 if index == 0 else 0)
    text = '\n'.join(result).strip()
    value = normalized(text)
    known = [normalized(m.get('text', '')) for m in messages if not m.get('kind')]
    if not value or any(value in item for item in known if item):
        return None
    # Require a transcript-known block above this one. Missing anchors produce a gap.
    before = normalized('\n'.join(line[2:] if line.startswith(('● ', '  ')) else line for line in lines[:start]))
    if not any(item and item in before for item in known[-4:]):
        return None
    return text
