"""Per-session MCP server: the tools a Claude session uses to talk to the fleet.

The ThunderHead (the lead session) gets extra tools on top. They are only registered
when this server runs inside it, and each one checks the caller's role again.
"""
import json
import os
import re
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import db as store
from . import launch
from .config import MAX_HOPS
from .hooks import _hops_after, delivery

mcp = MCPServer("thunderhead", instructions="Talk to the human (via Discord) and to other Claude sessions.")

STATES = ("working", "blocked", "done")
# Posts per channel per minute before post() refuses, so a busy channel can't flood everyone.
CHANNEL_RATE = 20


def _me(conn):
    """This session's row. The session id can change after /clear, so try the pid first."""
    row = None
    if os.environ.get("CLAUDE_PID", "").isdigit():
        row = store.session_by_pid(conn, int(os.environ["CLAUDE_PID"]))
    if row is None and os.environ.get("CLAUDE_CODE_SESSION_ID"):
        row = store.get_session(conn, os.environ["CLAUDE_CODE_SESSION_ID"])
    if row is None:
        raise RuntimeError("This session is not registered with THUNDERHEAD "
                           "(start it with th-claude or /spawn so the hooks run).")
    return row


def _is_lead(me) -> bool:
    return me["role"] == "lead" and me["name"] == store.LEAD


def _kind(me) -> str:
    return "lead" if _is_lead(me) else "agent"


def _next_hops(me) -> int | None:
    hops = me["current_hops"] + 1
    return None if hops > MAX_HOPS else hops


HOPS_REFUSAL = (f"Refused: this conversation between agents has gone {MAX_HOPS} hops without the human. "
                "Use report() to ask the human how to proceed.")


# --- every session ----------------------------------------------------------

@mcp.tool()
def status(state: str, summary: str) -> str:
    """Update your status on the human's fleet board.

    state: working, blocked or done. summary: one short line on what you are doing or why you are blocked.
    """
    if state not in STATES:
        return f"state must be one of {', '.join(STATES)}"
    with store.db() as conn:
        me = _me(conn)
        store.set_status(conn, me["id"], me["status"], summary=f"{state}: {summary}")
        store.post(conn, me["id"], "status", f"**{state}**: {summary}")
    return "Status updated."


@mcp.tool()
def report(message: str) -> str:
    """Send a message to the human in your Discord thread: results, questions, or anything they should see."""
    with store.db() as conn:
        me = _me(conn)
        store.post(conn, me["id"], "report", message)
    return "Sent to the human."


@mcp.tool()
def send(to: str, message: str) -> str:
    """Send a private message to another Claude session by name.

    It is delivered when that session's turn ends. A sleeping session is woken to receive it.
    """
    with store.db() as conn:
        me = _me(conn)
        target = store.session_by_name(conn, to)
        lead = _is_lead(me)
        if target is None or (target["status"] in store.DEAD and not lead):
            names = ", ".join(s["name"] for s in store.live_sessions(conn) if s["id"] != me["id"])
            why = "was stopped by the human" if target and target["status"] == "stopped" else "isn't running"
            return (f"'{to}' {why}; only the human or The ThunderHead can restart it. "
                    f"Sessions you can message: {names or 'none'}.")
        if target["id"] == me["id"]:
            return "That is you."
        hops = _next_hops(me)
        if hops is None:
            return HOPS_REFUSAL
        store.queue_message(conn, target["id"], _kind(me), me["name"], message, hops=hops)
        store.post(conn, me["id"], "agent_msg", f"📤 to **{target['name']}** (hop {hops}): {message}")
        store.post(conn, target["id"], "agent_msg_in", f"📥 from **{me['name']}** (hop {hops}): {message}")
    if target["status"] in store.DEAD + (store.SLEEPING,):
        return f"Queued for {to}. It isn't running and will be woken to receive it (about 20 seconds)."
    return f"Queued for {to}."


@mcp.tool()
def post(channel: str, message: str, notify: list[str]) -> str:
    """Post to a group channel to reach specific members. The human sees every post in Discord.

    Only post when you need someone to read it. notify: the member session names who need it
    (they're woken and get it now), or ["all"] when every member really must respond. Other members
    aren't interrupted; they see it as unread. To record a decision or document how something works,
    write documentation where it belongs instead, then post to point the right people at it.
    """
    with store.db() as conn:
        me = _me(conn)
        ch = store.get_channel(conn, channel.lstrip("#"))
        if ch is None or ch["closed"]:
            return f"No open channel #{channel}. channels() lists yours."
        if me["name"] not in store.members(conn, ch["name"]) and not _is_lead(me):
            return f"You aren't a member of #{ch['name']}. Ask The ThunderHead to add you."
        roster = store.members(conn, ch["name"])
        unknown = [n for n in notify if n != "all" and n not in roster]
        if not notify or unknown:
            return (f"notify must name members of #{ch['name']} ({', '.join(roster)}) or be [\"all\"]"
                    + (f"; not members: {', '.join(unknown)}" if unknown else "") + ".")
        if store.recent_posts(conn, ch["name"]) >= CHANNEL_RATE:
            return f"#{ch['name']} is busy ({CHANNEL_RATE} posts in the last minute). Wait, then try again."
        hops = _next_hops(me)
        if hops is None:
            return HOPS_REFUSAL
        got = store.fan_out(conn, ch["name"], _kind(me), me["name"], message, notify, hops=hops)
        pinged = "everyone" if "all" in notify else ", ".join(f"@{n}" for n in got) or "nobody"
        store.post(conn, me["id"], "channel_post", f"→ {pinged}\n{message}", channel=ch["name"])
    return f"Posted to #{ch['name']}; notified {', '.join(got) or 'nobody'}."


@mcp.tool()
def read_channel(channel: str, limit: int = 20) -> str:
    """Show a group channel's recent posts (including ones you weren't pinged on) and mark them read."""
    with store.db() as conn:
        me = _me(conn)
        name = channel.lstrip("#")
        if me["name"] not in store.members(conn, name) and not _is_lead(me):
            return f"You aren't a member of #{name}."
        rows = store.read_log(conn, name, me["name"], limit=max(1, min(limit, 100)))
    if not rows:
        return f"#{name} has no posts yet."
    return "\n\n".join(f"[{r['from_name']} → {', '.join(json.loads(r['notified'])) or 'nobody'}]\n{r['body']}"
                        for r in rows)


@mcp.tool()
def channels() -> str:
    """List the group channels you're in, with their topic and members."""
    with store.db() as conn:
        me = _me(conn)
        names = ([c["name"] for c in store.open_channels(conn)] if _is_lead(me)
                 else store.channels_of(conn, me["name"]))
        rows = [(store.get_channel(conn, n), store.members(conn, n)) for n in names]
        unread = dict(store.unread(conn, me["name"]))
    if not rows:
        return "You aren't in any group channels."
    return "\n".join(f"- #{c['name']}: {c['topic'] or 'no topic'} (members: {', '.join(m)})"
                     + (f", {unread[c['name']]} unread" if c["name"] in unread else "") for c, m in rows)


@mcp.tool()
def sessions() -> str:
    """List the other sessions in the fleet with their status and what they are working on."""
    with store.db() as conn:
        me = _me(conn)
        rows = [s for s in store.live_sessions(conn) if s["id"] != me["id"]]
    if not rows:
        return "No other live sessions."
    return "\n".join(f"- {s['name']} [{s['status']}] {s['summary'] or ''} (cwd: {s['cwd']})" for s in rows)


@mcp.tool()
def inbox() -> str:
    """Check for new messages without waiting for your turn to end."""
    with store.db() as conn:
        me = _me(conn)
        rows = store.take_messages(conn, me["id"])
        if rows:
            conn.execute("UPDATE sessions SET current_hops=? WHERE id=?", (_hops_after(rows), me["id"]))
        return delivery(conn, me["id"], rows) if rows else "No new messages."


# --- The ThunderHead only ---------------------------------------------------

def _lead_only(conn):
    me = _me(conn)
    if not _is_lead(me):
        raise PermissionError("Only The ThunderHead can do that.")
    return me


def _notify(conn, lead, names, text):
    """A direct note from The ThunderHead to each named session."""
    for name in names:
        target = store.session_by_name(conn, name)
        if target is not None and name != lead["name"]:
            store.queue_message(conn, target["id"], "lead", lead["name"], text, hops=1)


def _unknown(conn, names) -> list[str]:
    return [n for n in names if store.session_by_name(conn, n) is None]


def fleet() -> str:
    """Everything at a glance: every session (including stopped ones from the last day), every channel, queued mail."""
    with store.db() as conn:
        _lead_only(conn)
        rows = conn.execute(
            f"SELECT * FROM sessions WHERE status NOT IN {store.DEAD} OR updated_at > ? "
            f"ORDER BY status IN {store.DEAD}, created_at", (store.now() - 86400,)).fetchall()
        current = {}
        for s in rows:  # one row per name: the current one
            if s["name"] not in current and store.session_by_name(conn, s["name"])["id"] == s["id"]:
                current[s["name"]] = s
        lines = ["Sessions:"]
        for s in current.values():
            mail = store.pending_count(conn, s["id"])
            lines.append(f"- {s['name']} [{s['status']}] {s['summary'] or ''} (cwd: {s['cwd']}"
                         + (f", {mail} queued" if mail else "") + ")")
        lines.append("Channels:")
        for c in store.open_channels(conn):
            lines.append(f"- #{c['name']}: {c['topic'] or 'no topic'} (members: {', '.join(store.members(conn, c['name']))})")
        if len(lines) == 2:
            lines.append("- none")
    return "\n".join(lines)


def create_channel(name: str, members: list[str], topic: str = "") -> str:
    """Create a group channel (also a Discord text channel) and add sessions to it.

    name: lowercase letters, digits and dashes. members: session names. You're added automatically.
    Each member is told it was added, who else is there, and how to post.
    """
    name = name.lstrip("#").lower()
    if not re.match(store.CHANNEL_RE, name) or name in store.RESERVED_CHANNELS:
        return "Pick another name: lowercase letters, digits and dashes, not fleet/needs-you/agent-chatter/thunderhead."
    with store.db() as conn:
        lead = _lead_only(conn)
        if store.get_channel(conn, name):
            return f"#{name} already exists. Use add_to_channel()."
        missing = _unknown(conn, members)
        if missing:
            return f"No sessions named {', '.join(missing)}. fleet() lists them."
        conn.execute("INSERT INTO channels (name, topic, created_by, created_at) VALUES (?, ?, ?, ?)",
                     (name, topic, lead["name"], store.now()))
        everyone = list(dict.fromkeys([lead["name"], *members]))
        conn.executemany("INSERT INTO channel_members (channel, session_name, added_at) VALUES (?, ?, ?)",
                         [(name, m, store.now()) for m in everyone])
        _notify(conn, lead, members,
                f"You've been added to the group channel #{name}"
                + (f" (topic: {topic})" if topic else "")
                + f". Members: {', '.join(everyone)}. Use post('{name}', ...) to talk to all of them.")
        store.post(conn, lead["id"], "channel_created", f"Members: {', '.join(everyone)}", channel=name)
    return f"Created #{name} with {', '.join(everyone)}."


def add_to_channel(channel: str, sessions: list[str]) -> str:
    """Add sessions to a group channel. Each is told it was added."""
    channel = channel.lstrip("#")
    with store.db() as conn:
        lead = _lead_only(conn)
        ch = store.get_channel(conn, channel)
        if ch is None or ch["closed"]:
            return f"No open channel #{channel}."
        missing = _unknown(conn, sessions)
        if missing:
            return f"No sessions named {', '.join(missing)}."
        current = store.members(conn, channel)
        new = [s for s in dict.fromkeys(sessions) if s not in current]
        conn.executemany("INSERT INTO channel_members (channel, session_name, added_at) VALUES (?, ?, ?)",
                         [(channel, m, store.now()) for m in new])
        everyone = current + new
        _notify(conn, lead, new, f"You've been added to #{channel}"
                + (f" (topic: {ch['topic']})" if ch["topic"] else "")
                + f". Members: {', '.join(everyone)}. Use post('{channel}', ...) to talk to all of them.")
        if new:
            store.post(conn, lead["id"], "channel_note", f"➕ Added {', '.join(new)}.", channel=channel)
    return f"Added {', '.join(new) or 'nobody new'} to #{channel}."


def remove_from_channel(channel: str, sessions: list[str]) -> str:
    """Remove sessions from a group channel. Each is told it was removed."""
    channel = channel.lstrip("#")
    with store.db() as conn:
        lead = _lead_only(conn)
        current = store.members(conn, channel)
        gone = [s for s in sessions if s in current]
        conn.executemany("DELETE FROM channel_members WHERE channel=? AND session_name=?",
                         [(channel, s) for s in gone])
        _notify(conn, lead, gone, f"You've been removed from #{channel}. Don't post there any more.")
        if gone:
            store.post(conn, lead["id"], "channel_note", f"➖ Removed {', '.join(gone)}.", channel=channel)
    return f"Removed {', '.join(gone) or 'nobody'} from #{channel}."


def close_channel(channel: str) -> str:
    """Close a group channel. Members are told; the Discord channel is kept read-only for the record."""
    channel = channel.lstrip("#")
    with store.db() as conn:
        lead = _lead_only(conn)
        ch = store.get_channel(conn, channel)
        if ch is None or ch["closed"]:
            return f"No open channel #{channel}."
        conn.execute("UPDATE channels SET closed=1 WHERE name=?", (channel,))
        _notify(conn, lead, store.members(conn, channel), f"#{channel} has been closed. Don't post there any more.")
        store.post(conn, lead["id"], "channel_closed", "", channel=channel)
    return f"Closed #{channel}."


def spawn(directory: str, task: str, name: str) -> str:
    """Start a new background session in `directory` working on `task`. It joins the fleet as `name`."""
    with store.db() as conn:
        _lead_only(conn)
        if store.session_by_name(conn, name) is not None:
            return f"The name '{name}' is taken. Pick another."
    if not launch.NAME_RE.match(name) or name == store.LEAD:
        return "Names can only use letters, digits, - and _ (and not 'thunderhead')."
    cwd = Path(directory).expanduser()
    if not cwd.is_dir():
        return f"{cwd} is not a directory."
    code, text = launch.run(launch.bg_command(name) + [task], cwd=cwd)
    return f"Spawned {name} in {cwd}." if code == 0 else f"Spawn failed:\n{text}"


def stop_session(name: str) -> str:
    """Stop a session. Its conversation is kept; a message from you or the human wakes it again."""
    with store.db() as conn:
        lead = _lead_only(conn)
        target = store.session_by_name(conn, name)
        if target is None or target["status"] in store.DEAD:
            return f"No running session named '{name}'."
        if target["id"] == lead["id"]:
            return "You can't stop yourself."
        store.set_status(conn, target["id"], "stopped")
    code, text = launch.run(["claude", "stop", target["id"][:8]])
    if code != 0 and not ("No job matching" in text and target["status"] == store.SLEEPING):
        with store.db() as conn:
            store.set_status(conn, target["id"], target["status"])
        return f"Couldn't stop {name}:\n{text}"
    with store.db() as conn:
        store.post(conn, target["id"], "stopped", "Stopped by The ThunderHead. Send a message here to start it again.")
    return f"Stopped {name}."


LEAD_TOOLS = (fleet, create_channel, add_to_channel, remove_from_channel, close_channel, spawn, stop_session)
if os.environ.get("THUNDERHEAD_ROLE") == "lead":
    for fn in LEAD_TOOLS:
        mcp.tool()(fn)


def main():
    mcp.run("stdio")
