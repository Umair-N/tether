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

import paths

PATH = '/mobile/api/layouts'
MAX_BYTES = 16 * 1024
MAX_FILES = 50
PANELS = ('waiting', 'chat', 'agents', 'sessions', 'status', 'activity')
PLACED = ('activity', 'sessions', 'chat', 'status', 'waiting')  # Panels that take a place in the grid. 'agents' is the tab strip.
EDGE = ('waiting',)  # Panels that may sit above or below another panel in a column.
RESERVED = ('classic', 'status', 'islands', 'vscode', 'zed', 'mine')
ID = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
HEX = re.compile(r'#[0-9a-fA-F]{6}')
THEME_KEYS = ('bg', 'sidebar', 'surface', 'bubble', 'text', 'muted', 'line', 'hover', 'accent', 'accentText', 'attention', 'done', 'pin', 'error')
THEME_REQUIRED = ('bg', 'surface', 'text', 'muted', 'line', 'accent', 'attention')
TOP_KEYS = ('id', 'name', 'description', 'grid', 'panels', 'style', 'theme')
MIN_CONTRAST = 4.5
_last_good = {}
_lock = threading.Lock()


class Bad(ValueError):
    pass


def handles(path):
    return path.split('?', 1)[0] == PATH


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
    if not isinstance(columns, list) or not 1 <= len(columns) <= 5:
        raise Bad('grid.columns must be a list of 1 to 5 columns.')
    placed = []
    out = []
    for at, column in enumerate(columns):
        where = 'grid.columns[%d]' % at
        _keys(column, ('top', 'main', 'bottom', 'width'), where)
        if 'main' not in column:
            raise Bad('%s needs main.' % where)
        item = {}
        for slot in ('top', 'main', 'bottom'):
            if slot not in column:
                continue
            name = column[slot]
            if name not in PLACED:
                raise Bad('%s.%s must be one of: %s.' % (where, slot, ', '.join(PLACED)))
            if slot != 'main' and name not in EDGE:
                raise Bad('%s.%s can only be waiting.' % (where, slot))
            if name == 'activity' and at != 0:
                raise Bad('activity can only be the main panel of the first column.')
            if name in placed:
                raise Bad('%s is placed twice.' % name)
            if name not in panels:
                raise Bad('%s is in grid but not in panels.' % name)
            placed.append(name)
            item[slot] = name
        if 'width' in column:
            item['width'] = _int(column['width'], 160, 480, where + '.width')
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
            _int(width, 480, 1600, 'grid.chatWidth')
        grid['chatWidth'] = _int(width, 0, 1600, 'grid.chatWidth')
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
    if surface not in ('flat', 'glass'):
        raise Bad('style.surface must be flat or glass.')
    layout = {
        'id': layout_id,
        'name': _text(data.get('name'), 40, 'name'),
        'grid': _grid(data['grid'], panels),
        'panels': list(panels),
        'style': {'radius': _int(style.get('radius', 0), 0, 24, 'style.radius'), 'gap': _int(style.get('gap', 0), 0, 24, 'style.gap'), 'surface': surface},
        'theme': _theme(data.get('theme')),
    }
    if 'description' in data:
        layout['description'] = _text(data['description'], 200, 'description')
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


def get(handler):
    handler.respond(200, scan())
