#!/usr/bin/env bash
# Install and start the Gemini key router (tools/gemini-key-router.py) as a
# systemd user service, and create its bearer token. Every Gemini-backed
# runtime here (cc-gemini via CCR, the Antigravity CLI wrapper) talks to
# 127.0.0.1:3460 with this token; the router rotates the keys in
# ~/.gemini_api_keys and retries 5xx/429. Idempotent; needs no sudo.
set -euo pipefail

tools="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/tools"
bin="$HOME/.local/bin"
units="$HOME/.config/systemd/user"
token_file="$HOME/.claude-code-router/gemini-key-router-token"
mkdir -p "$bin" "$units" "$(dirname "$token_file")"
chmod 700 "$(dirname "$token_file")"

[ -f "$tools/gemini-key-router.py" ] || { echo "missing $tools/gemini-key-router.py" >&2; exit 1; }
if [ ! -s "$token_file" ]; then
  /usr/bin/python3 -c 'import secrets, sys; open(sys.argv[1], "w").write(secrets.token_urlsafe(32) + "\n")' "$token_file"
fi
chmod 600 "$token_file"
[ -f "$HOME/.gemini_api_keys" ] || echo "note: $HOME/.gemini_api_keys is missing; the router starts but has no keys to rotate" >&2

rm -f -- "$bin/gemini-key-router"
ln -s -- "$tools/gemini-key-router.py" "$bin/gemini-key-router"

cat >"$units/gemini-key-router.service" <<'UNIT'
[Unit]
Description=Gemini API key router (127.0.0.1:3460)
[Service]
Type=simple
UMask=0077
ExecStart=/usr/bin/python3 %h/.local/bin/gemini-key-router
Restart=on-failure
RestartSec=2s
[Install]
WantedBy=default.target
UNIT

# An ad-hoc router started by hand holds the port; the unit replaces it.
pkill -f "$tools/gemini-key-router.py" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now gemini-key-router.service
systemctl --user restart gemini-key-router.service

token="$(<"$token_file")"
for _ in $(seq 1 50); do
  if curl -fsS -m 2 -H "x-goog-api-key: $token" http://127.0.0.1:3460/__ccr_gemini_key_router__/health >/dev/null 2>&1; then
    echo "gemini-key-router: healthy on 127.0.0.1:3460 (systemd user service)"
    exit 0
  fi
  sleep 0.2
done
echo "gemini-key-router did not become healthy; see: systemctl --user status gemini-key-router" >&2
exit 1
