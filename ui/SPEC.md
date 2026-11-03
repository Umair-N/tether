# tmux-chat — spec

A phone-first chat UI over the tmux sessions on this machine. Layout like the Claude mobile app.
UI wrapper only: no changes to Claude Code, Codex, or the backend's behavior.

## Stack (already installed — do not add dependencies without a clear need)

- Vite 6 + React 19 + TypeScript, Tailwind v4 (`@tailwindcss/vite`), `lucide-react` icons.
- `@assistant-ui/react` 0.15 with `useExternalStoreRuntime` (custom backend, no LLM API), `@assistant-ui/react-markdown` + `remark-gfm`.
- Read the types in `node_modules/@assistant-ui/react/dist/*.d.ts` for exact APIs. Do not guess.

## Serving

- Built with `base: '/chat/'`. Production: the `dist/` folder is served at `https://<host>.<tailnet>.ts.net/chat/`.
- The backend is on the SAME origin at `/mobile/api/*`. Use relative fetches: `fetch('/mobile/api/sessions')`.
- Dev: in `vite.config.ts`, proxy `/mobile` to `http://127.0.0.1:7683` (changeOrigin false, keep Host).
- Auth: an HttpOnly cookie set by `/mobile/login`. If any API call returns 401, show a full-screen "Sign in" state with a link to `/mobile/login` (a plain link; the backend login page redirects to `/mobile/` afterwards — that is fine, user then opens `/chat/` again). Use `credentials: 'same-origin'`.
- Every POST must send headers `Content-Type: application/json` and `X-Codex-Mobile: 1` (backend rejects without it).

## Backend API (Mobile Tmux, Python, at 127.0.0.1:7683) — real payloads

### `GET /mobile/api/sessions` → sidebar list. Poll every 4 s.
```json
{"sessions":[{"id":"%10","tmux_session":"$6","label":"6","agent":"claude","thread":"d104…","writable":true,
  "dir":"~/Documents/projects/example-project","state":"Idle","approval":null,"suggestion":"…"}],
 "default_thread":"…"}
```
- `id` = tmux pane id (the key for everything). `label` = session name (`"ESEF:0.1"` when a session has several panes).
- `agent`: `"claude" | "codex" | "antigravity" | "shell"`. `state`: `"Idle" | "Working" | "Approval" | "Unavailable" | …` (treat unknown strings as plain text).

### `GET /mobile/api/chat?session=<url-encoded pane id>&limit=40`
```json
{"messages":[{"id":"2451","role":"user","text":"…","time":"2026-10-29T13:45:08.981Z"},
             {"id":"2534.0","role":"assistant","kind":"actions","text":"Read x.png\nWebSearch …","count":"1","time":"…"},
             {"id":"2454","role":"assistant","text":"markdown …","time":"…"}],
 "busy":false,"thread":"d104…","agent":"claude","writable":true,"session":"%10","earlier":271,"limit":40,
 "state":"Idle","approval":null,"suggestion":"…"}
```
- `kind:"actions"` = tool calls. Render as a compact collapsed row ("Used 6 tools ▸", expands to the lines of `text`). Not a bubble.
- `kind:"terminal"` (shell panes) = one terminal snapshot. Render as a monospace block, horizontally scrollable, scrolled to bottom.
- `earlier` > 0 → "Load earlier" button at the top: refetch with a bigger `limit` (+40).
- For `agent:"shell"` call with `&mode=shell` added.
- Live updates: poll this every 1.5 s for the open chat while the tab is visible (simple and reliable; SSE at `/mobile/api/stream` exists but skip it for v1).

### `POST /mobile/api/message` body `{"session":"%10","thread":"<thread from chat payload, may be null>","message":"text"}`
- For shell panes add `"mode":"shell"`.
- 200 `{"accepted":true,"delivery":"submitted"|"queued"|"shell"}`. 409 = thread changed → refetch chat and show a toast. 503 `{"error":"…"}` → show the error under the composer, keep the draft.

### `POST /mobile/api/start` body `{"session":"%12","thread":null,"agent":"claude"|"codex"}`
Starts an agent in a pane that is at an idle shell prompt.

### `POST /mobile/api/tmux-sessions` body `{"name":"my-task"}` → 201 `{"session":"$13","name":"my-task"}`
Name must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` (validate client-side). Creates a tmux session in `~` with a shell.

### `POST /mobile/api/key` body `{"session","thread","key"}` key ∈ `Up Down Left Right Enter Escape Tab Space y n 1..9`
### `POST /mobile/api/approval` — skip for v1; when `approval` is non-null, show a banner with buttons that send `key` `1`/`2`/`3` and `Escape` instead.

## UX (Claude mobile app pattern)

1. **Sidebar = drawer** on phones (hamburger top-left), fixed column ≥ 900px wide. Top: "New session" button. List: one row per pane: label, agent icon/name, folder (`dir`, truncated from the left), a small state dot (Working = animated, Approval = attention color). Sort: Working/Approval first, then by label.
2. **New session flow**: tap "New session" → sheet asks for a name (prefilled e.g. `chat-1031-1642`) and a choice: **Claude**, **Codex**, **Shell**. On submit: POST tmux-sessions → poll sessions until a pane with that `label` appears (≤ 5 s) → if Claude/Codex, POST start for that pane → open it.
3. **Pane with `agent:"shell"`** and no messages: show the same three choices inline at the top ("Start Claude", "Start Codex", or just type a command). Typing in the composer sends with `mode:"shell"`.
4. **Chat**: user messages = right-aligned soft bubbles; assistant = full-width markdown text (no bubble), like the Claude app. Code blocks scroll horizontally. Auto-scroll to bottom on new messages unless the user scrolled up (show a "↓" jump button then).
5. **Header**: session label + agent + state. A "keys" menu (⋯) with Esc, Enter, Up, Down, Tab, y, n — for answering TUI prompts.
6. **Composer** pinned to bottom, respects `env(safe-area-inset-bottom)` and the on-screen keyboard (use `100dvh`). Multi-line textarea, Enter = newline on touch devices, Send button. **Mic button**: use the Web Speech API (`webkitSpeechRecognition`) when available, interim text appears in the textarea, tap again to stop; hide the button when unsupported. Placeholder: "Message <label>" or "Run a command" for shell.
7. While `state === "Working"` show a subtle "Working…" line under the last message.

## Look

Warm, calm, readable — like the Claude app, without any Anthropic/Claude logos or brand names in the chrome.
- Light and dark via `prefers-color-scheme`. Light: warm off-white background (#F7F5F0-ish), near-black text. Dark: warm charcoal (#262624-ish), not pure black.
- One accent color (terracotta, e.g. #C96442) for primary buttons and the mic-active state only.
- Body 16px (prevents iOS zoom), assistant text line-height 1.6. System UI font for chrome; a serif (`ui-serif, Georgia`) for assistant message text is OK.
- Tap targets ≥ 44px. Visible focus rings. Respect `prefers-reduced-motion`.
- No gradients, no glassmorphism, no emoji in UI chrome.

## PWA

- `public/manifest.webmanifest` (name "tmux chat", start_url `/chat/`, scope `/chat/`, display standalone, theme colors) and a simple SVG/PNG icon you draw (no logos). Link it in `index.html`. Set `<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">`. No service worker for v1.

## Done when

- `npm run build` passes with no TypeScript errors, and `npx tsc -b` is clean.
- Remove the Vite starter files/assets you don't use.
- Short `README.md`: how to dev (`npm run dev`), build, and that production is served by Tailscale Serve at `/chat/`.
- Do NOT start long-running servers and leave them running. Do NOT call POST endpoints against the real backend (they type into the user's real terminals). GET calls for checking shapes are fine.
