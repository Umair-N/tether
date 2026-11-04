# Narrow screen captures

`screens.json` stores `capture-pane -e` output at widths 20, 22, 30, 40, and 80.
Each record names the agent, width, screen state, and source.

The native Codex record preserves the reported 22-column input line.
It contains a bold marker and a faint, cut placeholder.
A read-only capture confirmed the line before the fix.

The other records come from a private tmux server that prints known renderer sequences.
They cover Claude's faint suggestions, Codex's faint hints, and Antigravity's colour 109 hints.
They also cover typed text, physical wraps, menus, and approvals.
These are terminal replays, not native agent sessions at each width.
No agent, user pane, account, or service was changed to make these captures.

The private tmux regression test repeats the reported input sequence at 22 columns.
All other fixture tests need no tmux process.
