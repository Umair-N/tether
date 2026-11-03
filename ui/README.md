# tmux chat

A phone-first chat interface for the tmux panes on your server.

- Dev: `npm install`, then `npm run dev`. Open the printed URL at `/chat/`.
- Build: `npm run build`. Type check: `npx tsc -b`.
- Production: Tailscale Serve serves `dist/` at `/chat/` on the server origin.

The dev server proxies `/mobile` to `http://127.0.0.1:7683` and preserves the Host header. Production uses the same-origin `/mobile/api/` backend and its HttpOnly authentication cookie. Sign in at `/mobile/login`, then open `/chat/` again.

The PWA manifest supports installation. Version 1 has no service worker or offline mode. Speech input appears only when the browser supports speech recognition. Microphone access requires browser permission.

API writes control real terminals. Use mocks for interaction checks; do not test POST requests against the live backend.
