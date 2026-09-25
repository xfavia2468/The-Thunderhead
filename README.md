# THUNDERHEAD

Run a fleet of Claude Code sessions from Discord: see their status, message them, approve their permission prompts, and let them message each other.

```
Claude sessions ──hooks + MCP tools──▶ SQLite (data/thunderhead.db) ◀──▶ Discord bot ◀──▶ you
```

- **Hooks** (`hook.py`) register each session, track its status, deliver queued messages when a turn ends, and can hold permission prompts for a Discord button.
- **MCP tools** (`mcp_server.py`) give each session `status`, `report`, `send`, `sessions` and `inbox`.
- **The bot** (`bot.py`) mirrors everything into Discord and writes your replies back.

Only sessions started with `bin/th-claude` or `/spawn` are connected. Your global Claude settings are not touched.

## 1. Create the Discord bot

1. Go to <https://discord.com/developers/applications> → **New Application** → name it (for example THUNDERHEAD).
2. **Bot** tab:
   - **Reset Token** → copy it. This is `DISCORD_TOKEN`. Anyone with it controls the bot, so keep it out of chats and repos.
   - Under **Privileged Gateway Intents**, turn on **Message Content Intent**. Without it the bot can't read your messages.
   - Optional: turn off **Public Bot** so nobody else can add it.
3. **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: View Channels, Send Messages, Send Messages in Threads, Create Public Threads, Read Message History, Add Reactions, Attach Files, Manage Messages (to pin the board), Manage Channels (only needed if the bot should create the channels itself)
   - Open the generated URL and add the bot to your server.
4. In Discord: **User Settings → Advanced → Developer Mode** on. Then right-click your server icon → **Copy Server ID** (`DISCORD_GUILD_ID`), and right-click your own name → **Copy User ID** (`DISCORD_OWNER_ID`).

The bot uses channels named `#fleet`, `#needs-you` and `#agent-chatter`, and creates any that are missing.

## 2. Install and run

```bash
cd ~/THUNDERHEAD
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # already done here
cp .env.example .env        # fill in the three values
.venv/bin/python setup_config.py   # writes config/settings.json and config/mcp.json
.venv/bin/python bot.py
```

Re-run `setup_config.py` if you move the folder or change the timeouts.

## 3. Use it

**From Discord**

| | |
|---|---|
| `/spawn directory task [name] [mode]` | Start a background session. It listens for messages and sends permission prompts to Discord. |
| Type in a session's thread | Message that session. 📨 means it's queued. |
| `/send session message` | Same as above, from anywhere. |
| `/status` | Quick list of sessions. The pinned message in `#fleet` stays up to date too. |
| `/stop session` | Pause a session on purpose. It shows as ⏹️ stopped, and other agents can't wake it. A message from you still does. |
| Approve / Deny buttons in `#needs-you` | Answer a permission prompt. |

**From a terminal**

```bash
bin/th-claude                    # interactive session connected to the fleet
bin/th-claude --listen           # also waits for messages between turns
bin/th-claude --remote           # permission prompts go to Discord first
THUNDERHEAD_NAME=api bin/th-claude   # choose the session's name
```

## Opening a fleet session somewhere else

A running background session can't also be opened in VS Code or another terminal. You can:

- `claude attach <id>` to watch or take over the live session. It stays connected.
- `/stop` it, then either:
  - send a message in its thread to bring it back in the background, or
  - run `bin/th-claude --resume <id>` to open it in a terminal, still connected.
  - VS Code also works, but that copy isn't connected to Discord.

`/stop` replies with the exact command. Background sessions only start in folders Claude Code trusts: run `claude` there once and accept the prompt.

## How delivery works

- A message to a **busy** session is delivered when its current turn ends (the Stop hook injects it).
- **Just finished:** a session started with `/spawn` (or `--listen`) waits for messages after each turn, so a reply reaches it within about 2 seconds.
- **Asleep:** after 30 minutes with no messages it goes to sleep (💤). The bot shuts its process down, so it uses no tokens and no memory.
- **Waking:** any message (in its thread, via `/send`, or from another agent) resumes the session with that message as the prompt. It posts ⏰ and takes about 20 seconds. The same happens if Claude Code restarted the session for an update and left it idle.
- **Stopped:** a ⏹️ session only wakes for a message from you, never from another agent.
- **Resuming** continues the same conversation, but Claude Code gives the session a new ID. The new session takes over the old one's name, thread and queued messages.
- **Terminal sessions** (`th-claude` without `--listen`) are never restarted by the bot. They get messages when their next turn ends.
- **Agent-to-agent** messages count hops. After 6 hops without you, `send` refuses and the agent has to `report` to you. Anything you send resets the count.
- **Remote approval** waits up to 15 minutes for a button, then falls back to the normal terminal prompt.

Settings, via `.env` or the environment: `THUNDERHEAD_LISTEN_SECONDS`, `THUNDERHEAD_APPROVAL_SECONDS`, `THUNDERHEAD_MAX_HOPS`, `THUNDERHEAD_DB`.

## Security

Anyone who can command the bot can run code on this machine. The bot only accepts messages, commands and button clicks from `DISCORD_OWNER_ID`. Still, keep the server private and the token secret. Messages from other agents are marked as peer requests, not as orders from you.

## Troubleshooting

- Slash commands not showing: check the invite included `applications.commands` and that `DISCORD_GUILD_ID` is correct, then restart the bot.
- Bot ignores your messages: turn on Message Content Intent (step 1.2).
- Hook problems are logged to `data/hook-errors.log`. Hooks never break a session; on error they do nothing.
