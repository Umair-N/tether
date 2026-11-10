"""Custom layouts: JSON files that an agent writes to the layouts folder (paths.LAYOUTS).

GET /mobile/api/layouts returns {folder, layouts, errors}. Each file is checked strictly: known keys and panels only,
hex colours only, whole numbers in range, at most MAX_BYTES. A layout is data only: no scripts, CSS or URLs.
A file that turns bad keeps its last good layout (marked "stale") until the server restarts, and its reason shows.
ui/src/layouts.ts runs the same checks again in the browser. Keep the two in step.
"""
import json
import os
import re
import stat
import threading
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import paths

PATH = '/mobile/api/layouts'
MANIFEST = json.loads(Path(__file__).with_name('ui_manifest.json').read_text(encoding='utf-8'))
MAX_BYTES = MANIFEST['limits']['maxBytes']
MAX_FILES = MANIFEST['limits']['maxFiles']
PANELS = tuple(MANIFEST['panels'])
PLACED = tuple(name for name, panel in MANIFEST['panels'].items() if panel['placement'] != 'tab-strip')
EDGE = tuple(name for name, panel in MANIFEST['panels'].items() if panel['placement'] == 'edge')
RESERVED = tuple(MANIFEST['limits']['reserved'])
ID = re.compile(r'[a-z0-9][a-z0-9-]{0,%d}' % (MANIFEST['limits']['idLength'] - 1))
HEX = re.compile(r'#[0-9a-fA-F]{6}')
THEME_KEYS = tuple(MANIFEST['theme']['keys'])
THEME_REQUIRED = tuple(MANIFEST['theme']['required'])
TOP_KEYS = ('id', 'name', 'description', 'grid', 'panels', 'style', 'theme', 'variants', 'phone', 'manifest')
MIN_CONTRAST = MANIFEST['theme']['minContrast']
_last_good = {}
_lock = threading.Lock()


class Bad(ValueError):
    status = 400


class Conflict(Bad):
    status = 409


class Missing(Bad):
    status = 404


def handles(path):
    return path.split('?', 1)[0] in (PATH, PATH + '/generate', PATH + '/save', PATH + '/delete')


def _keys(value, allowed, where):
    if not isinstance(value, dict):
        raise Bad('%s must be an object.' % where)
    extra = sorted(set(value) - set(allowed))
    if extra:
        raise Bad('%s has an unknown key: %s.' % (where, extra[0]))


def _int(value, low, high, where):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Bad('%s must be a whole number from %d to %d.' % (where, low, high))
    return value


def _text(value, high, where):
    if not isinstance(value, str) or not value.strip() or len(value) > high or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Bad('%s must be plain text of 1 to %d characters.' % (where, high))
    return value.strip()


def _luminance(colour):
    def channel(at):
        value = int(colour[at:at + 2], 16) / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)


def contrast(one, two):
    a, b = sorted((_luminance(one), _luminance(two)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def _theme(value):
    if value is None:
        return None
    _keys(value, THEME_KEYS, 'theme')
    for key in THEME_REQUIRED:
        if key not in value:
            raise Bad('theme needs %s.' % key)
    for key, colour in value.items():
        if not isinstance(colour, str) or not HEX.fullmatch(colour):
            raise Bad('theme.%s must be a hex colour such as #1a2b3c.' % key)
    theme = {key: colour.lower() for key, colour in value.items()}
    for key in ('bg', 'surface'):
        if contrast(theme['text'], theme[key]) < MIN_CONTRAST:
            raise Bad('theme.text needs a contrast of 4.5:1 or more against theme.%s.' % key)
    return theme


def _grid(value, panels):
    _keys(value, ('columns', 'chatWidth', 'sessionsClosed'), 'grid')
    columns = value.get('columns')
    if not isinstance(columns, list) or not 1 <= len(columns) <= MANIFEST['grid']['maxColumns']:
        raise Bad('grid.columns must be a list of 1 to 5 columns.')
    placed = []
    out = []
    for at, column in enumerate(columns):
        where = 'grid.columns[%d]' % at
        _keys(column, (*MANIFEST['grid']['slots'], 'width'), where)
        if 'main' not in column:
            raise Bad('%s needs main.' % where)
        item = {}
        for slot in MANIFEST['grid']['slots']:
            if slot not in column:
                continue
            name = column[slot]
            if name not in PLACED:
                raise Bad('%s.%s must be one of: %s.' % (where, slot, ', '.join(PLACED)))
            if slot != 'main' and name not in EDGE:
                raise Bad('%s.%s can only be waiting.' % (where, slot))
            if MANIFEST['panels'][name]['placement'] == 'first-column-only' and (at != 0 or slot != 'main'):
                raise Bad('activity can only be the main panel of the first column.')
            if name in placed:
                raise Bad('%s is placed twice.' % name)
            if name not in panels:
                raise Bad('%s is in grid but not in panels.' % name)
            placed.append(name)
            item[slot] = name
        if 'width' in column:
            item['width'] = _int(column['width'], *MANIFEST['grid']['columnWidth'], where + '.width')
        out.append(item)
    if [column['main'] for column in out].count('chat') != 1:
        raise Bad('chat must be the main panel of one column.')
    for name in panels:
        if name in PLACED and name not in placed:
            raise Bad('%s is in panels but has no place in grid.' % name)
    grid = {'columns': out}
    if 'chatWidth' in value:
        width = value['chatWidth']
        if width != 0:
            _int(width, *MANIFEST['grid']['chatWidth'], 'grid.chatWidth')
        grid['chatWidth'] = _int(width, 0, MANIFEST['grid']['chatWidth'][1], 'grid.chatWidth')
    if 'sessionsClosed' in value:
        if not isinstance(value['sessionsClosed'], bool):
            raise Bad('grid.sessionsClosed must be true or false.')
        grid['sessionsClosed'] = value['sessionsClosed']
    return grid


def validate(data, fallback_id):
    """Return a clean layout, or raise Bad with a reason a person can act on."""
    _keys(data, TOP_KEYS, 'The layout')
    layout_id = data.get('id', fallback_id)
    if not isinstance(layout_id, str) or not ID.fullmatch(layout_id):
        raise Bad('id must be 1 to 40 lowercase letters, digits or hyphens.')
    if layout_id in RESERVED:
        raise Bad('id "%s" belongs to a built-in layout.' % layout_id)
    panels = data.get('panels')
    if not isinstance(panels, list) or not panels or any(name not in PANELS for name in panels):
        raise Bad('panels must be a list of: %s.' % ', '.join(PANELS))
    if len(set(panels)) != len(panels):
        raise Bad('panels has a name twice.')
    if 'chat' not in panels:
        raise Bad('panels must include chat.')
    if 'grid' not in data:
        raise Bad('The layout needs grid.')
    style = data.get('style', {})
    _keys(style, ('radius', 'gap', 'surface'), 'style')
    surface = style.get('surface', 'flat')
    if surface not in MANIFEST['style']['surface']:
        raise Bad('style.surface must be flat or glass.')
    layout = {
        'id': layout_id,
        'name': _text(data.get('name'), MANIFEST['limits']['nameLength'], 'name'),
        'grid': _grid(data['grid'], panels),
        'panels': list(panels),
        'style': {'radius': _int(style.get('radius', 0), *MANIFEST['style']['radius'], 'style.radius'), 'gap': _int(style.get('gap', 0), *MANIFEST['style']['gap'], 'style.gap'), 'surface': surface},
        'theme': _theme(data.get('theme')),
    }
    if 'description' in data:
        layout['description'] = _text(data['description'], MANIFEST['limits']['descriptionLength'], 'description')
    variants = data.get('variants', {})
    _keys(variants, MANIFEST['variants'], 'variants')
    for key, value in variants.items():
        if not isinstance(value, str) or value not in MANIFEST['variants'][key]['options']:
            raise Bad('variants.%s must be one of: %s.' % (key, ', '.join(MANIFEST['variants'][key]['options'])))
    phone = data.get('phone', MANIFEST['devices']['phone']['default'])
    if not isinstance(phone, str) or phone not in MANIFEST['devices']['phone']['options']:
        raise Bad('phone must be one of: %s.' % ', '.join(MANIFEST['devices']['phone']['options']))
    layout.update(variants=dict(variants), phone=phone,
                  manifest=_int(data.get('manifest', 1), 1, 9007199254740991, 'manifest'))
    return layout


def _read(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise Bad('Only plain files are read (no links or folders).')
    if info.st_size > MAX_BYTES:
        raise Bad('The file is larger than 16 KB.')
    with open(path, 'rb') as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise Bad('The file is larger than 16 KB.')
    try:
        return json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as error:
        raise Bad('The file is not valid JSON: %s.' % str(error).split(':')[0]) from None


def folder_label(folder):
    home = str(paths.HOME)
    text = str(folder)
    return '~' + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def scan(folder=None):
    folder = folder or paths.LAYOUTS
    layouts, errors, seen = [], [], set()
    try:
        names = sorted(name for name in os.listdir(folder) if name.endswith('.json') and not name.startswith('.'))
    except FileNotFoundError:
        names = []
    except OSError as error:
        return {'folder': folder_label(folder), 'layouts': [], 'errors': [{'file': '', 'reason': 'Tether cannot read the folder: %s.' % error.strerror}]}
    if len(names) > MAX_FILES:
        errors.append({'file': '', 'reason': 'Only the first %d files are read.' % MAX_FILES})
        names = names[:MAX_FILES]
    with _lock:
        for name in names:
            stem = re.sub(r'[^a-z0-9-]+', '-', name[:-5].lower()).strip('-')[:40] or 'layout'
            try:
                layout = validate(_read(os.path.join(folder, name)), stem)
                if layout['id'] in seen:
                    raise Bad('Another file already uses id "%s".' % layout['id'])
                _last_good[name] = layout
            except (Bad, OSError) as error:
                reason = str(error) if isinstance(error, Bad) else 'Tether cannot read the file: %s.' % (error.strerror or error)
                errors.append({'file': name, 'reason': reason})
                layout = _last_good.get(name)
                if not layout or layout['id'] in seen:
                    continue
                layout = dict(layout, stale=reason)
            seen.add(layout['id'])
            layouts.append(dict(layout, file=name))
        for name in list(_last_good):
            if name not in names:
                del _last_good[name]
    return {'folder': folder_label(folder), 'layouts': layouts, 'errors': errors}


def checked_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise Bad('id must be 1 to 40 lowercase letters, digits or hyphens.')
    if value in RESERVED:
        raise Bad('id "%s" belongs to a built-in layout.' % value)
    return value


def save(data, overwrite=False):
    if not isinstance(overwrite, bool):
        raise Bad('overwrite must be true or false.')
    layout = validate(data, '')
    raw = (json.dumps(layout, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise Bad('The file is larger than 16 KB.')
    folder = Path(paths.LAYOUTS)
    name = layout['id'] + '.json'
    with _lock:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        exists = os.path.lexists(target)
        if exists and not stat.S_ISREG(target.lstat().st_mode):
            raise Bad('Only plain files can be overwritten (no links or folders).')
        files = [item for item in folder.iterdir() if item.name.endswith('.json') and not item.name.startswith('.')]
        previous = []
        for item in files:
            if item == target:
                continue
            stem = re.sub(r'[^a-z0-9-]+', '-', item.stem.lower()).strip('-')[:40] or 'layout'
            try:
                if validate(_read(item), stem)['id'] == layout['id']:
                    previous.append(item)
            except (Bad, OSError):
                continue
        if (exists or previous) and not overwrite:
            raise Conflict('This layout already exists. Choose overwrite to replace it.')
        if len(files) + (not exists) - len(previous) > MAX_FILES:
            raise Bad('The layouts folder already has %d files.' % MAX_FILES)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=folder, prefix='.layout-', delete=False) as handle:
                temporary = handle.name
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            # Older agent-written files may use a filename other than their layout id.
            for item in previous:
                item.unlink(missing_ok=True)
                _last_good.pop(item.name, None)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        _last_good.pop(name, None)
    return {'layout': layout, 'file': name}


def delete(layout_id):
    name = checked_id(layout_id) + '.json'
    with _lock:
        target = Path(paths.LAYOUTS) / name
        try:
            if not stat.S_ISREG(target.lstat().st_mode):
                raise Bad('Only plain layout files can be deleted (no links or folders).')
            target.unlink()
        except FileNotFoundError:
            raise Missing('This custom layout does not exist.') from None
        _last_good.pop(name, None)
    return {'ok': True}


def get(handler):
    if urlsplit(handler.path).path == PATH:
        handler.respond(200, scan())
    elif urlsplit(handler.path).path == PATH + '/generate':
        import layout_generate
        job = parse_qs(urlsplit(handler.path).query).get('job', [''])[0]
        result = layout_generate.poll(job)
        handler.respond(200 if result else 404, result or {'error': 'This layout job does not exist.'})
    else:
        handler.respond(404, {'error': 'Not found'})


def post(handler):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 65536:
            handler.respond(413, {'error': 'Message is too large.'})
            return
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise Bad('The request must be an object.')
        if handler.path == PATH + '/save':
            result = save(body.get('layout'), body.get('overwrite', False))
        elif handler.path == PATH + '/delete':
            result = delete(body.get('id'))
        elif handler.path == PATH + '/generate':
            import layout_generate
            result = {'job': layout_generate.start(body.get('prompt'), body.get('base'))}
            handler.respond(202, result)
            return
        else:
            handler.respond(404, {'error': 'Not found'})
            return
        handler.respond(200, result)
    except Bad as error:
        handler.respond(error.status, {'error': str(error)})
    except (ValueError, UnicodeError, RecursionError):
        handler.respond(400, {'error': 'The request is not valid JSON.'})
    except OSError:
        handler.respond(500, {'error': 'Tether could not change the layout files.'})
