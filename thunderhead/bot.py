"""Discord bot: posts fleet activity to Discord and carries your messages back into sessions."""
import asyncio
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
from . import launch, look, org
from . import config
from .config import MEMORY_ROOT, MEMORY_SNAPSHOT_SECONDS, ROOT, SETTINGS_FILE
from .hooks import _hops_after, delivery
from .launch import NAME_RE, bg_command, lead_command, relaunch_command

log = logging.getLogger("thunderhead")

TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0") or 0)
OWNER_ID = int(os.environ.get("DISCORD_OWNER_ID", "0") or 0)

FLEET, NEEDS_YOU, CHATTER, LEAD_CHANNEL, ARCHIVED = "fleet", "needs-you", "agent-chatter", store.LEAD, "archived"
GROUPS = "groups"  # Discord category that holds the group channels
ICONS = look.ICONS
# A quiet thread (say, a sleeping session's) drops out of the sidebar after a day. It's archived, not
# deleted: board links still open it, and it comes back as soon as the session posts again.
THREAD_ARCHIVE_MINUTES = 1440
# After a usage limit, wake-ups pause; this often, one is let through to see if it has reset.
USAGE_PROBE_SECONDS = 15 * 60
# Files the human attaches in Discord are saved here, so sessions can read them.
ATTACHMENTS = ROOT / "data" / "attachments"
ATTACHMENT_LIMIT = 25_000_000


def short_count(n: int) -> str:
    n = n or 0
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def name_tokens(conn, name) -> int:
    """Tokens a session has used, across all its incarnations (resumes get new ids)."""
    return conn.execute("SELECT COALESCE(SUM(tokens), 0) FROM sessions WHERE name=?", (name,)).fetchone()[0]


def team_tokens(conn, team) -> int:
    return sum(name_tokens(conn, n) for n in store.team_members_of(conn, team))


def board_pages(lines: list[str]) -> list[str]:
    """Split the board into embed-sized pages at line boundaries."""
    pages, page = [], ""
    for line in lines or ["*No sessions yet.*"]:
        line = line[:look.DESC_LIMIT - 40]
        if page and len(page) + 1 + len(line) > look.DESC_LIMIT:
            pages.append(page)
            page = ""
        page += ("\n" if page else "") + line
    return pages + [page]


def board_embed(page: str, count: int) -> discord.Embed:
    return look.card(page, title="⚡ THUNDERHEAD fleet", color=look.BOARD,
                     footer=f"{count} session{'s' if count != 1 else ''} · archived ones are in #archived · updated")


def is_owner(user) -> bool:
    return user.id == OWNER_ID


# --- buttons and modals -----------------------------------------------------
# Buttons are dynamic items: their state lives in the custom_id, so they keep working after the
# bot restarts. Modals are pop-up forms, used where the human would otherwise have to type a
# command or couldn't say why.

class OwnerOnly:
    async def interaction_check(self, interaction) -> bool:
        if not is_owner(interaction.user):
            await interaction.response.send_message("Only the fleet owner can do that.", ephemeral=True)
            return False
        return True


def _text(label: str, *, paragraph=False, required=True, default=None, placeholder=None, max_length=4000):
    return discord.ui.Label(text=label, component=discord.ui.TextInput(
        style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
        required=required, default=default, placeholder=placeholder, max_length=max_length))


async def finish_approval(interaction: discord.Interaction, approval_id: int, action: str, reason: str = ""):
    """Record the human's answer to a permission prompt. It leaves #needs-you, since it no longer
    needs them, and the outcome goes to the session's thread."""
    with store.db() as conn:
        ok = store.decide_approval(conn, approval_id, action, reason)
        if ok:
            conn.execute("UPDATE approvals SET closed=1 WHERE id=?", (approval_id,))
    if not ok:
        await interaction.response.send_message("Already answered or expired.", ephemeral=True)
        return
    await interaction.response.defer()
    if interaction.message:
        await interaction.message.delete()
    bot_ = interaction.client
    with store.db() as conn:
        ap = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        sess = store.get_session(conn, ap["session_id"])
        thread = await bot_.thread_for(conn, sess)
    approved = action == "allow"
    await thread.send(embed=look.card(
        f"```json\n{ap['tool_input'][:1500]}\n```",
        title=f"{'✅ Approved' if approved else '⛔ Denied'} · {ap['tool_name']}",
        color=look.GOOD if approved else look.BAD,
        fields=[("Your reason", reason)] if reason else ()))


class DenyReasonModal(OwnerOnly, discord.ui.Modal, title="Deny, with a reason"):
    reason = _text("Why? The session sees this, so it can adjust", paragraph=True, max_length=1000,
                   placeholder="e.g. Don't touch the production config; use the staging one.")

    def __init__(self, approval_id: int):
        super().__init__()
        self.approval_id = approval_id

    async def on_submit(self, interaction: discord.Interaction):
        await finish_approval(interaction, self.approval_id, "deny", self.reason.component.value.strip())


class ApprovalButton(OwnerOnly, discord.ui.DynamicItem[discord.ui.Button],
                     template=r"th:(?P<action>allow|deny|why):(?P<id>[0-9]+)"):
    """Approve / Deny / Deny with reason on a permission prompt."""
    LOOK = {"allow": ("Approve", discord.ButtonStyle.success, "✅"),
            "deny": ("Deny", discord.ButtonStyle.danger, "⛔"),
            "why": ("Deny with reason…", discord.ButtonStyle.secondary, "✍️")}

    def __init__(self, action: str, approval_id: int):
        label, style, emoji = self.LOOK[action]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji,
                                           custom_id=f"th:{action}:{approval_id}"))
        self.action, self.approval_id = action, approval_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        if self.action == "why":
            await interaction.response.send_modal(DenyReasonModal(self.approval_id))
        else:
            await finish_approval(interaction, self.approval_id, self.action)


async def finish_request(interaction: discord.Interaction, request_id: int, approve: bool, note: str = ""):
    """The human's decision on an escalated request: carry it out, and show the outcome on its card."""
    await interaction.response.defer()
    with store.db() as conn:
        msg, cmd, cwd = org.decide(conn, request_id, approve, note, by="human")
    if cmd:
        code, text = await run_claude(cmd, cwd=cwd)
        if code != 0:
            msg += f" But the session didn't start: {text[-300:]}"
    decided = approve and "approved" in msg
    embed = interaction.message.embeds[0] if interaction.message.embeds else look.card()
    embed.color = look.GOOD if decided else look.BAD
    embed.add_field(name="✅ Your decision" if decided else "⛔ Your decision",
                    value=(msg + (f"\nYour note: {note}" if note else ""))[:1024], inline=False)
    await interaction.message.edit(embed=embed, view=None)


class RejectModal(OwnerOnly, discord.ui.Modal, title="Reject, with feedback"):
    note = _text("What's wrong, or what should change?", paragraph=True, required=False, max_length=1000)

    def __init__(self, request_id: int, charter: bool):
        super().__init__(title="Request changes to the charter" if charter else "Reject, with feedback")
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction):
        await finish_request(interaction, self.request_id, False, self.note.component.value.strip())


class RequestButton(OwnerOnly, discord.ui.DynamicItem[discord.ui.Button],
                    template=r"th:req:(?P<action>approve|reject):(?P<id>[0-9]+)"):
    """Approve / Reject on a request escalated to the human. Reject asks for feedback."""

    def __init__(self, action: str, request_id: int, charter: bool = False):
        label = ("Approve" if action == "approve" else
                 "Request changes…" if charter else "Reject…")
        super().__init__(discord.ui.Button(
            label=label, emoji="✅" if action == "approve" else "✍️",
            style=discord.ButtonStyle.success if action == "approve" else discord.ButtonStyle.danger,
            custom_id=f"th:req:{action}:{request_id}"))
        self.action, self.request_id = action, request_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        if self.action == "approve":
            await finish_request(interaction, self.request_id, True)
            return
        with store.db() as conn:
            req = org.get_request(conn, self.request_id)
        await interaction.response.send_modal(RejectModal(self.request_id, charter=bool(req and req["action"] == "charter")))


class ReplyModal(OwnerOnly, discord.ui.Modal, title="Reply"):
    message = _text("Message", paragraph=True, placeholder="It's delivered when the session's turn ends, "
                                                          "or wakes it if it's asleep.")

    def __init__(self, session: str):
        super().__init__(title=f"Message {session}"[:45])
        self.session = session

    async def on_submit(self, interaction: discord.Interaction):
        await send_to_session(interaction, self.session, self.message.component.value)


class ReplyButton(OwnerOnly, discord.ui.DynamicItem[discord.ui.Button],
                  template=r"th:reply:(?P<name>[A-Za-z0-9_-]+)"):
    """Reply to a session straight from a notice, in a pop-up form."""

    def __init__(self, name: str):
        super().__init__(discord.ui.Button(label="Reply", emoji="💬", style=discord.ButtonStyle.primary,
                                           custom_id=f"th:reply:{name}"))
        self.name = name

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["name"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ReplyModal(self.name))


class AckButton(OwnerOnly, discord.ui.DynamicItem[discord.ui.Button], template=r"th:ack"):
    """Acknowledge: deletes a notice so the channel stays clean. Threads started from it survive."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="Acknowledge", style=discord.ButtonStyle.secondary,
                                           emoji="✔️", custom_id="th:ack"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.message.delete()


def buttons(*items) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def ack_view() -> discord.ui.View:
    return buttons(AckButton())


def approval_view(approval_id: int) -> discord.ui.View:
    return buttons(*(ApprovalButton(a, approval_id) for a in ("allow", "deny", "why")))


async def send_to_session(interaction: discord.Interaction, name: str, text: str):
    """Queue a message from the human for a session, the same way typing in its thread does."""
    with store.db() as conn:
        sess = store.session_by_name(conn, name)
        if sess is None:
            await interaction.response.send_message(f"No session named `{name}`.", ephemeral=True)
            return
        store.queue_message(conn, sess["id"], "human", interaction.user.display_name, text)
        bot.copy_up(conn, name, text)
    await interaction.response.send_message(embed=look.note(f"📨 Sent to **{name}**.", look.GOOD), ephemeral=True)
    await bot.deliver_now(sess)


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
        self.deleting: set[str] = set()
        self.wake_failed: dict[str, float] = {}  # session id -> time of last failed wake
        self.issues: dict[str, dict] = {}  # health problems by key
        self.resolved: list[int] = []  # alert messages to remove, their problems fixed
        self.beats: dict[str, float] = {}  # loop -> when it last ran
        self.hook_log_size: int | None = None
        self.hook_errors_at = 0.0
        self.last_tasks: dict[str, str] = {}  # team -> task board text last posted

    async def setup_hook(self):
        self.add_dynamic_items(ApprovalButton, RequestButton, ReplyButton, AckButton)
        guild = discord.Object(GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self):
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            log.error("Bot is not in guild %s. Check DISCORD_GUILD_ID and the invite.", GUILD_ID)
            return
        for name in (FLEET, NEEDS_YOU, CHATTER, LEAD_CHANNEL, ARCHIVED):
            ch = discord.utils.get(guild.text_channels, name=name)
            if ch is None:
                topic = {LEAD_CHANNEL: "Talk to The Thunderhead, the lead session. /wipe gives it a fresh start.",
                         ARCHIVED: "Archived session threads. A listing disappears when its session comes back."
                         }.get(name)
                ch = await guild.create_text_channel(name, topic=topic)
                log.info("Created #%s", name)
            self.channels[name] = ch
        self.guild = guild
        log.info("Logged in as %s; watching %s", self.user, guild.name)
        await self.tidy_fleet()
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
        team = store.team_of(conn, sess["name"])
        if team is not None and team["supervisor"] == sess["name"]:
            ch = await self.team_desk(conn, team["name"])
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
                if isinstance(ch, discord.Thread) and ch.archived:
                    await ch.edit(archived=False)
                return ch
        # Devs get their thread in their team's channel; everyone else in #fleet.
        parent = await self.group_channel(conn, team["name"]) if team is not None else self.channels[FLEET]
        starter = await parent.send(embed=look.card(
            f"📂 `{sess['cwd']}`", author=f"🆕 {look.who(conn, sess['name'])}", color=look.role_color(conn, sess["name"]),
            footer="New session · its conversation is in the thread below"), view=ack_view())
        thread = await starter.create_thread(name=sess["name"][:100], auto_archive_duration=THREAD_ARCHIVE_MINUTES)
        conn.execute("UPDATE sessions SET thread_id=? WHERE name=?", (thread.id, sess["name"]))
        return thread

    # -- loops --

    @tasks.loop(seconds=2)
    async def pump(self):
        await self.guard("pump", self._pump)

    async def _pump(self):
        """Post pending outbox events and approval requests."""
        with store.db() as conn:
            events = conn.execute("SELECT * FROM outbox WHERE posted_at IS NULL ORDER BY id LIMIT 20").fetchall()
            for ev in events:
                try:
                    await self.post_event(conn, ev)
                except Exception as e:
                    log.exception("Failed to post outbox event %s", ev["id"])
                    self.problem("posting", f"Couldn't post a {ev['kind']} event to Discord: {e}")
                else:
                    self.fine("posting")
                conn.execute("UPDATE outbox SET posted_at=? WHERE id=?", (store.now(), ev["id"]))

            for ap in conn.execute("SELECT * FROM approvals WHERE message_id IS NULL AND status='pending'").fetchall():
                sess = store.get_session(conn, ap["session_id"])
                thread = await self.thread_for(conn, sess)
                msg = await self.channels[NEEDS_YOU].send(
                    content=f"<@{OWNER_ID}>",
                    embed=look.card(f"```json\n{ap['tool_input'][:1500]}\n```", author=look.who(conn, sess["name"]),
                                    title=f"🔐 Wants to use {ap['tool_name']}", color=look.APPROVAL,
                                    fields=[("Conversation", thread.mention, True),
                                            ("Answer by", f"<t:{int(ap['created_at'] + config.APPROVAL_SECONDS)}:R>", True)],
                                    footer="If nobody answers in time, it waits in the terminal instead"),
                    view=approval_view(ap["id"]))
                conn.execute("UPDATE approvals SET message_id=? WHERE id=?", (msg.id, ap["id"]))

            # Approvals that timed out: take the buttons away.
            for ap in conn.execute("SELECT * FROM approvals WHERE status='expired' AND closed=0 "
                                   "AND message_id IS NOT NULL").fetchall():
                sess = store.get_session(conn, ap["session_id"])
                try:
                    old = await self.channels[NEEDS_YOU].fetch_message(ap["message_id"])
                    await old.delete()
                except discord.HTTPException:
                    pass
                msg = await self.channels[NEEDS_YOU].send(
                    content=f"<@{OWNER_ID}>",
                    embed=look.card(f"Nobody answered in Discord in time, so the prompt is waiting in the terminal.\n"
                                    f"Open it with `claude attach {short_id(sess['id'])}`.",
                                    author=look.who(conn, sess["name"]),
                                    title=f"⌛ {ap['tool_name']} request expired", color=look.NEEDS_YOU),
                    view=buttons(ReplyButton(sess["name"]), AckButton()))
                conn.execute("INSERT INTO needs_you_posts (message_id, session_id, created_at) VALUES (?, ?, ?)",
                             (msg.id, sess["id"], store.now()))
                conn.execute("UPDATE approvals SET closed=1 WHERE id=?", (ap["id"],))

    async def team_category(self, conn, team: str) -> discord.CategoryChannel:
        t = store.get_team(conn, team)
        cat = self.get_channel(t["category_id"]) if t["category_id"] else None
        if cat is None:
            cat = discord.utils.get(self.guild.categories, name=team) or await self.guild.create_category(team)
            conn.execute("UPDATE teams SET category_id=? WHERE name=?", (cat.id, team))
        return cat

    async def team_desk(self, conn, team: str) -> discord.TextChannel:
        """The channel where the human talks to a team's supervisor, like #thunderhead for the lead."""
        t = store.get_team(conn, team)
        desk = self.get_channel(t["desk_id"]) if t["desk_id"] else None
        if desk is None:
            cat = await self.team_category(conn, team)
            name = f"{team}-supervisor"
            desk = discord.utils.get(cat.text_channels, name=name) or await self.guild.create_text_channel(
                name, category=cat, topic=f"Talk to {t['supervisor']}, the supervisor of team {team}.")
            conn.execute("UPDATE teams SET desk_id=? WHERE name=?", (desk.id, team))
        return desk

    async def group_channel(self, conn, name) -> discord.TextChannel:
        """The Discord channel for a group channel, created on first use."""
        ch_row = store.get_channel(conn, name)
        if ch_row["discord_id"]:
            ch = self.get_channel(ch_row["discord_id"])
            if ch is not None:
                return ch
        if ch_row["team"]:
            category = await self.team_category(conn, ch_row["team"])
        else:
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
        if kind == "team_created":
            t = store.get_team(conn, ev["channel"])
            desk = await self.team_desk(conn, t["name"])
            repos = "\n".join(f"`{r}`" for r in json.loads(t["repos"]))
            await ch.send(embed=look.card(
                f"The supervisor and its devs work together here, and devs' threads live here too.\n"
                f"Talk to the supervisor in {desk.mention}.", title=f"👥 Team {t['name']}",
                color=look.SUPERVISOR, fields=[("Supervisor", t["supervisor"], True), ("Repositories", repos, True)]))
            await desk.send(embed=look.card(
                f"The Thunderhead created this team. Its supervisor **{t['supervisor']}** is starting up and will "
                "introduce itself here. Talk to it in this channel.", title=f"👥 Team {t['name']}",
                color=look.SUPERVISOR, fields=[("Repositories", repos, False)]))
        elif kind == "channel_created":
            row = store.get_channel(conn, ev["channel"])
            members = body.split("Members: ", 1)[-1]
            await ch.send(embed=look.card(
                "A post here reaches the members it names. Yours, with no @mentions, reach everyone.",
                title=f"📣 #{ev['channel']}", color=look.AGENTS,
                fields=[("Topic", row["topic"] or "—", False), ("Members", members, False)]))
        elif kind == "channel_post":
            pinged, _, text = body.partition("\n")
            await look.send_card(ch, text or body, author=look.who(conn, who), color=look.role_color(conn, who),
                                 footer=pinged if pinged.startswith("→") else None)
        elif kind == "channel_note":
            await ch.send(embed=look.note(body, look.AGENTS))
        elif kind == "channel_closed":
            await ch.send(embed=look.note("🔒 **Closed.** Sessions no longer get posts here; it stays as the record.",
                                          look.QUIET))
            await ch.set_permissions(self.guild.default_role, send_messages=False)

    async def on_raw_thread_update(self, payload: discord.RawThreadUpdateEvent):
        # Covers every way a thread changes: the bot archiving it, Discord's auto-archive after a
        # quiet day, and it coming back when anything is posted. The raw event, because archived
        # threads aren't cached and the plain one never fires for them.
        archived = payload.data.get("thread_metadata", {}).get("archived")
        if archived is not None:
            await self.sync_archived(payload.thread_id, archived)

    async def sync_archived(self, thread_id: int, archived: bool):
        """Keep #archived listing exactly the archived session threads."""
        with store.db() as conn:
            sess = store.session_by_thread(conn, thread_id)
            if sess is None or sess["thread_id"] != thread_id:
                return  # not a session's thread
            post = conn.execute("SELECT message_id FROM archived_posts WHERE thread_id=?", (thread_id,)).fetchone()
            archived_ch = self.channels[ARCHIVED]
            if archived and post is None:
                msg = await archived_ch.send(embed=self.archived_card(conn, sess, look.now()))
                conn.execute("INSERT INTO archived_posts (thread_id, message_id) VALUES (?, ?)", (thread_id, msg.id))
            elif archived and post is not None:
                # Already listed: refresh it, since the session's state may have changed since.
                try:
                    old = await archived_ch.fetch_message(post["message_id"])
                    stamp = old.embeds[0].timestamp if old.embeds and old.embeds[0].timestamp else look.now()
                    new = self.archived_card(conn, sess, stamp)
                    if old.content or not old.embeds or old.embeds[0].description != new.description:
                        await old.edit(content=None, embed=new)
                except discord.NotFound:
                    conn.execute("DELETE FROM archived_posts WHERE thread_id=?", (thread_id,))
            elif not archived and post is not None:
                try:
                    old = await archived_ch.fetch_message(post["message_id"])
                    await old.delete()
                except discord.NotFound:
                    pass
                conn.execute("DELETE FROM archived_posts WHERE thread_id=?", (thread_id,))

    async def delete_session(self, name: str, keep_thread: bool, note: str = "") -> str:
        """Delete a session for good: its process, Claude jobs, records and (unless kept) its thread."""
        with store.db() as conn:
            err = org.delete_check(conn, name)
            if err:
                return err
            cur = store.session_by_name(conn, name)
            store.set_status(conn, cur["id"], "stopped")  # keeps its SessionEnd hook quiet
            ids = [r[0] for r in conn.execute("SELECT id FROM sessions WHERE name=?", (name,))]
        # Stop it before touching its records, so its hooks don't run against a half-deleted session.
        for sid in ids:
            await run_claude(["claude", "stop", short_id(sid)])
            await run_claude(["claude", "rm", short_id(sid)])
        with store.db() as conn:
            gone = org.forget_session(conn, name, keep_thread)
            posts = [r[0] for r in conn.execute(
                f"SELECT message_id FROM needs_you_posts WHERE session_id IN ({','.join('?' * len(gone['ids']))})",
                gone["ids"])]
            conn.execute(f"DELETE FROM needs_you_posts WHERE session_id IN ({','.join('?' * len(gone['ids']))})",
                         gone["ids"])
            listing = conn.execute("SELECT message_id FROM archived_posts WHERE thread_id=?",
                                   (gone["thread_id"],)).fetchone()
        for mid in posts:
            try:
                await (await self.channels[NEEDS_YOU].fetch_message(mid)).delete()
            except discord.HTTPException:
                pass
        thread = None
        if gone["thread_id"]:
            try:
                thread = self.get_channel(gone["thread_id"]) or await self.fetch_channel(gone["thread_id"])
            except discord.HTTPException:
                thread = None
        if isinstance(thread, discord.Thread):
            if keep_thread:
                await thread.send(embed=look.note(f"🗑️ **{name}** was deleted{note}. This thread is kept as its record."))
                await self.archive(thread)
            else:
                if listing:
                    try:
                        await (await self.channels[ARCHIVED].fetch_message(listing["message_id"])).delete()
                    except discord.HTTPException:
                        pass
                    with store.db() as conn:
                        conn.execute("DELETE FROM archived_posts WHERE thread_id=?", (thread.id,))
                try:
                    # A thread started from a message shares its id; that's the 🆕 notice.
                    await thread.parent.get_partial_message(thread.id).delete()
                except discord.HTTPException:
                    pass
                await thread.delete()
        what = "Its thread is kept and archived." if keep_thread else "Its thread is gone too."
        return f"🗑️ Deleted **{name}**. {what} The name is free again."

    def archived_card(self, conn, sess, archived_at) -> discord.Embed:
        """An #archived listing: what the session is now, in words, with a link to its thread."""
        state = {store.SLEEPING: "💤 **Asleep**: any message wakes it.",
                 "stopped": "⏹️ **Stopped**: only you can wake it.",
                 "ended": "⚫ **Ended**: a message from you brings it back.",
                 "gone": "⚫ **Ended**: a message from you brings it back.",
                 "deleted": "🗑️ **Deleted**: this thread is kept as its record."}.get(sess["status"], sess["status"])
        e = look.card(f"{state}\n{'Last status: ' + sess['summary'] if sess['summary'] else ''}\n<#{sess['thread_id']}>",
                      author=f"🗄️ {look.who(conn, sess['name']).split(' ', 1)[1]}", color=look.QUIET, stamp=False,
                      footer="Archived")
        e.timestamp = archived_at
        return e

    async def archive_session(self, sess, by: str):
        """Put a session to sleep and archive its thread: done for now, but any message wakes it."""
        if sess["status"] not in store.DEAD and sess["status"] != store.SLEEPING:
            # Asleep, not stopped, so anyone can still wake it. SessionEnd stays quiet for sleepers.
            with store.db() as conn:
                store.set_status(conn, sess["id"], store.SLEEPING)
            info = await agent_info(sess["id"])
            if info and info.get("kind") == "background":
                await run_claude(["claude", "stop", short_id(sess["id"])])
        with store.db() as conn:
            thread = await self.thread_for(conn, sess)
        await thread.send(embed=look.note(f"🗄️ **Archived by {by}.** A message here, or from another session, "
                                          "wakes it again."))
        await self.archive(thread)

    async def archive(self, thread):
        """Archive a finished session's thread. Nothing is lost; it unarchives if the session returns."""
        if isinstance(thread, discord.Thread) and not thread.archived:
            try:
                await thread.edit(archived=True)
            except discord.HTTPException:
                log.warning("Couldn't archive thread %s", thread.id)

    async def post_event(self, conn, ev):
        sess = store.get_session(conn, ev["session_id"])
        if ev["channel"]:
            await self.post_channel_event(conn, ev, sess)
            return
        if sess is None:
            return
        thread = await self.thread_for(conn, sess)
        kind, body = ev["kind"], ev["body"]
        if kind == "request_escalated":
            await self.post_request(conn, org.get_request(conn, int(body)))
            return
        name = sess["name"]
        if kind == "usage_limit":
            msg = await self.channels[NEEDS_YOU].send(content=f"<@{OWNER_ID}>", embed=look.card(
                f"**{name}** hit a usage limit:\n```\n{body[:900]}\n```\nWake-ups are paused across the fleet. "
                "Your messages are queued. Every 15 minutes one wake-up is let through to see whether the limit "
                "has reset, and everything resumes as soon as a turn succeeds.",
                title="⏸️ Usage limit reached", color=look.BAD))
            store.kv_set(conn, "usage_notice_id", msg.id)
            return
        if kind == "usage_resumed":
            notice = store.kv_get(conn, "usage_notice_id")
            if notice:
                try:
                    await (await self.channels[NEEDS_YOU].fetch_message(int(notice))).delete()
                except discord.HTTPException:
                    pass
                conn.execute("DELETE FROM kv WHERE key='usage_notice_id'")
            await self.channels[NEEDS_YOU].send(embed=look.note(
                "▶️ **Usage is available again.** Wake-ups have resumed, and queued messages are being delivered.",
                look.GOOD), view=ack_view())
            return
        if kind == "compacted":
            await thread.send(embed=look.note(f"🗜️ **Context compacted** ({body}). Some of its detail may be gone."))
            return
        if kind == "archive":
            if sess["status"] in ("working", "needs_you", "waking"):
                org.fyi(conn, body, f"Couldn't archive {name}: it became busy ({sess['status']}) before I got to it.")
            else:
                await self.archive_session(sess, f"its supervisor, {body}")
            return
        if kind == "report":
            await look.send_card(thread, body, author=look.who(conn, name), color=look.role_color(conn, name))
        elif kind == "status":
            state, _, rest = body.partition(":")
            await thread.send(embed=look.note(f"📌 {state}:{rest}", look.role_color(conn, name)))
        elif kind == "session_start":
            await thread.send(embed=look.note(f"🟢 **Started** in {body}"))
        elif kind == "session_end":
            await thread.send(embed=look.note(f"⚫ **Ended** ({body}). A message here brings it back."))
            await self.archive(thread)
        elif kind == "woken":
            await thread.send(embed=look.note(f"⏰ {body}"))
        elif kind == "stopped":
            await thread.send(embed=look.note(f"⏹️ **Stopped.** {body}"))
            await self.archive(thread)
        elif kind == "needs_you":
            await thread.send(embed=look.note(f"🔔 {body}", look.NEEDS_YOU))
            msg = await self.channels[NEEDS_YOU].send(
                content=f"<@{OWNER_ID}>",
                embed=look.card(body[:1500], author=look.who(conn, name), title="🔔 Needs you", color=look.NEEDS_YOU,
                                fields=[("Conversation", thread.mention, True)]),
                view=buttons(ReplyButton(name), AckButton()))
            conn.execute("INSERT INTO needs_you_posts (message_id, session_id, created_at) VALUES (?, ?, ?)",
                         (msg.id, sess["id"], store.now()))
        elif kind in ("agent_msg", "agent_msg_in"):
            # Bodies look like "📤 to **x** (hop 2): text" or "📥 from **x** (hop 2): text".
            # Bodies look like "📤 to **x** (hop 2, call): text".
            m = re.match(r"(📤 to|📥 from) \*\*(.+?)\*\* \(hop (\d+)(?:, (call|note))?\): (.*)", body, re.S)
            arrow, other, hop, how, text = m.groups() if m else ("", "", "?", None, body)
            header = f"{name} → {other}" if arrow.startswith("📤") else f"{other} → {name}"
            icon, how = ("📣", "call") if how == "call" or how is None else ("📝", "note")
            footer = f"{how} · hop {hop}"
            await look.send_card(thread, text, author=f"{icon} {header}", color=look.AGENTS, footer=footer)
            if kind == "agent_msg":
                await look.send_card(self.channels[CHATTER], text, author=f"{icon} {header}", color=look.AGENTS,
                                     footer=footer)

    async def post_request(self, conn, req):
        """A request the human must decide: charters go to the team's desk, everything else to #thunderhead."""
        charter = req["action"] == "charter"
        view = buttons(RequestButton("approve", req["id"]), RequestButton("reject", req["id"], charter=charter))
        if charter:
            desk = await self.team_desk(conn, req["team"])
            text = json.loads(req["params"])["text"]
            await look.send_card(desk, text, title=f"📜 Proposed charter · team {req['team']}", color=look.SUPERVISOR)
            msg = await desk.send(content=f"<@{OWNER_ID}>", embed=look.card(
                req["reason"][:1500], title="Approve this charter?", author=look.who(conn, req["from_name"]),
                color=look.APPROVAL, footer="Request changes to send the supervisor your feedback"), view=view)
        else:
            params = json.loads(req["params"])
            details = "\n".join(f"**{k}:** {str(v)[:300]}" for k, v in params.items())
            fields = [("Team", req["team"], True), ("Asked by", req["from_name"], True), ("Why", req["reason"], False)]
            if req["note"]:
                fields.append(("The Thunderhead's note", req["note"], False))
            msg = await self.channels[LEAD_CHANNEL].send(content=f"<@{OWNER_ID}>", embed=look.card(
                details, title=f"📋 Request #{req['id']} · {req['action']}", color=look.APPROVAL, fields=fields,
                footer="Reject to send feedback along with it"), view=view)
        conn.execute("UPDATE requests SET message_id=? WHERE id=?", (msg.id, req["id"]))

    async def clear_needs_you(self):
        """Remove #needs-you notices for sessions that don't need the human any more."""
        with store.db() as conn:
            stale = conn.execute("SELECT p.message_id FROM needs_you_posts p JOIN sessions s ON s.id=p.session_id "
                                 "WHERE s.status != 'needs_you'").fetchall()
            for (message_id,) in stale:
                try:
                    msg = await self.channels[NEEDS_YOU].fetch_message(message_id)
                    await msg.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    continue
                conn.execute("DELETE FROM needs_you_posts WHERE message_id=?", (message_id,))

    @tasks.loop(seconds=10)
    async def board(self):
        await self.guard("board", self._board)

    async def _board(self):
        """Keep the board in #fleet: one line per session, nothing hidden. If it outgrows one
        message it continues in more, kept together and edited in place."""
        await self.clear_needs_you()
        self.check_health()
        await self.send_alerts()
        await self.task_boards()
        with store.db() as conn:
            # Live sessions, plus ones that just ended (an hour) or were stopped (a day). A session
            # whose thread is archived is listed in #archived instead, so each appears in one place.
            rows = conn.execute(
                f"SELECT * FROM sessions WHERE (status NOT IN {store.DEAD} OR updated_at > ? "
                "OR (status='stopped' AND updated_at > ?)) AND status != 'deleted' "
                "AND (thread_id IS NULL OR thread_id NOT IN (SELECT thread_id FROM archived_posts)) "
                f"ORDER BY status IN {store.DEAD}, created_at",
                (time.time() - 3600, time.time() - 86400)).fetchall()
            seen, groups = set(), {}
            for s in rows:
                if s["name"] in seen or not store.is_current(conn, s):
                    continue  # older rows of a resumed session
                seen.add(s["name"])
                role, team = store.rank(conn, s["name"])
                key = ("👑 The Thunderhead" if role == "lead" else
                       f"🧭 Team {team} · {short_count(team_tokens(conn, team))} used" if team else "🛠️ Without a team")
                groups.setdefault(key, []).append((role != "supervisor", self.session_line(conn, s)))
            order = sorted(groups, key=lambda k: (not k.startswith("👑"), k.startswith("🛠️"), k))
            lines = []
            for key in order:
                lines += ([""] if lines else []) + [f"**{key}**"] + [line for _, line in sorted(groups[key])]
            pages = board_pages([self.health_line(), ""] + lines)
            if pages == self.last_board:
                return
            ids = json.loads(store.kv_get(conn, "board_message_ids") or "[]")
            legacy = store.kv_get(conn, "board_message_id")
            if not ids and legacy:
                ids = [int(legacy)]
            fleet = self.channels[FLEET]
            msgs = []
            for mid in ids:
                try:
                    msgs.append(await fleet.fetch_message(mid))
                except discord.NotFound:
                    msgs = None
                    break
            if msgs is not None and len(msgs) == len(pages):
                for msg, page in zip(msgs, pages):
                    if msg.content or not msg.embeds or msg.embeds[0].description != page:
                        await msg.edit(content=None, embed=board_embed(page, len(seen)))
                store.kv_set(conn, "board_message_ids", json.dumps([m.id for m in msgs]))
            else:
                # The page count changed: post the board afresh so its pages stay together.
                for msg in msgs or []:
                    try:
                        await msg.delete()
                    except discord.HTTPException:
                        pass
                msgs = [await fleet.send(embed=board_embed(page, len(seen))) for page in pages]
                try:
                    await msgs[0].pin()
                except discord.HTTPException:
                    pass
                store.kv_set(conn, "board_message_ids", json.dumps([m.id for m in msgs]))
                store.kv_set(conn, "board_message_id", msgs[0].id)
            self.last_board = pages

    TASK_SECTIONS = (("doing", "🔨 Doing"), ("review", "👀 Ready for review"), ("blocked", "🚧 Blocked"),
                     ("todo", "📝 To do"), ("done", "✅ Recently done"))

    async def task_boards(self):
        """One pinned task board per team, in its team channel, edited as tasks change."""
        with store.db() as conn:
            teams = [t["name"] for t in store.all_teams(conn)]
        for team in teams:
            with store.db() as conn:
                rows = org.team_tasks(conn, team, include_done=True)
                if not rows and store.kv_get(conn, f"task_board:{team}") is None:
                    continue  # nothing to show yet
                parts = []
                for status, title in self.TASK_SECTIONS:
                    items = [t for t in rows if t["status"] == status]
                    if status == "done":
                        items = sorted(items, key=lambda t: t["updated_at"])[-5:]
                    if items:
                        parts.append(f"**{title}**\n" + "\n".join(
                            org.task_line(t) + (f"\n╰ {t['note'][:120]}" if t["note"] and status != "done" else "")
                            for t in items))
                text = "\n\n".join(parts) or "*No open tasks.*"
                if self.last_tasks.get(team) == text:
                    continue
                channel = await self.group_channel(conn, team)
                embed = look.card(text[:look.DESC_LIMIT], title=f"📋 Tasks · team {team}", color=look.SUPERVISOR,
                                  footer="Kept by the team's supervisor · updated")
                mid = store.kv_get(conn, f"task_board:{team}")
                msg = None
                if mid:
                    try:
                        msg = await channel.fetch_message(int(mid))
                        await msg.edit(embed=embed)
                    except discord.NotFound:
                        msg = None
                if msg is None:
                    msg = await channel.send(embed=embed)
                    store.kv_set(conn, f"task_board:{team}", msg.id)
                    try:
                        await msg.pin()
                    except discord.HTTPException:
                        pass
                self.last_tasks[team] = text

    def session_line(self, conn, s) -> str:
        role, _ = store.rank(conn, s["name"])
        tag = " · supervisor" if role == "supervisor" else ""
        where = f" · <#{s['thread_id']}>" if s["thread_id"] else ""
        used = name_tokens(conn, s["name"])
        if used or s["context_tokens"]:
            where += f" · ctx {short_count(s['context_tokens'])} · {short_count(used)} used"
        summary = f"\n╰ {s['summary']}" if s["summary"] else ""
        return f"{ICONS.get(s['status'], '❔')} **{s['name']}**{tag} `{s['status']}`{where}{summary}"

    @tasks.loop(seconds=5)
    async def liveness(self):
        await self.guard("liveness", self._liveness)

    async def _liveness(self):
        """Reconcile with `claude agents`: shut down sleepers, wake sessions with mail, mark dead ones gone."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "agents", "--json", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 30)
            active = {a.get("sessionId"): a for a in json.loads(out or b"[]")}
        except Exception as e:
            self.problem("claude", f"`claude agents` failed, so sessions can't be woken, reaped or checked: {e!r}")
            return
        self.fine("claude")
        pids = {a.get("pid") for a in active.values()}
        to_wake, to_reap = [], []
        with store.db() as conn:
            # session id -> (oldest undelivered message time, whether any is from the human)
            mail = {r[0]: (r[1], bool(r[2])) for r in conn.execute(
                "SELECT to_session, MIN(created_at), MAX(from_kind IN ('human', 'lead')) FROM messages "
                "WHERE delivered_at IS NULL AND urgent=1 GROUP BY to_session")}

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

            # Stopped or ended sessions come back when the human or The Thunderhead writes to them.
            for sid, (_, authoritative) in mail.items():
                s = store.get_session(conn, sid)
                if (s and s["status"] in store.DEAD and s["status"] != "wiped" and authoritative
                        and sid not in self.waking
                        and store.is_current(conn, s)):
                    to_wake.append(s)

        with store.db() as conn:
            done = [n for (n,) in conn.execute("SELECT name FROM oneoffs")
                    if (store.session_by_name(conn, n) or {"status": None})["status"] in (store.SLEEPING, "ended", "gone")]
        for name in done:
            if name not in self.deleting:
                self.deleting.add(name)
                try:
                    await self.delete_session(name, keep_thread=True, note=" automatically (it was a one-off and is done)")
                finally:
                    self.deleting.discard(name)
        for s in to_reap:
            await run_claude(["claude", "stop", short_id(s["id"])])
        await self.refresh_stale(active)
        for s in to_wake:
            asyncio.create_task(self.wake(s))

    # -- health --

    LOOP_INTERVALS = {"pump": 2, "board": 10, "liveness": 5}
    ALERT_AFTER = 30  # seconds a problem must last before it pings the human

    async def guard(self, name, body):
        try:
            await body()
        except Exception as e:
            log.exception("The %s loop failed", name)
            self.problem(f"loop:{name}", f"The bot's {name} loop failed: {e!r}. It keeps retrying.")
        else:
            self.fine(f"loop:{name}")
        self.beats[name] = time.time()

    def problem(self, key: str, text: str):
        if key not in self.issues:
            self.issues[key] = {"since": time.time(), "text": text, "alert": None}
        else:
            self.issues[key]["text"] = text

    def fine(self, key: str):
        issue = self.issues.pop(key, None)
        if issue and issue["alert"]:
            self.resolved.append(issue["alert"])

    def check_health(self):
        """Problems the loops can't report themselves: a stalled loop, and new hook errors."""
        now = time.time()
        for name, every in self.LOOP_INTERVALS.items():
            last = self.beats.get(name)
            if last and now - last > max(60, every * 6):
                self.problem(f"stall:{name}", f"The bot's {name} loop hasn't run for {int(now - last)}s.")
            elif last:
                self.fine(f"stall:{name}")
        log_file = ROOT / "data" / "hook-errors.log"
        size = log_file.stat().st_size if log_file.exists() else 0
        if self.hook_log_size is not None and size > self.hook_log_size:
            tail = log_file.read_text(errors="replace")[-600:]
            self.problem("hooks", f"Session hooks logged new errors (data/hook-errors.log):\n```\n{tail}\n```")
            self.hook_errors_at = now
        elif "hooks" in self.issues and now - self.hook_errors_at > 1800:
            self.fine("hooks")  # quiet for half an hour
        self.hook_log_size = size

    def health_line(self) -> str:
        if not self.issues:
            return "🩺 **Health:** all systems normal"
        lines = [f"🩺 **Health: {len(self.issues)} problem{'s' if len(self.issues) > 1 else ''}**"]
        lines += [f"⚠️ {i['text'].splitlines()[0][:150]}" for i in self.issues.values()]
        return "\n".join(lines)

    async def send_alerts(self):
        """Ping the human about problems that have lasted, and clear alerts for fixed ones."""
        ch = self.channels.get(NEEDS_YOU)
        if ch is None:
            return
        while self.resolved:
            try:
                await (await ch.fetch_message(self.resolved.pop())).delete()
            except discord.HTTPException:
                pass
        for key, issue in self.issues.items():
            if issue["alert"] is None and time.time() - issue["since"] >= self.ALERT_AFTER:
                msg = await ch.send(content=f"<@{OWNER_ID}>", embed=look.card(
                    issue["text"][:3500], title="🩺 Something in THUNDERHEAD is failing", color=look.BAD,
                    footer="This clears itself once it's fixed · the bot's log is data/bot.log"), view=ack_view())
                issue["alert"] = msg.id

    async def refresh_stale(self, active: dict):
        """Sessions that loaded older tools get them on their next wake-up. So once an idle background
        session is running old tools, put it to sleep: the next message wakes it with the new ones.
        Busy sessions are left alone until they're idle."""
        current = store.tools_version()
        with store.db() as conn:
            stale = [s for s in store.live_sessions(conn)
                     if s["tools_version"] != current and s["status"] in ("listening", "idle")
                     and (active.get(s["id"]) or {}).get("kind") == "background"
                     and s["id"] not in self.waking]
            for s in stale:
                store.set_status(conn, s["id"], store.SLEEPING)  # quiet SessionEnd; any message wakes it
        for s in stale:
            await run_claude(["claude", "stop", short_id(s["id"])])
            log.info("Put %s to sleep to pick up updated tools", s["name"])

    @tasks.loop(seconds=MEMORY_SNAPSHOT_SECONDS)
    async def snapshot_memory(self):
        await self.guard("snapshot_memory", self._snapshot_memory)

    async def _snapshot_memory(self):
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
                self.problem("memory", f"The memory repo snapshot failed: {text[-300:]}")
            else:
                self.fine("memory")

    def usage_paused(self, conn) -> dict | None:
        """The current usage-limit pause, unless it's time to let a wake-up through to probe it."""
        raw = store.kv_get(conn, "usage_limit")
        if raw is None:
            return None
        info = json.loads(raw)
        return info if time.time() - info["since"] < USAGE_PROBE_SECONDS else None

    async def wake(self, sess):
        """Resume a session in the background with its queued messages as the prompt.

        Also used for a running session that went idle without listening: it is stopped first.
        """
        sid = sess["id"]
        if sid in self.waking or time.time() - self.wake_failed.get(sid, 0) < 300:
            return
        with store.db() as conn:
            if self.usage_paused(conn):
                return  # queued; delivered once the usage limit resets
        self.waking.add(sid)
        try:
            # Never restart a session that's open in someone's terminal; it gets the
            # message at its next turn there.
            if (await agent_info(sid) or {}).get("kind") == "interactive":
                return
            # A team caps how many of its devs are awake at once. A call from another session waits
            # for a free slot (the next check tries again); one from the human goes through.
            with store.db() as conn:
                role, team = store.rank(conn, sess["name"])
                if role == "dev":
                    t = store.get_team(conn, team)
                    from_human = conn.execute("SELECT 1 FROM messages WHERE to_session=? AND delivered_at IS NULL "
                                              "AND urgent=1 AND from_kind='human'", (sid,)).fetchone()
                    if not from_human and len(store.awake_devs(conn, team)) >= t["max_awake"]:
                        return
            with store.db() as conn:
                rows = store.take_messages(conn, sid)
                if not rows:
                    return
                text_for = delivery(conn, sid, rows)
                relaunch_cmd = relaunch_command(conn, sess, resume=sid)
                store.set_status(conn, sid, "waking")
            if not Path(sess["cwd"] or "").is_dir():
                code, text = 1, f"its folder `{sess['cwd']}` no longer exists."
            else:
                # Fails harmlessly when the process has already exited.
                await run_claude(["claude", "stop", short_id(sid)])
                code, text = await run_claude(
                    relaunch_cmd + [text_for], cwd=sess["cwd"])
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

    async def tidy_fleet(self):
        """Once at startup: add Acknowledge to older session notices, and clear pin notices,
        so #fleet is just the board."""
        with store.db() as conn:
            board_ids = set(json.loads(store.kv_get(conn, "board_message_ids") or "[]"))
            board_ids.add(int(store.kv_get(conn, "board_message_id") or 0))
            team_ids = [r[0] for r in conn.execute(
                "SELECT discord_id FROM channels WHERE team IS NOT NULL AND discord_id IS NOT NULL")]
        channels = [self.channels[FLEET]] + [ch for ch in map(self.get_channel, team_ids) if ch]
        for ch in channels:
            async for thread in ch.archived_threads(limit=None):
                await self.sync_archived(thread.id, True)
            for thread in ch.threads:  # active threads
                await self.sync_archived(thread.id, False)
                with store.db() as conn:
                    sess = store.session_by_thread(conn, thread.id)
                try:
                    if sess is not None and sess["status"] in store.DEAD:
                        await thread.edit(archived=True)
                    elif thread.auto_archive_duration != THREAD_ARCHIVE_MINUTES:
                        await thread.edit(auto_archive_duration=THREAD_ARCHIVE_MINUTES)
                except discord.HTTPException:
                    log.warning("Couldn't tidy thread %s", thread.id)
            async for msg in ch.history(limit=200):
                try:
                    if msg.type == discord.MessageType.pins_add and msg.author == self.user:
                        await msg.delete()
                    elif (msg.author == self.user and msg.id not in board_ids
                          and msg.content.startswith("🆕") and not msg.components):
                        await msg.edit(view=ack_view())
                except discord.HTTPException:
                    log.warning("Couldn't tidy message %s in #%s", msg.id, ch.name)

    async def on_message(self, message: discord.Message):
        # The bot pins its boards; the "pinned a message" notices are clutter.
        if message.type == discord.MessageType.pins_add and message.author == self.user:
            await message.delete()
            return
        if message.author.bot or message.guild is None:
            return
        channel = message.channel
        with store.db() as conn:
            group = store.channel_by_discord(conn, channel.id)
            session_channel = (channel.id == self.channels[LEAD_CHANNEL].id or group is not None
                               or store.session_by_thread(conn, channel.id) is not None
                               or conn.execute("SELECT 1 FROM teams WHERE desk_id=?", (channel.id,)).fetchone())
        if not session_channel:
            return
        if not is_owner(message.author):
            await message.add_reaction("⛔")
            return
        text = await self.with_attachments(message)
        if not text.strip():
            return  # nothing to deliver (a sticker, say)
        with store.db() as conn:
            if channel.id == self.channels[LEAD_CHANNEL].id:
                sess = store.session_by_name(conn, store.LEAD)
                if sess is not None and sess["status"] == "wiped":
                    sess = None
            elif isinstance(channel, discord.Thread):
                sess = store.session_by_thread(conn, channel.id)
            else:
                desk = conn.execute("SELECT supervisor FROM teams WHERE desk_id=?", (channel.id,)).fetchone()
                sess = store.session_by_name(conn, desk["supervisor"]) if desk else None
            if sess is None and group is None and channel.id != self.channels[LEAD_CHANNEL].id:
                return
            if group is not None:
                # @name calls those sessions. With no mentions, only the channel's coordinators are
                # called (its team's supervisor, or the supervisors and The Thunderhead in it), and
                # everyone else gets a note, so one message doesn't wake a whole team.
                roster = store.members(conn, group["name"])
                mentioned = [m for m in re.findall(r"@([A-Za-z0-9_-]+)", message.content) if m in roster]
                if not mentioned:
                    team = store.get_team(conn, group["team"]) if group["team"] else None
                    mentioned = ([team["supervisor"]] if team else
                                 [m for m in roster if store.rank(conn, m)[0] in ("lead", "supervisor")]) or ["all"]
                got = store.fan_out(conn, group["name"], "human", message.author.display_name,
                                    text, notify=mentioned, note_others=True)
                targets = [store.session_by_name(conn, n) for n in got]
            elif sess is None:
                targets = None  # no Thunderhead yet
            else:
                store.queue_message(conn, sess["id"], "human", message.author.display_name, text)
                targets = [sess]
                self.copy_up(conn, sess["name"], text)
        if targets is None:
            await self.start_lead(first_message=text)
            await message.add_reaction("⚡")
            return
        await message.add_reaction("📨")
        with store.db() as conn:
            paused = self.usage_paused(conn)
        if paused:
            await message.reply(embed=look.note("⏸️ Queued. The fleet is paused on a usage limit; this is "
                                                "delivered once it resets.", look.BAD), mention_author=False)
        for t in targets:
            await self.deliver_now(t, message if group is None else None)

    async def with_attachments(self, message: discord.Message) -> str:
        """The message's text, plus its attachments saved to disk. Sessions read the local copy
        (images included); Discord's links expire after about a day, so they're only a fallback."""
        text = message.content
        lines = []
        for att in message.attachments:
            if att.size > ATTACHMENT_LIMIT:
                lines.append(f"- {att.filename} ({att.size // 1_000_000} MB, too big to save): {att.url}")
                continue
            path = ATTACHMENTS / str(message.id) / Path(att.filename).name
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                await att.save(path)
                lines.append(f"- {path} ({att.content_type or 'file'}; original link: {att.url})")
            except discord.HTTPException:
                lines.append(f"- {att.filename} (couldn't be saved): {att.url}")
        if lines:
            text += ("\n\n" if text else "") + "Attached:\n" + "\n".join(lines)
        return text

    def copy_up(self, conn, name, text):
        """When the human goes around a level, tell the level they skipped (as an FYI, not a task),
        so its picture of the team doesn't go stale."""
        role, team = store.rank(conn, name)
        if role == "supervisor":
            org.fyi(conn, store.LEAD, f"The human messaged {name} (supervisor of {team}) directly: {text[:1500]}")
        elif role == "dev":
            sup = store.get_team(conn, team)["supervisor"]
            org.fyi(conn, sup, f"The human messaged your dev {name} directly: {text[:1500]}")

    async def start_lead(self, first_message: str | None = None) -> tuple[int, str]:
        """Start a fresh Thunderhead: no earlier conversation, memory from hq/NOTES.md."""
        cmd, cwd = lead_command(first_message)
        code, text = await run_claude(cmd, cwd=cwd)
        await self.channels[LEAD_CHANNEL].send(embed=look.note(
            "⚡ Starting a fresh Thunderhead…", look.LEAD) if code == 0 else look.card(
            f"```\n{text}\n```", title="Couldn't start The Thunderhead", color=look.BAD))
        return code, text

    async def deliver_now(self, sess, message=None):
        """Wake a sleeping or stopped session right away instead of waiting for the next check."""
        if sess["status"] in store.DEAD or sess["status"] == store.SLEEPING:
            asyncio.create_task(self.wake(sess))
        elif not sess["listen"] and sess["status"] not in ("working", "needs_you") and message:
            await message.reply(embed=look.note("📨 Queued. This session runs in a terminal and isn't listening, "
                                                "so it gets the message when its next turn ends."), mention_author=False)


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
            "SELECT name FROM sessions WHERE status != 'deleted' GROUP BY name ORDER BY MAX(updated_at) DESC")]
    return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]


async def team_names(interaction, current: str):
    with store.db() as conn:
        names = [t["name"] for t in store.all_teams(conn)]
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
        lines = [bot.session_line(conn, s) for s in store.live_sessions(conn) if store.is_current(conn, s)]
    await interaction.response.send_message(embeds=[board_embed(p, len(lines)) for p in board_pages(lines)][:10],
                                            ephemeral=True)


@bot.tree.command(description="Send a message to a session (leave out the message for a form)")
@app_commands.describe(message="Leave it out to write a longer message in a pop-up form")
@app_commands.autocomplete(session=session_names)
async def send(interaction: discord.Interaction, session: str, message: str | None = None):
    if not await owner_only(interaction):
        return
    if message is None:
        await interaction.response.send_modal(ReplyModal(session))
        return
    await send_to_session(interaction, session, message)


class SpawnModal(OwnerOnly, discord.ui.Modal, title="Start a session"):
    """The /spawn form: room for a real multi-line task."""

    def __init__(self, directory: str, name: str | None, options: dict):
        super().__init__(title=f"Start a session{' on team ' + options['team'] if options.get('team') else ''}"[:45])
        self.directory = _text("Working directory", default=directory or None, max_length=500,
                               placeholder="~/projects/my-app (leave empty for a fresh workspace)", required=False)
        self.session_name = _text("Name (optional)", default=name, required=False, max_length=40,
                                  placeholder="letters, digits, - and _")
        self.task = _text("Task", paragraph=True, placeholder="What should the session do? Give it everything it needs.")
        for item in (self.directory, self.session_name, self.task):
            self.add_item(item)
        self.options = options

    async def on_submit(self, interaction: discord.Interaction):
        await do_spawn(interaction, self.directory.component.value.strip(), self.task.component.value,
                       self.session_name.component.value.strip() or None, **self.options)


@bot.tree.command(description="Start a new background Claude session (leave out the task for a form)")
@app_commands.describe(directory="Working directory (absolute or ~/...); empty for a fresh workspace",
                       task="What the session should do. Leave it out to write it in a pop-up form",
                       name="Session name (letters, digits, - and _)", mode="Permission mode",
                       team="Put it on this team as a dev, reporting to the team's supervisor",
                       oneoff="Delete it automatically once it's done and falls asleep (its thread is kept)",
                       model="Model to run on (default sonnet)")
@app_commands.choices(mode=[app_commands.Choice(name=m, value=m)
                            for m in ("default", "acceptEdits", "auto", "plan")],
                      model=[app_commands.Choice(name=m, value=m) for m in config.MODELS])
@app_commands.autocomplete(team=team_names)
async def spawn(interaction: discord.Interaction, directory: str = "", task: str | None = None,
                name: str | None = None, mode: app_commands.Choice[str] | None = None,
                team: str | None = None, oneoff: bool = False, model: app_commands.Choice[str] | None = None):
    if not await owner_only(interaction):
        return
    options = {"mode": mode.value if mode else None, "team": team, "oneoff": oneoff,
               "model": model.value if model else None}
    if task is None:
        await interaction.response.send_modal(SpawnModal(directory, name, options))
        return
    await do_spawn(interaction, directory, task, name, **options)


async def do_spawn(interaction: discord.Interaction, directory: str, task: str, name: str | None,
                   mode: str | None = None, team: str | None = None, oneoff: bool = False, model: str | None = None):
    async def fail(text):
        await interaction.response.send_message(embed=look.card(text, title="Couldn't start it", color=look.BAD),
                                                ephemeral=True)
    if directory:
        cwd = Path(directory).expanduser()
        if not cwd.is_dir() or launch.forbidden_dir(cwd):
            await fail(launch.forbidden_dir(cwd) or f"`{cwd}` is not a directory.")
            return
    else:
        cwd = None
    name = name or f"{cwd.name if cwd else 'session'}-{secrets.token_hex(2)}"
    if not NAME_RE.match(name) or name == store.LEAD:
        await fail("Names can only use letters, digits, - and _ ('thunderhead' is taken by The Thunderhead).")
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    cwd = cwd or launch.workspace_dir(name)

    if team:
        with store.db() as conn:
            if store.get_team(conn, team) is None:
                await interaction.followup.send(f"No team `{team}`.", ephemeral=True)
                return
            # You're the authority: your spawns skip the team's awake and model limits.
            err, cmd, _ = org.spawn_dev(conn, team, str(cwd), task, name, model or "", by="human")
            if not err:
                org.fyi(conn, store.get_team(conn, team)["supervisor"],
                        f"The human spawned '{name}' onto your team with this task: {task[:1000]}")
        if err:
            await interaction.followup.send(embed=look.card(err, title="Couldn't start it", color=look.BAD),
                                            ephemeral=True)
            return
        prompt = cmd.pop()
    else:
        cmd, prompt = bg_command(name, model=model), task
        with store.db() as conn:
            store.set_model(conn, name, model or config.DEFAULT_DEV_MODEL)
    if mode:
        # Replace the fleet's default permission mode with the one you picked.
        if "--permission-mode" in cmd:
            i = cmd.index("--permission-mode")
            del cmd[i:i + 2]
        if mode != "default":
            cmd += ["--permission-mode", mode]
    code, text = await run_claude(cmd + [prompt], cwd=cwd)
    if code != 0:
        if team:
            with store.db() as conn:
                org.undo_spawn(conn, team, name)
        await interaction.followup.send(embed=look.card(f"```\n{text}\n```", title="Spawn failed", color=look.BAD),
                                        ephemeral=True)
        return
    if oneoff:
        with store.db() as conn:
            conn.execute("INSERT OR IGNORE INTO oneoffs (name) VALUES (?)", (name,))
    where = f"team {team}'s channel" if team else f"#{FLEET}"
    fields = [("Folder", f"`{cwd}`", False), ("Thread", f"appears in {where} once it starts", True),
              ("Model", model or config.DEFAULT_DEV_MODEL, True)]
    if oneoff:
        fields.append(("One-off", "deleted once it's done and falls asleep; its thread is kept", True))
    await interaction.followup.send(embed=look.card(task[:1500], title=f"🚀 Started {name}", color=look.GOOD,
                                                    fields=fields), ephemeral=True)


@bot.tree.command(name="archive", description="Done with a conversation: file its thread away and let the session sleep")
@app_commands.describe(session="The session (leave empty inside its thread)")
@app_commands.autocomplete(session=session_names)
async def archive_cmd(interaction: discord.Interaction, session: str | None = None):
    if not await owner_only(interaction):
        return
    with store.db() as conn:
        if session:
            sess = store.session_by_name(conn, session)
        elif isinstance(interaction.channel, discord.Thread):
            sess = store.session_by_thread(conn, interaction.channel.id)
        else:
            sess = None
        role = store.rank(conn, sess["name"])[0] if sess else None
    if sess is None:
        await interaction.response.send_message(
            "Name a session, or run /archive inside its thread.", ephemeral=True)
        return
    if role in ("lead", "supervisor"):
        await interaction.response.send_message(
            f"**{sess['name']}** lives in a channel, not a thread, and Discord can't archive channels.",
            ephemeral=True)
        return
    if sess["status"] in ("working", "needs_you", "waking"):
        await interaction.response.send_message(
            f"**{sess['name']}** is `{sess['status']}` right now. Let it finish, or /stop it if you mean to "
            "interrupt it.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    await bot.archive_session(sess, "you")
    await interaction.followup.send(embed=look.note(f"🗄️ Archived **{sess['name']}**. It's listed in #{ARCHIVED}."),
                                    ephemeral=True)


class ConfirmDelete(discord.ui.View):
    def __init__(self, name: str, keep_thread: bool):
        super().__init__(timeout=120)
        self.name, self.keep_thread = name, keep_thread

    async def interaction_check(self, interaction) -> bool:
        return is_owner(interaction.user)

    @discord.ui.button(label="Delete forever", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=look.note(f"Deleting **{self.name}**…"), view=None)
        result = await bot.delete_session(self.name, self.keep_thread)
        await interaction.edit_original_response(embed=look.note(result, look.GOOD if result.startswith("🗑️") else look.BAD))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=look.note("Cancelled. Nothing was deleted."), view=None)


@bot.tree.command(name="delete", description="Delete a session for good (asks you to confirm)")
@app_commands.describe(session="The session to delete",
                       keep_thread="Keep its Discord thread (archived) as a record")
@app_commands.autocomplete(session=session_names)
async def delete(interaction: discord.Interaction, session: str, keep_thread: bool = False):
    if not await owner_only(interaction):
        return
    with store.db() as conn:
        err = org.delete_check(conn, session)
        sess = store.session_by_name(conn, session)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return
    busy = " It's **working right now**; deleting stops it mid-task." if sess["status"] in ("working", "needs_you") else ""
    thread = "Its thread is kept and archived." if keep_thread else "Its thread and history in Discord go too."
    await interaction.response.send_message(embed=look.card(
        f"This stops it, removes its Claude job and its fleet records, and frees the name. {thread}{busy}",
        title=f"🗑️ Delete {session} for good?", color=look.BAD, footer="This can't be undone"),
        view=ConfirmDelete(session, keep_thread), ephemeral=True)


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
        await interaction.followup.send(embed=look.card(
            f"Only background sessions can be stopped from here.\n```\n{text}\n```",
            title=f"Couldn't stop {session}", color=look.BAD), ephemeral=True)
        return
    await interaction.followup.send(embed=look.card(resume_hint(sess), title=f"⏹️ Stopped {session}",
                                                    color=look.QUIET, footer="Its conversation is kept"),
                                    ephemeral=True)


@bot.tree.command(description="Delete threads of sessions that ended a while ago (asks before deleting)")
@app_commands.describe(days="Only sessions that ended at least this many days ago",
                       confirm="Leave off to see what would be deleted; set to True to delete")
async def cleanup(interaction: discord.Interaction, days: app_commands.Range[int, 1, 3650] = 30,
                  confirm: bool = False):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    cutoff = time.time() - days * 86400
    with store.db() as conn:
        # Current row per name, finished, idle past the cutoff, with a thread of its own (not a desk).
        rows = [s for s in conn.execute(
            f"SELECT * FROM sessions WHERE status IN {store.DEAD} AND updated_at < ? AND thread_id IS NOT NULL",
            (cutoff,)).fetchall()
            if s["status"] == "deleted"
            or (store.is_current(conn, s) and store.rank(conn, s["name"])[0] in ("dev", "unteamed"))]
    if not rows:
        await interaction.followup.send(f"No finished sessions older than {days} days.", ephemeral=True)
        return
    names = ", ".join(s["name"] for s in rows)
    if not confirm:
        await interaction.followup.send(embed=look.card(
            names, title=f"🧹 Would delete {len(rows)} thread(s)", color=look.APPROVAL,
            footer=f"This can't be undone. Run /cleanup days:{days} confirm:True to do it."), ephemeral=True)
        return
    deleted = 0
    for s in rows:
        try:
            thread = bot.get_channel(s["thread_id"]) or await bot.fetch_channel(s["thread_id"])
            await thread.delete()
            deleted += 1
        except discord.NotFound:
            pass
        with store.db() as conn:
            post = conn.execute("SELECT message_id FROM archived_posts WHERE thread_id=?", (s["thread_id"],)).fetchone()
            if post:
                try:
                    await (await bot.channels[ARCHIVED].fetch_message(post["message_id"])).delete()
                except discord.NotFound:
                    pass
                conn.execute("DELETE FROM archived_posts WHERE thread_id=?", (s["thread_id"],))
            conn.execute("UPDATE sessions SET thread_id=NULL WHERE id=?", (s["id"],))
            conn.execute("DELETE FROM sessions WHERE id=? AND status='deleted'", (s["id"],))
    await interaction.followup.send(embed=look.card(names, title=f"🧹 Deleted {deleted} thread(s)", color=look.GOOD),
                                    ephemeral=True)


@bot.tree.command(name="team-config", description="Set a team's autonomy, awake limit and model limit")
@app_commands.describe(team="Team", autonomy="propose: works only on what it's given; act: picks up its own backlog",
                       max_awake="How many of its devs may be awake at once (it may keep any number)",
                       max_model="The strongest model its supervisor may give a dev without asking")
@app_commands.choices(autonomy=[app_commands.Choice(name=a, value=a) for a in org.AUTONOMY],
                      max_model=[app_commands.Choice(name=m, value=m) for m in config.MODELS])
@app_commands.autocomplete(team=team_names)
async def team_config(interaction: discord.Interaction, team: str,
                      autonomy: app_commands.Choice[str] | None = None,
                      max_awake: app_commands.Range[int, 0, 50] | None = None,
                      max_model: app_commands.Choice[str] | None = None):
    if not await owner_only(interaction):
        return
    with store.db() as conn:
        t = store.get_team(conn, team)
        if t is None:
            await interaction.response.send_message(f"No team `{team}`.", ephemeral=True)
            return
        if autonomy is None and max_awake is None and max_model is None:
            await interaction.response.send_message(embed=look.card(
                title=f"🧭 Team {team}", color=look.SUPERVISOR, fields=[
                    ("Autonomy", t["autonomy"], True),
                    ("Awake", f"{len(store.awake_devs(conn, team))} of {t['max_awake']}", True),
                    ("Devs kept", str(store.dev_count(conn, team)), True),
                    ("Max model", t["max_model"], True), ("Charter", t["charter_status"], True)]), ephemeral=True)
            return
        result = org.apply_config(conn, team, {"autonomy": autonomy.value if autonomy else None,
                                              "max_awake": max_awake,
                                              "max_model": max_model.value if max_model else None}, by="human")
    await interaction.response.send_message(embed=look.note(f"⚙️ {result}", look.SUPERVISOR), ephemeral=True)


@bot.tree.command(description="Give The Thunderhead a fresh start (its memory comes from hq/NOTES.md)")
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
    await bot.channels[LEAD_CHANNEL].send(embed=look.note("🧹 **Wiped.** The Thunderhead's conversation was cleared. "
                                                           "It starts again from its notes.", look.LEAD))
    code, text = await bot.start_lead()
    await interaction.followup.send("Fresh Thunderhead starting." if code == 0 else f"Failed:\n```\n{text}\n```",
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
