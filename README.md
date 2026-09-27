<h1 align="center">Personal Slack agents</h1>

<p align="center"><b>DM your own Claude Code, Codex, and Gemini sessions from Slack, and they answer in the thread.</b></p>

<p align="center">
  <img alt="tests" src="https://img.shields.io/badge/tests-219%20passing-2ea44f">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776ab">
  <img alt="runtimes" src="https://img.shields.io/badge/runtimes-Claude%20Code%20%7C%20Antigravity-6f42c1">
</p>

Each command-line agent session you run in tmux gets its own Slack bot.
DM the bot or @-mention it, and the message lands in the live session as if you typed it; the answer comes back in the thread.

Agents run in Claude Code (recommended) or Antigravity. To use Codex, Gemini, or another model, run it inside Claude Code through [claude-code-router](https://github.com/musistudio/claude-code-router).

<p align="center"><img src="docs/images/test-fix.gif" alt="A Slack thread on the left asks an agent to fix a failing test; the agent's tmux pane on the right edits the file; the diff is posted back in the thread"></p>

## What you can do

| In Slack | What happens |
|---|---|
| DM or @-mention an agent | The message lands in its live session, and it answers in the thread. |
| Attach a file | The agent gets it as a local path. It can post files back. |
| `@agent !stop` | Interrupts the running task. |
| `@agent !effort high` or `!model <name>` | Changes the effort level or model of the live session. |
| `@agent !compact` or `!clear` | Compacts or clears the session context. |
| `@agent !goal <text>` or `!clear-goal` | Sets or clears a standing goal. |
| `@agent !rename <name>` or `!unregister` | Renames or removes the agent. |
| `@channel` or `@here` | Reaches every agent in the channel. |
| Mention another agent | Agents can talk to each other across computers in the same thread. |

Only the Slack users listed in `operator.txt` and bots whose Slack app they can manage reach an agent; messages from anyone else are dropped.
Only the operators can use the `!` controls. Each agent can use its own subscription. The full behavior is in [`SLACK_FEATURES.md`](SLACK_FEATURES.md).

## Quick start

**Requirements:**
- Linux with systemd user services, bash, tmux, git, curl, python3 (3.11 or newer), and python3-venv.
- A Slack workspace where you can create apps.
- `claude` or `agy` on `PATH` or in `~/.local/bin`.

```bash
git clone https://github.com/uchendui/personal-slack-agents ~/personal-slack-agents
cd ~/personal-slack-agents
setup/setup-slack-bridge.sh          # venv, ~/.local/bin links, systemd units, bridge start
export PATH="$HOME/.local/bin:$PATH" # adds bridge tools and slack CLI to PATH
slack login                          # one-time Slack CLI login on this machine

tmux new-session -d -s work -c ~/your-project   # the agent's tmux session and working directory
slack-spawn my-agent --tmux-session work     # new agent in that session, with its own Slack bot
tmux attach -t work:my-agent                 # approve the folder trust and bypass-permissions prompts, then Ctrl-b d
```

Then DM `@my-agent` in Slack.

Paste [`docs/CLAUDE-slack.md`](docs/CLAUDE-slack.md) into your agents' `CLAUDE.md`. Those rules tell an agent when to answer, how to reply with `slack-send`, and when to stay quiet.

## Demos

<details>
<summary><b>Fix a bug from an attached crash log</b></summary>
<p>Attach <code>crash.log</code> and ask for the fix.</p>
<p align="center"><img src="docs/images/crash-log.gif" alt="an agent reads an attached crash log, names the bug, and fixes it"></p>
</details>

<details>
<summary><b>Files in, files out</b></summary>
<p>A file attached in Slack lands in the session as a local path.</p>
<p align="center"><img src="docs/images/file-in.gif" alt="an attached file reaches the session and the agent answers about it"></p>
<p>The agent posts files back with <code>slack-send --file</code>.</p>
<p align="center"><img src="docs/images/file-out.gif" alt="the agent uploads a PNG into the thread"></p>
</details>

<details>
<summary><b>Control the live session: <code>!effort</code>, <code>!stop</code>, <code>!goal</code></b></summary>
<p><code>!effort low</code> changes the effort level.</p>
<p align="center"><img src="docs/images/effort.gif" alt="!effort from Slack, confirmed in the thread, then a question answered"></p>
<p><code>!stop</code> interrupts the running task.</p>
<p align="center"><img src="docs/images/stop.gif" alt="!stop from Slack interrupts the agent mid-task"></p>
<p><code>!goal</code> sets a standing instruction for every later answer.</p>
<p align="center"><img src="docs/images/goal.gif" alt="!goal from Slack sets the session goal"></p>
</details>

<details>
<summary><b>Two agents on one repo at once</b></summary>
<p align="center"><img src="docs/images/two-agents.gif" alt="one Slack message gives demo-agent and demo-helper one bug each; both fix their bug at the same time and post a diff in the thread"></p>
</details>

<details>
<summary><b>Ask an agent on another computer</b></summary>
<p align="center"><img src="docs/images/cross-computer.gif" alt="demo-agent on one computer asks kr-demo-agent on another computer in the same thread, then posts a comparison table"></p>
</details>

<details>
<summary><b>Create a new agent from Slack</b></summary>
<p align="center"><img src="docs/images/spawn.gif" alt="a new agent is spawned from Slack and answers in the thread"></p>
</details>

## More

- [What runs on its own](docs/automatic.md)
- [Setup and configuration](docs/setup.md)
- [Full behavior](SLACK_FEATURES.md)

## Stop or remove

Stop the bridge and its timers, now and at every login:

```bash
systemctl --user disable --now $(systemctl --user list-unit-files 'slack-*' --no-legend | cut -d' ' -f1)
```

Agent sessions keep running in tmux until you end them, for example with `tmux kill-session -t work`.
Start everything again with `setup/setup-slack-bridge.sh`.
Delete an agent's Slack app with `slack-admin delete-app my-agent`.

## Tests

```bash
~/.local/share/slack-bridge/venv/bin/python -m unittest discover -s tests -t .
```
