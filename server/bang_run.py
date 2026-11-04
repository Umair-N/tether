"""Read the live output of a Claude bang command from a pane capture."""
import re


RULE = re.compile(r'^\s*─{3,}')
BACKGROUND = re.compile(r'^\s+\(ctrl\+b.*to run in background\)\s*$')
SECONDS = re.compile(r'^\s+\((\d+)s\)\s*$')
# Claude Code writes "  ⎿" then a normal or no-break space; the first seconds show only "⎿  Running…".
FIRST = re.compile(r'^  ⎿[ \xa0]{1,2}')
STARTING = re.compile(r'^  ⎿[ \xa0]+Running…\s*$')


def extract(screen: str) -> dict | None:
    """Return the last running command above the input box, if visible."""
    if not isinstance(screen, str):
        return None
    lines = screen.splitlines()
    prompts = [i for i, line in enumerate(lines)
               if line.startswith('❯') and i and RULE.match(lines[i - 1])]
    if not prompts:
        return None
    stop = prompts[-1] - 1
    commands = [i for i, line in enumerate(lines[:stop]) if line.startswith('! ')]
    if not commands:
        return None
    start = commands[-1]
    block = lines[start + 1:stop]
    if not any(BACKGROUND.match(line) or STARTING.match(line) for line in block):
        return None
    seconds = None
    output = []
    in_output = False
    for line in block:
        elapsed = SECONDS.match(line)
        if elapsed:
            try:
                seconds = int(elapsed[1])
            except ValueError:
                pass
        elif BACKGROUND.match(line) or STARTING.match(line):
            continue
        elif FIRST.match(line):
            in_output = True
            output.append(line[FIRST.match(line).end():])
        elif in_output and line.startswith('     '):
            output.append(line[5:])
        else:
            in_output = False
    return {'command': lines[start][2:], 'output': '\n'.join(output[-40:]), 'seconds': seconds}
