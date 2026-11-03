# Coding guide

Tether is a browser chat UI for Claude Code, Codex, and Antigravity in tmux, for desktop, tablet, and phone.

## Layout and checks

- `server/`: Python standard-library server, agent adapters, fixtures, and tests.
- `ui/`: React/Vite UI. Production builds go to `ui/dist`, served at `/chat/`.
- `server/index.html` and `server/app.js`: legacy UI, served at `/mobile/chat/`.
- Server checks: `cd server && python3 -m unittest discover -q`.
- UI checks: `cd ui && npm ci && npx tsc -b && npx eslint . && npm run build`.
- Deployment and configuration: see [README.md](README.md).

## Rules and architecture

- Keep secrets, personal paths, and private host names out of the repository.
- Use `server/paths.py` and environment overrides for agent and state paths.
- Keep the core server free of third-party Python dependencies.
- Chat comes from agent transcripts. Pending prompts come from the tmux pane.
- Auth lives in `server/auth.py`. Preserve cookie checks, Origin checks, and the `X-Codex-Mobile` header for writes.
- Bind writes to the selected pane and conversation. Reject stale identities and preserve existing terminal drafts.
- Input checks live in `terminal_input.py`, `terminal_replies.py`, `model_picker.py`, and `shortcuts.py`, under `server/`.
- For agent parser changes, inspect record types and check behavior across all three agents.
- Tests use fixtures and isolated or mocked processes. Use mocks for UI writes; they control real terminals.
- Restart the backend after Python or legacy UI changes. Legacy assets load at startup; React assets come from `ui/dist`.
