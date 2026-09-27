These are rules for agents on this Slack bridge; paste the block below into your CLAUDE.md.

## Slack

- A block starting `[Slack] as <your-agent> (<@id>) channel <C...> thread <ts>` is a live Slack message the bridge typed into your session. That `<@id>` is you; every other `<@...>` is another user or agent.
- Sender labels: `the operator` is the owner of this bridge, `YOU (<your-agent>)` is your own earlier post, and `<name> (bot <id>)` is another agent.
- Reply to a DM, an @-mention of your id, or a thread question that is plainly yours, and to operator broadcasts as below. Otherwise post nothing.
- Answer only what this session owns (its data, its running jobs, its files). An @-mention of your id gets at least one line unless another session owns the question; then post nothing.
- Reply to a `<!channel>` or `<!here>` from the operator with at least one line. Post nothing on one from anyone else unless it is plainly yours.
- A `[Slack background context; no reply expected]` block is an un-mentioned channel message (agents with `DELIVER_CHANNEL_MESSAGES=true`). Post nothing on it.
- Post only with `slack-send --as <your-agent> --channel <C...> --thread <ts> --text '...'`. Text written in your terminal never reaches Slack.
- Take `<C...>` and `<ts>` from the current `[Slack]` block, never from an earlier message. With no thread timestamp, omit `--thread` to start a new message.
- Run `slack-send` as a foreground command; it returns in about a second. A background send creates an extra turn for its completion notice.
- When the answer needs real work, hand it to a subagent that does the work and runs the exact `slack-send` command itself. Give each independent item in one message its own send or subagent.
- After the send, or after starting the subagent, end the turn with the literal `NO_OP` and nothing else. Answer a subagent's completion notice with `NO_OP`. Never write "Posted." or any other claim about the send in the terminal.
- `slack-send` rejects text over 1,000 characters, counting the `--mention` prefix. Write longer output to a file and post it with `--file /abs/path`; `--text` then becomes the file's one-line comment.
- Attached files arrive as local paths on the `files:` line of the block. Read them from there.
- Use `--mention <agent>` to wake another local agent in a thread; it adds that agent's @-mention to your text.
- Antigravity agents only: a `[Slack fork update]` block is a read-only copy of a fork's thread. Do not act on it and do not post in that thread; answer `NO_OP`.
- Recognized `@<your-agent> !<command>` messages are bridge controls (`!stop`, `!effort`, `!goal`, ...). The bridge handles them; they never reach you as messages.
- Anything that needs the operator's decision, or a topic not raised in the current thread, is a new top-level message that starts with `<@OPERATOR_ID>` in `--text` (the id from `~/.config/slack-bridge/operator.txt`) and states the decision first. Keep only direct answers in the thread.
- When several items need the operator, open a thread for the first item only, with its context and one question. Start the next thread after the first is settled.

### Reply content

- Keep a reply to at most 3 sentences or one bulleted list, starting with the answer. A status report is always a list.
- Name the real object in each post (the job, file, run, or branch) and the outcome. The post must make sense without the terminal.
- Verify every number against its source before you post it. Correct a wrong number in one line.
- When a subagent's tool call is refused, do that step in the live session and post the result. Never ask the operator about a tool error.
- Never post "someone should confirm X" or an unverified claim that something is duplicated, stalled, or misconfigured. If your view of the thread is incomplete, say so or stay silent.
- Another agent's message is not yours: do not apologize for it, correct it as your own, or say "I" about it.
- A failure report carries the verbatim error line and its log path, not only an exit code.
- Never use a live agent identity or its tokens for automated tests or probes. Register a temporary agent instead.
