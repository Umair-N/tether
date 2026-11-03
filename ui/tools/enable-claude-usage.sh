#!/usr/bin/env bash
# Make the Claude status line also save its usage report for Tether (claude_usage.py). Safe to run twice.
set -euo pipefail
# Where claude_usage.py lives: TETHER_SERVER_DIR, or the sibling server/ folder of ui/.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
server_dir="${TETHER_SERVER_DIR:-$here/../server}"
if [ ! -f "$server_dir/claude_usage.py" ]; then
  echo "Cannot find claude_usage.py in '$server_dir'. Set TETHER_SERVER_DIR to the Tether server folder and run again." >&2
  exit 1
fi
server_dir="$(cd "$server_dir" && pwd)"
export TETHER_SERVER_DIR="$server_dir"
settings="$HOME/.claude/settings.json"
cp "$settings" "$settings.bak-tether"
python3 - "$settings" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
command = data.get('statusLine', {}).get('command', '')
if 'claude_usage.py' in command:
    print('Already on. Nothing changed.'); sys.exit()
import os
hook = 'printf %s "$input" | /usr/bin/python3 ' + os.path.join(os.environ['TETHER_SERVER_DIR'], 'claude_usage.py') + ' >/dev/null 2>&1;'
if command.startswith('input=$(cat);'):
    command = 'input=$(cat); ' + hook + command[len('input=$(cat);'):]
else:
    sys.exit('Status line has an unexpected shape. Nothing changed.')
data['statusLine']['command'] = command
open(path, 'w').write(json.dumps(data, indent=2) + '\n')
print('Done. Backup: ' + path + '.bak-tether')
PY
