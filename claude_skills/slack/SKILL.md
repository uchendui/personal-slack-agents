---
name: slack
description: "Register this session as its own Slack bot identity on the machine's slack-bridge, so the operator and other agents can reach it by DM or @-mention. Use when the user invokes /slack."
---

# /slack — self-register this session on the Slack bridge

Give THIS session a Slack identity named after itself. After registration the
session is reachable as @<session-name> in the workspace: the operator DMs it
or mentions it in a channel, the bridge wakes a headless turn under the same
login profile, and replies land back in the Slack thread.

## Steps

1. Determine the agent name: THIS SESSION'S OWN NAME, shown by `/rename` or
   `ListAgents`, e.g. `claude-admin`. If the session has no name, ask the user
   for one. Never invent a name.
2. Determine the login profile this session runs under: the Claude config dir,
   the directory containing this project's memory path (for example
   `~/.claude-<profile>` for a subscription profile), and pass it
   explicitly.
   When `CLAUDE_CONFIG_DIR` is unset in this session, omit
   `--claude-config-dir`; the agent then runs with it unset too.
3. Use your agents workspace (`--team <TEAM_ID>`) unless the user explicitly names
   another workspace. Do not ask which workspace to use. Pass this team flag
   for every registration kind below. Run using background Bash:

   ```
   slack-register <name> --team <TEAM_ID> --kind claude \
     --workdir <this session's primary working directory> \
     --claude-config-dir <this session's config dir>
   ```

   For an Antigravity (agy) session, which is the case whenever the
   environment variable `ANTIGRAVITY_PTY_BROKER_PID` is set: skip step 2 and
   run `slack-register <name> --team <TEAM_ID> --kind antigravity --workdir <working directory>`
   with NO profile or config-dir flag; registration reads the session id from
   the PTY broker advertisement and refuses profile flags for this kind. agy
   has no `/rename`, so take the name from the user.
4. The script creates (or reuses) the Slack app, writes
   `~/.config/slack-bridge/agents/<name>.env`, and restarts the bridge.
5. Report to the user in one sentence: the handle, and that DM or @-mention
   now reaches this agent. If `slack-register` dies, report its exact error;
   the usual cause is a missing one-time `slack login` on this machine.

## Separately requested channel joins

`/slack` registers an identity and joins `#all-agents` (or the channel in `SLACK_DEFAULT_CHANNEL`); every other channel needs a
separately requested join. For such a join on an existing Claude identity, rerun the command
with `--join` and the same explicit team, workdir, and profile flags:

```
slack-register <name> --team <TEAM_ID> --kind claude \
  --workdir <this session's primary working directory> \
  --claude-config-dir <this session's config dir> --join <channel>
```

For another kind, use its registration form above and add `--join`; do not add
profile flags for Antigravity.
Never claim a channel was joined from a zero exit status alone. Report the
verified membership output, including the channel name and id, or check Slack
membership directly. Registration joins only `#all-agents` (or the channel in `SLACK_DEFAULT_CHANNEL`); `#admin` and
project channels need `--join`.

## Notes

- Registration is idempotent: the same name reuses its app and keeps its DM
  history (identity map: `~/.config/slack-bridge/registry.json`).
- Always pass `--team <TEAM_ID>` for your agents workspace, even when multiple
  workspaces are logged in. If that login is missing, report the error rather
  than selecting another workspace.
- To message peers from a turn:
  `slack-send --as <name> --channel '#general' --mention <peer> --text '...'`.
- When Slack reports its app cap, registration evicts the oldest retired app
  and retries without assuming a fixed cap.
