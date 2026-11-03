"""Custom pane icons: small SVGs that Claude draws, kept in paths.TETHER/icons/<name>.svg.

A pane wears one through the tmux option @chat_icon = "svg-<name>". The file may come from draw() (Tether asks
`claude -p` with a small model) or from an agent that writes the file itself, so read() always rebuilds the SVG
from a short allow-list of shapes and attributes: no scripts, links, styles, images or text survive.
The UI shows it as a CSS mask, so it takes the theme's color like the built-in icons.
"""
import hashlib
import os
import re
import subprocess
import xml.etree.ElementTree as ET

import paths

NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
SHAPES = {'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'ellipse', 'g'}
ATTRIBUTES = {'d', 'cx', 'cy', 'r', 'rx', 'ry', 'x', 'y', 'width', 'height', 'x1', 'y1', 'x2', 'y2', 'points', 'transform',
              'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'opacity', 'fill-rule'}
VALUE = re.compile(r'[-0-9.,\s a-zA-Z#()%]{0,4000}')  # numbers, path commands, colors, transform(...)
LIMIT = 8000  # bytes; a stroke icon is about 500
MODEL = 'haiku'
PROMPT = """Draw one icon for a chat tab named "{label}".{hint}
Style: like lucide.dev. viewBox 0 0 24 24, fill none, stroke currentColor, stroke-width 2, round caps and joins,
2 to 6 simple shapes, readable at 16 px. Use only path, circle, rect, line, polyline, polygon, ellipse.
Reply with the <svg> element only, no prose and no code fence."""


def folder():
    return paths.TETHER / 'icons'


def clean(text):
    """The SVG rebuilt from allowed shapes and attributes, or None when nothing drawable is left."""
    match = re.search(r'<svg\b.*?</svg>', text or '', re.S)
    if not match or len(match.group(0)) > LIMIT or re.search(r'<!(DOCTYPE|ENTITY)', text, re.I):
        return None
    try:
        root = ET.fromstring(match.group(0))
    except ET.ParseError:
        return None

    def local(tag):
        return tag.rsplit('}', 1)[-1]

    def copy(source, target):
        for child in source:
            tag = local(child.tag)
            if tag not in SHAPES:
                continue
            node = ET.SubElement(target, tag)
            for key, value in child.attrib.items():
                key = local(key)
                if key in ATTRIBUTES and VALUE.fullmatch(value) and 'url' not in value.lower():
                    node.set(key, value)
            copy(child, node)

    svg = ET.Element('svg', {'xmlns': 'http://www.w3.org/2000/svg', 'viewBox': '0 0 24 24', 'fill': 'none', 'stroke': 'black',
                             'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round'})
    copy(root, svg)
    if not len(svg):
        return None
    # currentColor means nothing inside a mask; black is the mask's "show" color.
    return ET.tostring(svg, encoding='unicode').replace('currentColor', 'black')


def read(name):
    """The clean SVG for icon <name>, or None."""
    if not NAME.fullmatch(name or ''):
        return None
    try:
        return clean((folder() / (name + '.svg')).read_text(errors='replace')[:LIMIT * 2])
    except OSError:
        return None


def draw(label, hint='', run=subprocess.run):
    """Ask Claude for an icon; save it and return its name. Raises RuntimeError when Claude gives nothing usable."""
    label = ' '.join(str(label or 'chat').split())[:40]
    hint = ' '.join(str(hint or '').split())[:200]
    prompt = PROMPT.format(label=label.replace('"', "'"), hint=f' The person asks for: {hint}.' if hint else '')
    env = dict(os.environ, ENABLE_CLAUDEAI_MCP_SERVERS='false', CLAUDE_CODE_AUTO_CONNECT_IDE='0')
    env.pop('TMUX', None)
    env.pop('TMUX_PANE', None)
    try:
        result = run(['claude', '-p', prompt, '--model', MODEL, '--output-format', 'text', '--tools', '',
                      '--settings', '{"disableAllHooks":true}', '--no-session-persistence',
                      '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}'],
                     cwd=str(paths.HOME), env=env, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('Claude could not draw the icon.') from exc
    svg = clean(result.stdout) if result.returncode == 0 else None
    if not svg:
        raise RuntimeError('Claude did not return a usable icon. Try again.')
    name = 'd' + hashlib.sha256(svg.encode()).hexdigest()[:11]
    folder().mkdir(parents=True, exist_ok=True)
    (folder() / (name + '.svg')).write_text(svg)
    return name
