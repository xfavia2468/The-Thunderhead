# THUNDERHEAD

Discord control plane for a fleet of Claude Code sessions. See README.md for what it does and how to use it.

## Layout

- `thunderhead/db.py`: SQLite schema and helpers. `data/thunderhead.db` is the only channel between processes.
- `thunderhead/hooks.py`: Claude Code hook handlers (register, status, message delivery on Stop, remote approval, sleep).
- `thunderhead/mcp_server.py`: tools each session gets (`status`, `report`, `send`, ...).
- `thunderhead/bot.py`: Discord bot. Posts the outbox, writes your messages into `messages`, wakes and reaps sessions.
- `hook.py`, `mcp_server.py`, `bot.py`: thin entry points. `setup_config.py` generates `config/*.json`.
- `thunderhead/org.py`: the org chart (teams, channels, supervisors' requests), shared by the tools and the bot. `thunderhead/launch.py` builds every `claude` command line. `thunderhead/look.py` is how everything looks in Discord: colors and embed builders.
- `briefs/`: standing briefs for The Thunderhead (`thunderhead.md`) and team supervisors (`supervisor.md`), plus The Thunderhead's system personality (`thunderhead-personality.md`, written by the user; don't change its content without being asked). The launcher copies a brief into the session's working folder as its CLAUDE.md at every start.
- Memory lives outside this repo in `~/thunderhead-memory/` (`THUNDERHEAD_MEMORY`), a separate git repo the bot snapshots every 10 minutes: `hq/NOTES.md` is The Thunderhead's notebook, `teams/<team>/` each team's charter and notes. Don't commit memory here.

## Working on this repo

- Run things with `.venv/bin/python`.
- Test hooks and tools against a scratch database with `THUNDERHEAD_DB=<scratch path>` so nothing reaches Discord.
- The bot runs as a systemd user service (`deploy/thunderhead-bot.service`). After changing `bot.py` or anything it imports, restart it: `systemctl --user restart thunderhead-bot`. Its log is `data/bot.log`. Don't start a second copy by hand.
- Hooks pick up changes on their next run. MCP tools reload `config`, `db`, `launch`, `hooks` and `org` whenever those change on disk, so running sessions get new logic on their next tool call. Changes to `mcp_server.py` itself (a new tool, changed parameters or docstrings) only reach a session when it restarts, which happens the next time it's woken, `/stop`ped and messaged, or wiped.
- Sessions never read Discord. Everything they send and receive goes through `data/thunderhead.db`; the bot renders it as embeds and turns what the human types (plus attachments, saved under `data/attachments/`) back into plain text.
- After changing hook events or timeouts, rerun `.venv/bin/python setup_config.py`.
- `claude --bg` flag order matters: `--resume <id>` straight after `--bg`, and `--mcp-config=<path>` with `=`. See `bg_command`.
- Never print, log or commit `.env` or the bot token.

## Commits

Commit early and often, without asking for permission first. Make a commit whenever a change works on its own: a fix, a feature step, a docs update. Don't wait until the end of a task.

- Keep each commit to one logical change, with a message that says what changed and why.
- Check `git status` before committing, so secrets (`.env`, `bot-token.md`) and runtime files (`data/`) stay out. `.gitignore` covers them. Keep it that way.
- Don't commit broken code on `main`. If something is half-done, finish it or leave it uncommitted.
- Pushing, rewriting history and deleting branches still need the user's go-ahead.
