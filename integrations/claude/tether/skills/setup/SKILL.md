---
name: setup
description: Guide Tether setup when the user says "set up tether", "install tether", or "update tether settings".
---

# Set up Tether

Use the repository's `install.py` for all installation work, dependencies, services, and hooks.
Keep this skill a guide; do not write service files or agent settings yourself.

## Find the repository and show defaults

1. Check the current directory and its parents for Tether's root `install.py`, `server/`, and `ui/`.
2. If inside Tether, use that checkout without a location question.
3. Otherwise, ask where to use or clone Tether; offer `~/tether` as the default.
4. Reuse a Tether checkout at that location, or clone `https://github.com/incmak/tether.git` there.
   Preserve existing files if that location contains another project.
5. Read `install.py` and run `python3 install.py --help` from the repository root.
6. Run `python3 install.py --plan --json` with no option overrides.
7. Show a short table from the JSON: URL, host/auth, ports, features, reused choices, checks, legacy units, and planned changes.
   Include failed checks and their `fix` text; exit code zero alone does not prove readiness.

## Ask once for choices

Use one `AskUserQuestion` call with at most four questions.
Use the baseline plan's feature choices as defaults and label them in the question.
Do not ask for ports, dependencies, or build choices unless the plan reports a failure that requires a decision.

1. **Features**, `multiSelect: true`: interactive terminal, voice-to-text, push notifications, usage hooks, host resources.
   Explain briefly: terminal needs ttyd; voice adds a local transcription worker; push needs browser support and HTTPS. Resources shows host usage in Settings.
   Usage hooks add status lines only where the installer permits them.
   An empty selection disables all five features.
2. **Phone access**, single choice: Tailscale / same Wi-Fi LAN / HTTPS reverse proxy / this computer only.
   Recommend Tailscale for phone access; this computer only offers no remote phone access.
   Include the LAN plain-HTTP warning below in this choice.
3. **Updates**, single choice: notify / auto / off; recommend notify.
   Pass it as `--updates notify|auto|off`; `auto` adds a daily update timer.
   A git checkout updates with `git pull`; release installs use `tether update`.
4. **Legacy units**, only when `legacy_units` is nonempty: keep / replace; default keep.
   List the reported units and explain that replace disables and stops them.

## Map choices, review the diff, and apply

Use an explicit flag for each feature, including features the user disables:

| Choice | Selected | Not selected |
| --- | --- | --- |
| Interactive terminal | `--terminal` | `--no-terminal` |
| Voice-to-text | `--voice` | `--no-voice` |
| Host resources | `--resources` | `--no-resources` |
| Push notifications | `--push` | `--no-push` |
| Usage hooks | `--hooks` | `--no-hooks` |

| Phone access | Installer flags |
| --- | --- |
| Tailscale | `--host 127.0.0.1 --auth token` |
| Same Wi-Fi LAN | `--host 0.0.0.0 --auth token` |
| HTTPS reverse proxy | `--host 127.0.0.1 --auth proxy` |
| This computer only | `--host 127.0.0.1 --auth token` |

1. Retain the baseline chat port through `--port` unless the user chooses another port to resolve a failed check.
   Leave the UI build choice at the installer's default; it builds missing or stale assets.
2. Add `--replace-legacy` only when the user selects replace.
3. Re-run `python3 install.py --plan --json` with the chosen flags.
4. Show a short before/after table for changed options, checks, units, commands, hooks, and legacy actions.
   State that apply can install dependencies, build the UI, save choices, and start or restart the listed services.
5. Resolve failed checks before apply; keep necessary follow-up questions limited to the reported failure.
6. Ask for an explicit yes to apply this reviewed plan, including any legacy replacement.
   Wait for the answer; a menu choice does not authorize apply.
7. After yes, run `python3 install.py --apply --json` with exactly the reviewed flags.
   Report a failed command as a failure; do not claim setup succeeded.
   If the returned plan differs materially from the review, report the difference.

## Print the result and phone steps

Print the applied plan's `url`, `token_file` location, selected features, and updates choice.
Never print the token itself.
Explain that proxy mode uses the proxy's sign-in rather than the Tether token.
Use the returned port in examples; do not treat a wildcard or loopback URL as a phone address.

- **Tailscale:** connect the computer and phone to the same tailnet.
  Show `tailscale serve http://127.0.0.1:<port>` as a command for the user to run.
  Open the HTTPS address that Serve prints, with `/chat/` appended, then sign in with the Tether token.
  Follow any HTTPS enablement prompt from Serve.
  See [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve).
- **Same Wi-Fi LAN:** connect both devices to the same trusted Wi-Fi network.
  Open `http://<computer-LAN-IP>:<port>/chat/` and sign in with the Tether token.
  Warn: plain HTTP sends the token and chat without encryption; push and home-screen installation need HTTPS.
- **HTTPS reverse proxy:** use an HTTPS proxy that requires sign-in and forwards to the loopback chat port.
  Link to [the nginx auth proxy example](https://github.com/incmak/tether/blob/main/server/deploy/examples/nginx-auth-proxy.conf).
  Also identify the local file `server/deploy/examples/nginx-auth-proxy.conf`; replace its example port with the chosen port.
  Open the configured HTTPS `/chat/` URL on the phone and use the proxy's sign-in.
  Mark the public URL pending if the proxy is not yet configured.
- **This computer only:** open the returned loopback URL on this computer and sign in with the Tether token.
  State that the phone cannot reach this mode.

Start an agent inside tmux, then select its session in Tether.
For HTTPS phone access, offer Add to Home Screen and notification permission when push was selected.
Leave network setup as the printed next steps; installer success alone does not prove phone access.
