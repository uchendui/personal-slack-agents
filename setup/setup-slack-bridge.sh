#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
root="$(cd -- "$script_dir/.." && pwd -P)"
tools="$root/tools"
state="$HOME/.local/share/slack-bridge"
venv="$state/venv"
bin="$HOME/.local/bin"
units="$HOME/.config/systemd/user"

required=(slack_bridge.py slack_register.py slack_spawn.py slack_sweep.py slack_admin.py slack_send.py slack_forks.py slack_api.py slack_live_delivery.py pty_broker.py claude-pty-broker.py antigravity-pty-broker.py antigravity-send.py)
for file in "${required[@]}"; do
  [[ -f "$tools/$file" ]] || { printf 'Missing required file: %s\n' "$tools/$file" >&2; exit 1; }
done
command -v python3 >/dev/null || { printf 'python3 is required.\n' >&2; exit 1; }
# slack-register uses ~/.config/slack-bridge/service-token-<team> from
# `slack auth token` when it exists, else ~/.slack/credentials.json from
# `slack login`; the installer puts the Slack CLI under ~/.slack/bin.
command -v slack >/dev/null || curl -fsSL https://downloads.slack-edge.com/slack-cli/install.sh | bash
install -d -m 700 "$state" "$bin" "$units"
python3 -m venv --clear "$venv"
"$venv/bin/python" -m pip install --no-cache-dir 'websockets==10.4'
chmod -R go-rwx "$venv"

link() { rm -f -- "$2"; ln -s -- "$1" "$2"; }
link "$tools/slack_bridge.py" "$bin/slack-bridge"
link "$tools/slack_register.py" "$bin/slack-register"
link "$tools/slack_spawn.py" "$bin/slack-spawn"
link "$tools/slack_send.py" "$bin/slack-send"
link "$tools/slack_forks.py" "$bin/slack-forks"
link "$tools/slack_sweep.py" "$bin/slack-sweep"
link "$tools/slack_admin.py" "$bin/slack-admin"
link "$tools/claude-pty-broker.py" "$bin/claude-pty-broker"
link "$tools/antigravity-pty-broker.py" "$bin/antigravity-pty-broker"
link "$tools/antigravity-send.py" "$bin/antigravity-send"
link "$tools/pty_broker.py" "$bin/pty_broker.py"
# The bridge reads the agents directory and the registry at start, and
# slack-register creates them only at the first registration.
PYTHONPATH="$tools" "$venv/bin/python" -c 'import slack_register as r
with r._lock():
    r._directory(r.AGENTS_DIR, create=True)
    r.REGISTRY_PATH.exists() or r._save(r._registry())'

cat >"$units/slack-bridge.service" <<'EOF'
[Unit]
Description=Local Slack bridge
Wants=network-online.target
After=network-online.target
[Service]
Type=simple
UMask=0077
# Sessions the bridge restarts inherit this PATH and need slack-send and claude from ~/.local/bin.
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/share/slack-bridge/venv/bin/python %h/.local/bin/slack-bridge
Restart=on-failure
RestartSec=5s
TimeoutStopSec=90
KillMode=mixed
[Install]
WantedBy=default.target
EOF
cat >"$units/slack-token-rotate.service" <<'EOF'
[Unit]
Description=Keep Slack CLI credentials alive
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
UMask=0077
ExecStart=%h/.local/share/slack-bridge/venv/bin/python %h/.local/bin/slack-register --keep-alive
EOF
cat >"$units/slack-token-rotate.timer" <<'EOF'
[Unit]
Description=Periodically keep Slack CLI credentials alive
[Timer]
OnBootSec=15m
OnUnitActiveSec=6h
RandomizedDelaySec=10m
[Install]
WantedBy=timers.target
EOF
# The workspace-wide sweep runs only where the admin user token lives.
if [[ -f "$HOME/.config/slack-bridge/admin-user.env" ]]; then
  cat >"$units/slack-sweep.service" <<'EOF'
[Unit]
Description=Delete Slack apps of bots that stopped posting
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
UMask=0077
ExecStart=%h/.local/share/slack-bridge/venv/bin/python %h/.local/bin/slack-sweep
EOF
  cat >"$units/slack-sweep.timer" <<'EOF'
[Unit]
Description=Hourly sweep of inactive Slack bot apps
[Timer]
OnBootSec=30m
OnUnitActiveSec=1h
RandomizedDelaySec=5m
Persistent=true
[Install]
WantedBy=timers.target
EOF
  sweep_timer=(slack-sweep.timer)
else
  sweep_timer=()
fi
# Without linger the per-user systemd manager only starts at an interactive login,
# so an enabled user unit never runs after a reboot.
loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER"
[[ -e /var/lib/systemd/linger/$USER ]] || { echo "linger not enabled for $USER" >&2; exit 1; }
systemctl --user daemon-reload
systemctl --user enable slack-bridge.service
systemctl --user enable --now slack-token-rotate.timer
[[ ${#sweep_timer[@]} -eq 0 ]] || systemctl --user enable --now "${sweep_timer[@]}"
systemctl --user restart slack-bridge.service
