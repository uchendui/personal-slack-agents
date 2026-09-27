# Setup and configuration

## What `setup/setup-slack-bridge.sh` does

- Checks that every required tool file exists.
- Installs the Slack CLI when `slack` is not on `PATH`.
- Builds a private virtualenv under `~/.local/share/slack-bridge`.
- Links the tools into `~/.local/bin` (`slack-bridge`, `slack-register`, `slack-spawn`, `slack-send`, `slack-forks`, `slack-sweep`, `slack-admin`, and the brokers).
- Writes the systemd user units: the bridge, a keep-alive timer every 6 hours, and an hourly sweep where the admin token exists.
- Enables linger so the units run without an interactive login, and restarts the bridge.

## Configuration

- `~/.config/slack-bridge/agents/<name>.env` holds each agent's tokens. It is written by `slack-register` and never belongs in this repository.
- `~/.config/slack-bridge/operator.txt` holds the Slack user ids allowed to use controls, one per line. The first `slack-register` on a machine writes it with the Slack CLI user's id.
- `SLACK_DEFAULT_CHANNEL` names the channel every agent joins on registration (default `all-agents`).
- `SLACK_TEAM_ID` names the workspace for `slack-admin`. When it is unset, the only logged-in workspace is used.
- `SLACK_ADMIN_APP_NAME` names the admin Slack app (default `slack-admin`).
- `CLAUDE_PTY_IDLE_COMPACT_SECONDS` sets the idle time before a Claude Code session's automatic `/compact` (default `600`; `0` turns it off).
- `DELIVER_CHANNEL_MESSAGES=true` in an agent's env file delivers channel messages that do not mention it.
- `CLAUDE_ACCOUNT_CYCLE` is a colon-separated list of Claude config directories to switch between when a session hits a usage limit; the session's own `CLAUDE_CONFIG_DIR` must be in the list (default unset, which turns switching off).
- `CLAUDE_ARGS` in an agent's env file holds the flags used when the bridge reopens that session (default: the `--model`/`--effort` of the Claude process that ran `slack-register`, or `--claude-args` when given).
- `slack-sweep --hours` sets how long a bot app must be inactive before the sweep deletes it (default `72`).

<p align="center"><img src="images/slack-register-help.png" width="720" alt="slack-register --help output"></p>

## Create an agent from Slack

```
slack-spawn <name> --tmux-session <session> [--join <channel>] [--launcher "<command>"] [--claude-config-dir <dir>] [--model <model>] [--team <team id>]
```

- It registers a new agent and starts a Claude Code session for it.
- It opens a window named after the agent in that tmux session, in that session's directory.
- It binds the session to the agent with `claude --session-id` and `--name`.
- A folder that Claude has not trusted yet shows the trust prompt once.
- Every spawned session runs with `--dangerously-skip-permissions`.
- When the bridge restarts a session, it starts it through the same launcher.

`--claude-config-dir` picks the Claude config directory, which is the account. It defaults to `CLAUDE_CONFIG_DIR`.
`--launcher` is the command that starts Claude Code: `claude` (the default) or a command prefix such as `ccr cc-work cli --`.
Its first word must be on `PATH` when you run `slack-spawn`.
`slack-spawn` stores that word as an absolute path, together with your `PATH` at that time, and every restart uses both.
The session then runs `<launcher> --session-id <id> --name <name> <flags>` behind `claude-pty-broker`.
`--model` sets the model for the new session.

Start an agent through a claude-code-router profile named `cc-work`:

```
slack-spawn my-agent --tmux-session work --launcher "ccr cc-work cli --" \
  --claude-config-dir ~/.claude-code-router/profiles/cc-work/claude --model "<provider>,<model>"
```

### Several accounts

Each account is its own Claude config directory, such as `~/.claude-work` and `~/.claude-personal`.
Log in to each one once: run `CLAUDE_CONFIG_DIR=~/.claude-work claude`, then `/login`.
A spawned agent picks its account with `--claude-config-dir`.
An account routed through claude-code-router (Codex or Gemini) uses `--launcher "ccr <profile> cli --"` with that profile's `claude` directory.

```
slack-spawn work-agent --tmux-session work --claude-config-dir ~/.claude-work
slack-spawn home-agent --tmux-session work --claude-config-dir ~/.claude-personal
slack-spawn codex-agent --tmux-session work --launcher "ccr cc-codex cli --" \
  --claude-config-dir ~/.claude-code-router/profiles/cc-codex/claude
```

### Codex and Gemini

`setup/setup-model-router.py` installs claude-code-router 3.0.20. It always makes `cc-codex` for your Codex login, and makes `cc-gemini` only when `~/.gemini_api_keys` exists.

Requirements:
- Node.js 22 or newer, and npm.
- For Codex: the `codex` CLI.
- For Gemini: your API keys in `~/.gemini_api_keys`, separated by commas or whitespace. A local router on 127.0.0.1:3460 rotates them.

```
python3 setup/setup-model-router.py
slack-spawn codex-agent --tmux-session work --launcher "ccr cc-codex cli --" \
  --claude-config-dir ~/.claude-code-router/profiles/cc-codex/claude
slack-spawn gemini-agent --tmux-session work --launcher "ccr cc-gemini cli --" \
  --claude-config-dir ~/.claude-code-router/profiles/cc-gemini/claude
```

## Two or more computers on one Slack account

Agents on different computers can share one Slack workspace and talk in one thread.
`slack login` gives every login of one Slack user the same token pair.
When one computer rotates that pair, the refresh token on the other computers stops working.
The fix is a service token per computer, which never expires and is separate from the login pair:

1. Run `slack auth token` on the computer.
2. Paste the `/slackauthticket` line it prints into Slack and approve it.
3. Run `slack auth token --challenge <code> --ticket <ticket>`.
4. Save the printed token: `umask 077; printf '%s\n' '<token>' > ~/.config/slack-bridge/service-token-<TEAM_ID>`.

`slack-register` and its keep-alive then use that token and never rotate the login pair.
Reference: https://docs.slack.dev/tools/slack-cli/reference/commands/slack_auth_token/
