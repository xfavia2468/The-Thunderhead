"""Discord bot: posts fleet activity to Discord and carries your messages back into sessions."""
import asyncio
import io
import json
import logging
import os
import re
import secrets
import shlex
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

from . import db as store
from .config import MEMORY_ROOT, MEMORY_SNAPSHOT_SECONDS, ROOT, SETTINGS_FILE
from .hooks import _hops_after, delivery
from .launch import NAME_RE, bg_command, lead_command

log = logging.getLogger("thunderhead")

TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0") or 0)
OWNER_ID = int(os.environ.get("DISCORD_OWNER_ID", "0") or 0)

FLEET, NEEDS_YOU, CHATTER, LEAD_CHANNEL = "fleet", "needs-you", "agent-chatter", store.LEAD
GROUPS = "groups"  # Discord category that holds the group channels
ICONS = {"starting": "⏳", "working": "🟢", "listening": "🔵", "idle": "⚪",
         "needs_you": "🔴", "waking": "⏰", "sleeping": "💤", "stopped": "⏹️", "ended": "⚫", "gone": "⚫"}
MSG_LIMIT = 1900


def chunks(text: str, limit: int = MSG_LIMIT):
    """Split text into Discord-sized pieces, preferring line breaks."""
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    if text:
        yield text


async def send_long(channel, text: str, prefix: str = ""):
    text = prefix + text
    if len(text) > 4 * MSG_LIMIT:
        # Too long to read in chat: preview plus the full text as a file.
        await channel.send(text[:1500] + "\n… *(full text attached)*",
                           file=discord.File(io.BytesIO(text.encode()), filename="message.md"))
        return
    for part in chunks(text):
        await channel.send(part)


def is_owner(user) -> bool:
    return user.id == OWNER_ID


# --- approval buttons -------------------------------------------------------

class ApprovalButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"th:(?P<action>allow|deny):(?P<id>[0-9]+)"):
    """Approve/Deny button. Dynamic so it keeps working after the bot restarts."""

    def __init__(self, action: str, approval_id: int):
        super().__init__(discord.ui.Button(
            label="Approve" if action == "allow" else "Deny",
            style=discord.ButtonStyle.success if action == "allow" else discord.ButtonStyle.danger,
            custom_id=f"th:{action}:{approval_id}"))
        self.action, self.approval_id = action, approval_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def interaction_check(self, interaction) -> bool:
        if not is_owner(interaction.user):
            await interaction.response.send_message("Only the fleet owner can answer.", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        with store.db() as conn:
            ok = store.decide_approval(conn, self.approval_id, self.action)
            if ok:
                conn.execute("UPDATE approvals SET closed=1 WHERE id=?", (self.approval_id,))
        if not ok:
            await interaction.response.send_message("Already answered or expired.", ephemeral=True)
            return
        outcome = "✅ Approved" if self.action == "allow" else "⛔ Denied"
        await interaction.response.edit_message(
            content=f"{interaction.message.content}\n**{outcome}**", view=None)


def approval_view(approval_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ApprovalButton("allow", approval_id))
    view.add_item(ApprovalButton("deny", approval_id))
    return view


# --- the bot ----------------------------------------------------------------

class Thunderhead(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.channels: dict[str, discord.TextChannel] = {}
        self.last_board = None
        self.waking: set[str] = set()
        self.wake_failed: dict[str, float] = {}  # session id -> time of last failed wake

    async def setup_hook(self):
        self.add_dynamic_items(ApprovalButton)
        guild = discord.Object(GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self):
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            log.error("Bot is not in guild %s. Check DISCORD_GUILD_ID and the invite.", GUILD_ID)
            return
        for name in (FLEET, NEEDS_YOU, CHATTER, LEAD_CHANNEL):
            ch = discord.utils.get(guild.text_channels, name=name)
            if ch is None:
                topic = "Talk to The ThunderHead, the lead session. /wipe gives it a fresh start." \
                    if name == LEAD_CHANNEL else None
                ch = await guild.create_text_channel(name, topic=topic)
                log.info("Created #%s", name)
            self.channels[name] = ch
        self.guild = guild
        log.info("Logged in as %s; watching %s", self.user, guild.name)
        for loop in (self.pump, self.board, self.liveness, self.snapshot_memory):
            if not loop.is_running():
                loop.start()

    # -- session threads --

    async def thread_for(self, conn, sess) -> discord.abc.Messageable:
        if sess["role"] == "lead":
            ch = self.channels[LEAD_CHANNEL]
            if sess["thread_id"] != ch.id:
                conn.execute("UPDATE sessions SET thread_id=? WHERE name=?", (ch.id, sess["name"]))
            return ch
        if sess["thread_id"]:
            ch = self.get_channel(sess["thread_id"])
            if ch is None:
                try:
                    ch = await self.fetch_channel(sess["thread_id"])
                except discord.NotFound:
                    ch = None
            if ch is not None:
                return ch
        starter = await self.channels[FLEET].send(f"🆕 **{sess['name']}** · `{sess['cwd']}`")
        thread = await starter.create_thread(name=sess["name"][:100], auto_archive_duration=10080)
        conn.execute("UPDATE sessions SET thread_id=? WHERE name=?", (thread.id, sess["name"]))
        return thread

    # -- loops --

    @tasks.loop(seconds=2)
    async def pump(self):
        """Post pending outbox events and approval requests."""
        with store.db() as conn:
            events = conn.execute("SELECT * FROM outbox WHERE posted_at IS NULL ORDER BY id LIMIT 20").fetchall()
            for ev in events:
                try:
                    await self.post_event(conn, ev)
                except Exception:
                    log.exception("Failed to post outbox event %s", ev["id"])
                conn.execute("UPDATE outbox SET posted_at=? WHERE id=?", (store.now(), ev["id"]))

            for ap in conn.execute("SELECT * FROM approvals WHERE message_id IS NULL AND status='pending'").fetchall():
                sess = store.get_session(conn, ap["session_id"])
                thread = await self.thread_for(conn, sess)
                msg = await self.channels[NEEDS_YOU].send(
                    f"<@{OWNER_ID}> 🔐 **{sess['name']}** wants to use **{ap['tool_name']}** ({thread.mention})\n"
                    f"```json\n{ap['tool_input'][:1500]}\n```",
                    view=approval_view(ap["id"]))
                conn.execute("UPDATE approvals SET message_id=? WHERE id=?", (msg.id, ap["id"]))

            # Approvals that timed out: take the buttons away.
            for ap in conn.execute("SELECT * FROM approvals WHERE status='expired' AND closed=0 "
                                   "AND message_id IS NOT NULL").fetchall():
                try:
                    msg = await self.channels[NEEDS_YOU].fetch_message(ap["message_id"])
                    await msg.edit(content=msg.content + "\n⌛ **Expired**: answer it in the terminal.", view=None)
                except discord.HTTPException:
                    pass
                conn.execute("UPDATE approvals SET closed=1 WHERE id=?", (ap["id"],))

    async def group_channel(self, conn, name) -> discord.TextChannel:
        """The Discord channel for a group channel, created on first use."""
        ch_row = store.get_channel(conn, name)
        if ch_row["discord_id"]:
            ch = self.get_channel(ch_row["discord_id"])
            if ch is not None:
                return ch
        category = discord.utils.get(self.guild.categories, name=GROUPS) \
            or await self.guild.create_category(GROUPS)
        ch = discord.utils.get(category.text_channels, name=name) \
            or await self.guild.create_text_channel(name, category=category, topic=ch_row["topic"] or None)
        conn.execute("UPDATE channels SET discord_id=? WHERE name=?", (ch.id, name))
        return ch

    async def post_channel_event(self, conn, ev, sess):
        ch = await self.group_channel(conn, ev["channel"])
        kind, body = ev["kind"], ev["body"]
        who = sess["name"] if sess else "?"
        if kind == "channel_created":
            row = store.get_channel(conn, ev["channel"])
            await ch.send(f"📣 **#{ev['channel']}** was created by The ThunderHead."
                          + (f"\nTopic: {row['topic']}" if row["topic"] else "") + f"\n{body}\n"
                          "Everything posted here, by a member or by you, goes to every member.")
        elif kind == "channel_post":
            await send_long(ch, body, prefix=f"**{who}**: ")
        elif kind == "channel_note":
            await ch.send(body)
        elif kind == "channel_closed":
            await ch.send("🔒 This channel was closed by The ThunderHead. Sessions no longer receive posts here.")
            await ch.set_permissions(self.guild.default_role, send_messages=False)

    async def post_event(self, conn, ev):
        sess = store.get_session(conn, ev["session_id"])
        if ev["channel"]:
            await self.post_channel_event(conn, ev, sess)
            return
        if sess is None:
            return
        thread = await self.thread_for(conn, sess)
        kind, body = ev["kind"], ev["body"]
        if kind == "report":
            await send_long(thread, body)
        elif kind == "status":
            await thread.send(f"📌 {body}")
        elif kind == "session_start":
            await thread.send(f"🟢 Session started in {body}")
        elif kind == "session_end":
            await thread.send(f"⚫ Session ended: {body}")
        elif kind == "woken":
            await thread.send(f"⏰ {body}")
        elif kind == "stopped":
            await thread.send(f"⏹️ Stopped from Discord. {body}")
        elif kind == "needs_you":
            await thread.send(f"🔔 {body}")
            await self.channels[NEEDS_YOU].send(f"<@{OWNER_ID}> 🔔 **{sess['name']}**: {body[:1500]} ({thread.mention})")
        elif kind == "agent_msg":
            await send_long(thread, body)
            await send_long(self.channels[CHATTER], body, prefix=f"**{sess['name']}** ")
        elif kind == "agent_msg_in":
            # Already in #agent-chatter from the sender's side; only the recipient's thread needs it.
            await send_long(thread, body)

    @tasks.loop(seconds=10)
    async def board(self):
        """Keep one pinned message in #fleet showing every session."""
        with store.db() as conn:
            # Ended sessions stay listed for an hour, stopped ones (resumable) for a day.
            rows = conn.execute(
                f"SELECT * FROM sessions WHERE status NOT IN {store.DEAD} OR updated_at > ? "
                "OR (status='stopped' AND updated_at > ?) "
                f"ORDER BY status IN {store.DEAD}, created_at",
                (time.time() - 3600, time.time() - 86400)).fetchall()
            lines = []
            for s in rows:
                where = f" · <#{s['thread_id']}>" if s["thread_id"] else ""
                summary = f" — {s['summary']}" if s["summary"] else ""
                lines.append(f"{ICONS.get(s['status'], '❔')} **{s['name']}** `{s['status']}`{summary}{where}")
            text = "**⚡ THUNDERHEAD fleet**\n" + ("\n".join(lines) or "*No sessions yet.*")
            if text == self.last_board:
                return
            text = text[:1950]
            msg_id = store.kv_get(conn, "board_message_id")
            fleet = self.channels[FLEET]
            msg = None
            if msg_id is not None:
                try:
                    msg = await fleet.fetch_message(int(msg_id))
                    await msg.edit(content=text)
                except discord.NotFound:
                    msg = None
            if msg is None:
                msg = await fleet.send(text)
                store.kv_set(conn, "board_message_id", msg.id)
                try:
                    await msg.pin()
                except discord.HTTPException:
                    pass
            self.last_board = text

    @tasks.loop(seconds=5)
    async def liveness(self):
        """Reconcile with `claude agents`: shut down sleepers, wake sessions with mail, mark dead ones gone."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "agents", "--json", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 30)
            active = {a.get("sessionId"): a for a in json.loads(out or b"[]")}
        except Exception:
            return
        pids = {a.get("pid") for a in active.values()}
        to_wake, to_reap = [], []
        with store.db() as conn:
            # session id -> (oldest undelivered message time, whether any is from the human)
            mail = {r[0]: (r[1], bool(r[2])) for r in conn.execute(
                "SELECT to_session, MIN(created_at), MAX(from_kind IN ('human', 'lead')) FROM messages "
                "WHERE delivered_at IS NULL GROUP BY to_session")}

            for s in store.live_sessions(conn):
                if s["id"] in self.waking:
                    continue
                if s["status"] == "waking":
                    # The resumed session's SessionStart normally clears this; don't let it stick.
                    if time.time() - s["updated_at"] < 60:
                        continue
                    store.set_status(conn, s["id"], "idle")
                agent = active.get(s["id"])
                if s["status"] == store.SLEEPING:
                    if s["id"] in mail:
                        to_wake.append(s)
                    elif agent and agent.get("kind") == "background" and agent.get("status") == "idle":
                        to_reap.append(s)
                    continue
                if agent is None:
                    if s["pid"] not in pids and time.time() - s["updated_at"] >= 120:
                        store.set_status(conn, s["id"], "gone")
                        store.post(conn, s["id"], "session_end", "process is no longer running")
                    continue
                if agent.get("status") != "idle":
                    continue
                if s["status"] in ("starting", "working"):
                    store.set_status(conn, s["id"], "idle")
                # Idle with no Stop hook waiting (it was restarted, interrupted or resumed without a
                # turn), so queued messages would never arrive. A listening hook takes them within
                # seconds, so mail older than 15s means nobody is listening.
                if (s["listen"] and agent.get("kind") == "background" and s["id"] in mail
                        and time.time() - mail[s["id"]][0] > 15):
                    to_wake.append(s)

            # Stopped or ended sessions come back when the human or The ThunderHead writes to them.
            for sid, (_, authoritative) in mail.items():
                s = store.get_session(conn, sid)
                if (s and s["status"] in store.DEAD and s["status"] != "wiped" and authoritative
                        and sid not in self.waking
                        and store.session_by_name(conn, s["name"])["id"] == sid):
                    to_wake.append(s)

        for s in to_reap:
            await run_claude(["claude", "stop", short_id(s["id"])])
        for s in to_wake:
            asyncio.create_task(self.wake(s))

    @tasks.loop(seconds=MEMORY_SNAPSHOT_SECONDS)
    async def snapshot_memory(self):
        """Commit whatever the lead sessions changed in the memory repo, so notes have history."""
        if not (MEMORY_ROOT / ".git").exists():
            return
        git = ["git", "-C", str(MEMORY_ROOT)]
        await run_claude(git + ["add", "-A"])
        code, _ = await run_claude(git + ["diff", "--cached", "--quiet"])
        if code != 0:  # something is staged
            code, text = await run_claude(git + ["commit", "-q", "-m", "Memory snapshot"])
            if code != 0:
                log.warning("Memory snapshot failed: %s", text)

    async def wake(self, sess):
        """Resume a session in the background with its queued messages as the prompt.

        Also used for a running session that went idle without listening: it is stopped first.
        """
        sid = sess["id"]
        if sid in self.waking or time.time() - self.wake_failed.get(sid, 0) < 300:
            return
        self.waking.add(sid)
        try:
            # Never restart a session that's open in someone's terminal; it gets the
            # message at its next turn there.
            if (await agent_info(sid) or {}).get("kind") == "interactive":
                return
            with store.db() as conn:
                rows = store.take_messages(conn, sid)
                if not rows:
                    return
                text_for = delivery(conn, sid, rows)
                store.set_status(conn, sid, "waking")
            if not Path(sess["cwd"] or "").is_dir():
                code, text = 1, f"its folder `{sess['cwd']}` no longer exists."
            else:
                # Fails harmlessly when the process has already exited.
                await run_claude(["claude", "stop", short_id(sid)])
                code, text = await run_claude(
                    bg_command(sess["name"], resume=sid, role=sess["role"]) + [text_for], cwd=sess["cwd"])
            with store.db() as conn:
                if code == 0:
                    self.wake_failed.pop(sid, None)
                    conn.execute("UPDATE sessions SET current_hops=? WHERE id=?", (_hops_after(rows), sid))
                    was = {"sleeping": "asleep", "stopped": "stopped"}.get(sess["status"], "idle")
                    store.post(conn, sid, "woken", f"Waking the session (it was {was}) to deliver "
                                                   f"{len(rows)} message(s).")
                else:
                    # Put the messages back so they aren't lost, and don't retry for a while.
                    self.wake_failed[sid] = time.time()
                    conn.executemany("UPDATE messages SET delivered_at=NULL WHERE id=?", [(r["id"],) for r in rows])
                    store.set_status(conn, sid, sess["status"])
                    store.post(conn, sid, "needs_you", f"Couldn't wake this session to deliver your message:\n{text}")
        finally:
            self.waking.discard(sid)

    # -- your messages --

    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        channel = message.channel
        with store.db() as conn:
            group = store.channel_by_discord(conn, channel.id)
            if channel.id == self.channels[LEAD_CHANNEL].id:
                sess = store.session_by_name(conn, store.LEAD)
                if sess is not None and sess["status"] == "wiped":
                    sess = None
            elif isinstance(channel, discord.Thread):
                sess = store.session_by_thread(conn, channel.id)
            else:
                sess = None
            if sess is None and group is None and channel.id != self.channels[LEAD_CHANNEL].id:
                return
            if not is_owner(message.author):
                await message.add_reaction("⛔")
                return
            if group is not None:
                # @name pings those sessions; no mentions pings everyone, since you're the one asking.
                mentioned = [m for m in re.findall(r"@([A-Za-z0-9_-]+)", message.content)
                             if m in store.members(conn, group["name"])]
                got = store.fan_out(conn, group["name"], "human", message.author.display_name,
                                    message.content, notify=mentioned or ["all"])
                targets = [store.session_by_name(conn, n) for n in got]
            elif sess is None:
                targets = None  # no ThunderHead yet
            else:
                store.queue_message(conn, sess["id"], "human", message.author.display_name, message.content)
                targets = [sess]
        if targets is None:
            await self.start_lead(first_message=message.content)
            await message.add_reaction("⚡")
            return
        await message.add_reaction("📨")
        for t in targets:
            await self.deliver_now(t, message if group is None else None)

    async def start_lead(self, first_message: str | None = None) -> tuple[int, str]:
        """Start a fresh ThunderHead: no earlier conversation, memory from hq/NOTES.md."""
        cmd, cwd = lead_command(first_message)
        code, text = await run_claude(cmd, cwd=cwd)
        await self.channels[LEAD_CHANNEL].send(
            "⚡ Starting a fresh ThunderHead…" if code == 0 else f"Couldn't start The ThunderHead:\n```\n{text}\n```")
        return code, text

    async def deliver_now(self, sess, message=None):
        """Wake a sleeping or stopped session right away instead of waiting for the next check."""
        if sess["status"] in store.DEAD or sess["status"] == store.SLEEPING:
            asyncio.create_task(self.wake(sess))
        elif not sess["listen"] and sess["status"] not in ("working", "needs_you") and message:
            await message.reply("Queued. This session runs in a terminal and isn't listening, so it gets "
                                "the message when its next turn ends.", mention_author=False)


bot = Thunderhead()


# --- running claude ---------------------------------------------------------

async def run_claude(cmd: list[str], cwd=None, timeout=60, tail: int | None = 1500) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, f"`{' '.join(cmd[:2])}` didn't return within {timeout}s."
    text = out.decode(errors="replace").strip()
    text = text[-tail:] if tail else text
    if proc.returncode != 0 and "not trusted" in text and "trust prompt" not in text:
        text += ("\n\nClaude Code only starts background sessions in trusted folders. Open a terminal there, "
                 "run `claude` once and accept the trust prompt, then try again.")
    return proc.returncode, text


async def agent_info(session_id: str) -> dict | None:
    """This session's entry in `claude agents --json`, if it's running."""
    code, text = await run_claude(["claude", "agents", "--json"], timeout=30, tail=None)
    try:
        return next((a for a in json.loads(text[text.find("["):]) if a.get("sessionId") == session_id), None)
    except (ValueError, TypeError):
        return None


def short_id(session_id: str) -> str:
    """`claude stop`/`attach` take the short job id: the session id's first 8 characters."""
    return session_id[:8]


def resume_hint(sess) -> str:
    cmd = f"cd {shlex.quote(sess['cwd'])} && {shlex.quote(str(ROOT / 'bin' / 'th-claude'))} --resume {sess['id']}"
    return (f"Send a message in its thread to start it again. To take it over in a terminal instead:\n`{cmd}`\n"
            "Opening it in VS Code works too, but that copy won't be connected to Discord.")


# --- slash commands ---------------------------------------------------------

async def session_names(interaction, current: str):
    with store.db() as conn:
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM sessions GROUP BY name ORDER BY MAX(updated_at) DESC")]
    return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]


async def owner_only(interaction) -> bool:
    if not is_owner(interaction.user):
        await interaction.response.send_message("Only the fleet owner can do that.", ephemeral=True)
        return False
    return True


@bot.tree.command(description="Show every session and what it's doing")
async def status(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    with store.db() as conn:
        rows = store.live_sessions(conn)
    lines = [f"{ICONS.get(s['status'], '❔')} **{s['name']}** `{s['status']}` {s['summary'] or ''}" for s in rows]
    await interaction.response.send_message("\n".join(lines)[:1990] or "No live sessions.", ephemeral=True)


@bot.tree.command(description="Send a message to a session")
@app_commands.autocomplete(session=session_names)
async def send(interaction: discord.Interaction, session: str, message: str):
    if not await owner_only(interaction):
        return
    with store.db() as conn:
        sess = store.session_by_name(conn, session)
        if sess is None:
            await interaction.response.send_message(f"No session named `{session}`.", ephemeral=True)
            return
        store.queue_message(conn, sess["id"], "human", interaction.user.display_name, message)
    await interaction.response.send_message(f"📨 Queued for **{session}**.", ephemeral=True)
    await bot.deliver_now(sess)


@bot.tree.command(description="Start a new background Claude session")
@app_commands.describe(directory="Working directory (absolute or ~/...)", task="What the session should do",
                       name="Session name (letters, digits, - and _)", mode="Permission mode")
@app_commands.choices(mode=[app_commands.Choice(name=m, value=m)
                            for m in ("default", "acceptEdits", "auto", "plan")])
async def spawn(interaction: discord.Interaction, directory: str, task: str,
                name: str | None = None, mode: app_commands.Choice[str] | None = None):
    if not await owner_only(interaction):
        return
    cwd = Path(directory).expanduser()
    if not cwd.is_dir():
        await interaction.response.send_message(f"`{cwd}` is not a directory.", ephemeral=True)
        return
    name = name or f"{cwd.name or 'root'}-{secrets.token_hex(2)}"
    if not NAME_RE.match(name) or name == store.LEAD:
        await interaction.response.send_message("Names can only use letters, digits, - and _ "
                                                "('thunderhead' is taken by The ThunderHead).", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)

    cmd = bg_command(name)
    if mode and mode.value != "default":
        cmd += ["--permission-mode", mode.value]
    code, text = await run_claude(cmd + [task], cwd=cwd)
    if code != 0:
        await interaction.followup.send(f"Spawn failed:\n```\n{text}\n```", ephemeral=True)
        return
    await interaction.followup.send(f"🚀 Spawned **{name}** in `{cwd}`. Its thread appears in #{FLEET} "
                                    f"once it starts.\n```\n{text}\n```", ephemeral=True)


@bot.tree.command(description="Stop a background session (its conversation is kept)")
@app_commands.autocomplete(session=session_names)
async def stop(interaction: discord.Interaction, session: str):
    if not await owner_only(interaction):
        return
    with store.db() as conn:
        sess = store.session_by_name(conn, session)
    if sess is None or sess["status"] in store.DEAD:
        await interaction.response.send_message(f"No running session named `{session}`.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    # Mark it first so the SessionEnd hook that the stop triggers leaves it as 'stopped'.
    with store.db() as conn:
        store.set_status(conn, sess["id"], "stopped")
    code, text = await run_claude(["claude", "stop", short_id(sess["id"])])
    if code != 0 and "No job matching" in text and sess["status"] == store.SLEEPING:
        code = 0  # asleep: the process was already shut down
    with store.db() as conn:
        if code != 0:
            store.set_status(conn, sess["id"], sess["status"])
        else:
            store.post(conn, sess["id"], "stopped", "Other agents can't wake it now. Send a message here to start it again.")
    if code != 0:
        await interaction.followup.send(f"Couldn't stop **{session}** (only background sessions can be "
                                        f"stopped from here):\n```\n{text}\n```", ephemeral=True)
        return
    await interaction.followup.send(f"⏹️ Stopped **{session}**. Its conversation is kept.\n{resume_hint(sess)}",
                                    ephemeral=True)


@bot.tree.command(description="Give The ThunderHead a fresh start (its memory comes from hq/NOTES.md)")
async def wipe(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    with store.db() as conn:
        lead = store.session_by_name(conn, store.LEAD)
        if lead is not None and lead["status"] not in store.DEAD:
            # 'stopped' keeps SessionEnd quiet; then retire it so nothing resumes the old conversation.
            store.set_status(conn, lead["id"], "stopped")
    if lead is not None:
        await run_claude(["claude", "stop", short_id(lead["id"])])
        with store.db() as conn:
            store.set_status(conn, lead["id"], "wiped")
    await bot.channels[LEAD_CHANNEL].send("🧹 **Wiped.** The ThunderHead's conversation was cleared.")
    code, text = await bot.start_lead()
    await interaction.followup.send("Fresh ThunderHead starting." if code == 0 else f"Failed:\n```\n{text}\n```",
                                    ephemeral=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    missing = [k for k, v in (("DISCORD_TOKEN", TOKEN), ("DISCORD_GUILD_ID", GUILD_ID),
                              ("DISCORD_OWNER_ID", OWNER_ID)) if not v]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)} (see README.md)")
    if not SETTINGS_FILE.exists():
        raise SystemExit("Run `.venv/bin/python setup_config.py` first.")
    bot.run(TOKEN, log_handler=None)
