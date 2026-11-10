"""One bounded, headless layout draft job at a time. Never save an agent result."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import custom_layouts as layouts

MODEL = 'sonnet'
TIMEOUT = 180
MAX_JOBS = 3
TTL = 600
PROMPTS = Path(__file__).with_name('prompts')
_jobs = {}
_lock = threading.Lock()


def built_ins():
    """Data copies of layouts.ts, checked against that source by test_ui_manifest."""
    return json.loads(Path(__file__).with_name('builtin_layouts.json').read_text(encoding='utf-8'))


def _prune():
    now = time.monotonic()
    for job, item in list(_jobs.items()):
        if item['state'] != 'running' and now - item['finished'] >= TTL:
            del _jobs[job]


def poll(job):
    with _lock:
        _prune()
        item = _jobs.get(job)
        return {key: value for key, value in item.items() if key != 'finished'} if item else None


def _base(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise layouts.Bad('base must be a layout object.')
    # Browser layouts may carry display metadata or a built-in id.
    data = {key: item for key, item in value.items() if key not in ('file', 'stale', 'themeName')}
    layout_id = data.get('id', 'base-layout')
    if not isinstance(layout_id, str) or not layouts.ID.fullmatch(layout_id):
        raise layouts.Bad('base.id must be a layout id.')
    clean = layouts.validate(dict(data, id='base-layout'), 'base-layout')
    clean['id'] = layout_id
    if len(json.dumps(clean).encode('utf-8')) > layouts.MAX_BYTES:
        raise layouts.Bad('base is larger than 16 KB.')
    return clean


def start(prompt, base=None):
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
        raise layouts.Bad('Write a prompt of 1 to 2000 characters.')
    base = _base(base)
    with _lock:
        _prune()
        if any(item['state'] == 'running' for item in _jobs.values()):
            raise layouts.Conflict('A layout is being made. Wait for it to finish.')
        while len(_jobs) >= MAX_JOBS:
            del _jobs[next(iter(_jobs))]
        job = uuid.uuid4().hex
        _jobs[job] = {'state': 'running'}
        try:
            threading.Thread(target=_work, args=(job, prompt.strip(), base), daemon=True,
                             name='layout-' + job[:8]).start()
        except RuntimeError:
            del _jobs[job]
            raise layouts.Bad('Tether could not start the layout job.') from None
        return job


def first_object(text):
    if not isinstance(text, str):
        raise layouts.Bad('The agent did not return a JSON object.')
    decoder = json.JSONDecoder()
    for at, char in enumerate(text):
        if char != '{':
            continue
        try:
            value, _ = decoder.raw_decode(text, at)
            return value
        except ValueError:
            continue
    raise layouts.Bad('The agent did not return a JSON object.')


def _agent(system, prompt):
    # Match project_launch.launch's PATH lookup; do not hard-code an installation.
    claude = shutil.which('claude')
    with tempfile.TemporaryDirectory(prefix='tether-layout-') as folder:
        if claude:
            command = [claude, '-p', prompt, '--system-prompt', system, '--model', MODEL,
                       '--output-format', 'json', '--tools', '', '--strict-mcp-config',
                       '--setting-sources', '', '--no-session-persistence']
        else:
            codex = shutil.which('codex')
            if not codex:
                raise layouts.Bad('Install Claude or Codex to make a layout.')
            output = Path(folder) / 'last-message.txt'
            command = [codex, 'exec', '--sandbox', 'read-only', '--skip-git-repo-check',
                       '-o', str(output), system + '\n\n' + prompt]
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=TIMEOUT, cwd=folder)
        if result.returncode:
            raise layouts.Bad('The layout agent failed. Try again.')
        if not claude:
            return output.read_text(encoding='utf-8')
        envelope = first_object(result.stdout)
        if envelope.get('is_error'):
            raise layouts.Bad('The layout agent failed. Try again.')
        # Claude's --output-format json wraps its final reply in result.
        return envelope.get('result', result.stdout)


def draft_id(name):
    stem = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40].rstrip('-') or 'layout'
    used = set(layouts.RESERVED)
    used.update(item['id'] for item in layouts.scan()['layouts'])
    folder = Path(layouts.paths.LAYOUTS)
    candidate, suffix = stem, 2
    while candidate in used or (folder / (candidate + '.json')).exists() or (folder / (candidate + '.json')).is_symlink():
        tail = '-' + str(suffix)
        candidate = stem[:40 - len(tail)].rstrip('-') + tail
        suffix += 1
    return candidate


def _work(job, prompt, base):
    try:
        system = (PROMPTS / 'layout_generator.md').read_text(encoding='utf-8')
        context = {'manifest': layouts.MANIFEST, 'examples': [{key: value for key, value in item.items() if key != 'themeName'}
                                                    for item in built_ins() if item['id'] in ('classic', 'vscode')],
                   'base': base, 'prompt': prompt}
        request = json.dumps(context, ensure_ascii=False)
        for attempt in range(2):
            try:
                reply = _agent(system, request)
                response = first_object(reply)
                data = response.get('layout')
                if not isinstance(data, dict):
                    raise layouts.Bad('The agent reply needs a layout object.')
                data = dict(data)
                notes = response.get('notes', data.pop('notes', ''))
                if not isinstance(notes, str) or len(notes) > 1000:
                    raise layouts.Bad('notes must be short text.')
                data['id'] = draft_id(data['name']) if isinstance(data.get('name'), str) else 'layout'
                clean = layouts.validate(data, 'layout')
                result = {'state': 'done', 'layout': clean, 'notes': notes.strip()}
                break
            except (layouts.Bad, ValueError) as error:
                if attempt:
                    raise
                request += '\nFix this error and return the whole layout again: ' + str(error)
        else:
            raise layouts.Bad('The agent did not return a layout.')
    except subprocess.TimeoutExpired:
        result = {'state': 'failed', 'error': 'The layout agent took too long. Try again.'}
    except (layouts.Bad, ValueError) as error:
        result = {'state': 'failed', 'error': str(error)}
    except (OSError, subprocess.SubprocessError):
        result = {'state': 'failed', 'error': 'Tether could not run the layout agent.'}
    except Exception:
        # A malformed agent reply must never leave the single job slot stuck.
        result = {'state': 'failed', 'error': 'The agent returned an invalid layout.'}
    with _lock:
        _jobs[job] = dict(result, finished=time.monotonic())
