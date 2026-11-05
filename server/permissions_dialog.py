"""Pure parsing of Claude Code's /permissions dialog captures."""
import re


SGR = re.compile(r'\x1b\[([0-9;]*)m')
TAB_ROW = re.compile(r'^\s*Permissions\s{2,}(Recently blocked|Allow)\b')
HINT = re.compile(r'\bEsc\b.*(?:cancel|close|appl(?:y|ies))|←/→.*(?:switch|tabs?)', re.I)


def highlighted_text(line, include_inverse=True):
    """Return text spans with a background or inverse, excluding color parameters."""
    background = inverse = False
    spans = []
    parts = []
    start = 0
    for match in SGR.finditer(line):
        if background or (include_inverse and inverse):
            parts.append(line[start:match.start()])
        codes = iter(int(code or 0) for code in match[1].split(';'))
        for code in codes:
            if code in (38, 48, 58):
                mode = next(codes, None)
                for _ in range(1 if mode == 5 else 3 if mode == 2 else 0):
                    next(codes, None)
                if code == 48:
                    background = True
            elif code == 0:
                background = inverse = False
            elif 40 <= code <= 47 or 100 <= code <= 107:
                background = True
            elif code == 49:
                background = False
            elif code in (7, 27):
                inverse = code == 7
        if not (background or (include_inverse and inverse)) and parts:
            spans.append(''.join(parts))
            parts = []
        start = match.end()
    if background or (include_inverse and inverse):
        parts.append(line[start:])
    if parts:
        spans.append(''.join(parts))
    return spans


def parse(screen: str, styled: str | None) -> dict | None:
    lines = SGR.sub('', screen).splitlines()
    rows = [index for index, line in enumerate(lines) if TAB_ROW.match(line)]
    if not rows:
        return None
    row = rows[-1]
    tabs = re.split(r'\s{2,}', lines[row].strip())[1:]
    styled_lines = styled.splitlines() if styled else []
    styled_rows = [index for index, line in enumerate(styled_lines) if TAB_ROW.match(SGR.sub('', line))]
    styled_row = styled_rows[-1] if styled_rows else None
    # The active tab has a background colour; once the list has focus, Claude shows it in inverse instead.
    tab = next((tab for inverse in (False, True) for span in highlighted_text(styled_lines[styled_row], include_inverse=inverse)
                for tab in tabs if span.strip() == tab), None) if styled_row is not None else None
    end = next((index for index in range(row + 1, len(lines)) if HINT.search(lines[index])), len(lines))
    body = lines[row + 1:end]
    while body and not body[-1].strip():
        body.pop()
    indent = min((len(line) - len(line.lstrip()) for line in body if line.strip()), default=0)
    body = [line[indent:] if line.strip() else '' for line in body]
    selected = None
    for index, line in enumerate(body):
        at = styled_row + 1 + index if styled_row is not None else len(styled_lines)
        if (line.lstrip().startswith(('❯', '›')) or
                at < len(styled_lines) and any(span.strip() for span in highlighted_text(styled_lines[at]))):
            selected = index
            break
    return {'tabs': tabs, 'tab': tab, 'lines': body, 'selected': selected,
            'hint': lines[end].strip() if end < len(lines) else '',
            'empty': tab == 'Recently blocked' and any('No recent blocks' in line for line in body)}
