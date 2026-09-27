# Slack bridge — required functionality

The whole spec. If it's not here, don't build it.

## Messaging
- An agent registered on this machine gets its own Slack bot identity; people
  reach it by DM, @-mention in a channel, or reply in one of its threads.
  @channel and @here broadcasts count as mentions for every agent in the
  channel — a broadcast exists to notify everyone. A
  thread is the agent's if any message in it (root included) @-mentions the
  agent or was posted by it, so replies from the operator or its agents
  keep reaching every agent mentioned in the thread even before it posts.
- Only the operator (operator.txt or the agent's operator id) and the
  operator's agents reach an agent: bots registered on this machine, and bots
  whose Slack app the operator's Slack CLI login can manage
  (`apps.manifest.export` succeeds, checked once per app). Any other sender is
  dropped and logged.
- Replies land in the right thread: every addressed message is injected into
  the agent's live session the moment it arrives, tagged with its channel and
  thread timestamp.
- Duplicate-delivery protection is in-memory: retries never double-post while
  the bridge is running, and two sessions never both answer the same message.
  A bridge restart resets it; no ledger on disk.
- Channel messages that don't @-mention an agent are NOT delivered to it by
  default; `DELIVER_CHANNEL_MESSAGES=true` in an agent's env file opts it in.
  New registrations write `false`.
- The bridge adds an 👀 reaction to every message it successfully accepts for
  an agent — the read receipt; a message that gets refused or dropped never
  gets the reaction.
- After successful registration, the new identity DMs the operator
  `@<name> is online`; announcement failure is logged and does not roll back
  registration.
- Un-mentioned channel messages are never addressed: for an opted-in agent
  they are injected into the live session as background context, with no
  reply expected. Only DMs, @-mentions, and replies in the agent's threads
  are addressed messages.

## Turn execution
- Every addressed message is injected into the agent's live session as a
  `[Slack]` block carrying the agent name, bot user id, channel, thread
  timestamp, sender label, text, and local file paths. The live session
  spawns one subagent per message that needs a reply; that subagent does the
  work and posts through `slack-send --as <agent> --channel <C...> --thread
  <ts>`. Nothing else runs on the agent's behalf: no second CLI, no queue, no
  Slack history fetch — the session's own transcript is its context.
- Every recognized `!` control is intercepted before normal routing and never
  reaches the session as a message.
- Transport split: the PTY broker is for what only a terminal can do — the
  bridge injecting messages and slash-command controls (`/clear`,
  `/compact`, `/goal`, model/effort) into the live session, and `!stop`'s
  Ctrl+C interrupt. Sessions post replies by invoking `slack-send --as
  <agent>`; only that helper reads the owner-only credential file and calls
  the Slack API. Token text never enters a session's prompt, context, or
  environment, and the bridge never extracts or posts agent-generated replies
  (bridge-owned operational posts — warnings, refusals, lifecycle results —
  are its own).
- Slack message edits and deletions are ignored as events.
- If the agent has no live session: the bridge posts a dead-session warning
  once per thread in reply to a DM, @-mention, or thread reply (the message
  itself is dropped, since a dead agent can't answer); un-mentioned channel
  chatter is dropped silently with no warning.

## Files
- Files uploaded with an addressed message are downloaded (maximum 1 GiB per
  file, owner-only perms, into an owner-only temp directory) and their local
  paths handed to the session with the message text. A message with a file
  over the cap is refused outright: the bridge posts an explicit refusal in
  the thread and delivers nothing from that message — no partial text-only
  delivery. A download failure on the current message refuses the whole
  message, like over-cap. Background chatter is text-only — no downloads for
  messages expecting no reply.
- `slack-send --as <agent> --channel <#name|id> [--thread <ts>]
  [--mention <agent>] [--text <msg>|stdin] [--file <path>]` posts from the
  shell; `--mention` wakes the named local agent through the bridge.

## Controls
- Syntax: `@<agent> !<command> <optional-text>` — e.g. `@my-agent !unregister`,
  `@my-agent !effort high`, `@my-agent !goal ship the rebuild`.
- Commands: model, effort, compact, stop, rename, unregister,
  `!goal <text>` (set), `!clear-goal` (clear), `!clear` (clear the parent
  conversation's context). Three kinds: slash-command injections typed into
  the parent's live CLI session (`!goal X` → `/goal X`,
  `!clear-goal`, `!clear`, `!compact`, model/effort); lifecycle operations the
  bridge executes itself through the registration code (`!rename`,
  `!unregister` — same path as `slack-register --unregister`); and `!stop`
  (PTY interrupt, below). The session's own context is the only place a goal
  lives — the bridge stores nothing.
- `!stop` interrupts the live session immediately: send the interrupt
  (Ctrl+C equivalent) through the PTY broker. Not a stop-at-next-turn marker.
- Only the operator (ID in ~/.config/slack-bridge/operator.txt) can issue
  controls or authorize protected actions; a non-operator control attempt
  gets an explicit refusal in that thread.
- Injected slash-command controls post no Slack acknowledgment (their output
  renders in the terminal); bridge-executed lifecycle operations post
  explicit success or failure.

## Registration and lifecycle
- `slack-register <name> --kind <claude|antigravity> --workdir <dir>` with
  Claude profile flags (`--claude-config-dir`/`--claude-args`/`--launcher`;
  a Codex model runs as kind claude with `--launcher "ccr <profile> cli --"`), `--team` when several workspaces are logged
  in, `--join` for channels (default all-agents, set by SLACK_DEFAULT_CHANNEL); also `--unregister`,
  `--list`, `--keep-alive`. Reuses or creates Slack apps, auto-evicting the
  oldest retired app when Slack's own app cap is hit (no fixed number in this
  spec; the registration code handles it); writes the agent env file,
  all-or-nothing on failure. Re-registering a valid existing name touches no
  operator credential and updates only its session-specific fields.
- A running terminal session advertises itself as an agent's live session by
  invoking the `/slack` skill; the bridge injects only into advertised,
  verified sessions.
- Agent bot and app tokens are permanent. The existing slack-token-rotate
  systemd user timer instead runs `--keep-alive` to refresh the operator CLI
  token when needed (OnBootSec=15m, OnUnitActiveSec=6h,
  RandomizedDelaySec=10m); rename retires the old identity.
- Two computers on one Slack user share one `slack login` token pair, so a
  rotation on one computer invalidates the refresh token on the other. Each
  computer instead holds a Slack service token from `slack auth token` (paste
  the printed `/slackauthticket` line into Slack, approve, then run
  `slack auth token --challenge <code> --ticket <ticket>`), saved with mode 0600
  to `~/.config/slack-bridge/service-token-<team>`. A service token never
  expires; when the file exists, registration and `--keep-alive` use it and
  never rotate the login pair. Without `--team`, the single service-token file
  selects the team. `--kind` is exactly
  claude|antigravity. `slack-send` never
  auto-retries a post; a lost response is the same accepted rare-duplicate
  class as inbound.
- The bridge runs as a systemd user service. On stop it delivers nothing new,
  closes its Slack connections, and exits.

## Antigravity CLI setup
Install `agy` into `$HOME/.local/bin`, create its settings file at
`$HOME/.gemini/antigravity-cli/settings.json`, and run
`setup/setup-slack-bridge.sh` to install the PTY and send helpers.
Register the running `agy` session with `slack-register --kind antigravity`.

A message addressed to an Antigravity agent is answered by a reply fork,
one per thread: a new `agy` process the bridge starts with `GEMINI_API_KEY` set to the contents
of `$HOME/.claude-code-router/gemini-key-router-token` and
`GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:3460`. A Gemini router must listen
on that port and accept that token. When the file is missing or empty, the
fork does not start: the bridge logs `fork worker failed to start` with the
cause and types the message into the live Antigravity session instead. If
that also fails, it posts `Reply worker failed to start` in the thread.

## Security (all fail loud, never fall back)
- Verify a live session before injecting: socket ownership and mode, process
  identity and start time; refuse duplicates and stale entries.
- The registry and agent env files validate on load; unreadable or
  wrong-shape entries kill the run. (Other bridge state on disk: `last-seen.json`
  and `forks.json` under `~/.config/slack-bridge`, and `last-post.json` and the
  broker files under `~/.local/state/slack-bridge`; an invalid `last-seen.json`
  kills the run.)
- No secrets in logs, posts, or committed files.

## Formats
- Keep existing CLI syntax, env-file schema, and registry schema, so running
  agents survive the swap. Settings changes
  are slash commands applied to the live session, goals live in the session's
  context, thread membership is derived from Slack, and duplicate-delivery
  protection is in-memory.
