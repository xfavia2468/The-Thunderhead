"""Per-session MCP server: the tools a Claude session uses to talk to the fleet.

Supervisors and The ThunderHead get extra tools on top. They're only registered
when this server runs inside a session with that role, and each one checks the
caller's place in the org chart again.
"""
import json
import os

from mcp.server.mcpserver import MCPServer

from . import db as store
from . import launch, org
from .config import MAX_HOPS
from .hooks import _hops_after, delivery

mcp = MCPServer("thunderhead", instructions="Talk to the human (via Discord) and to other Claude sessions.")

STATES = ("working", "blocked", "done")
# Posts per channel per minute before post() refuses, so a busy channel can't flood everyone.
CHANNEL_RATE = 20
HOPS_REFUSAL = (f"Refused: this chain of agent messages has gone {MAX_HOPS} hops without the human. "
                "Use report() to ask the human how to proceed.")


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


def _hops(conn, me, recipients) -> int | None:
    """Hop count for a message. Going down the org chart is free (delegation can't loop: the
    chart has a bottom); going up or sideways counts, so back-and-forth is still capped."""
    if recipients and all(store.is_down(conn, me["name"], r) for r in recipients):
        return me["current_hops"]
    hops = me["current_hops"] + 1
    return None if hops > MAX_HOPS else hops


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
            why = "was stopped" if target and target["status"] == "stopped" else "isn't running"
            return (f"'{to}' {why}; only the human or The ThunderHead can restart it. "
                    f"Sessions you can message: {names or 'none'}.")
        if target["id"] == me["id"]:
            return "That is you."
        role, team = store.rank(conn, to)
        if lead and role == "dev":
            sup = store.get_team(conn, team)["supervisor"]
            return (f"'{to}' is a dev on team '{team}'. Route work through its supervisor: send('{sup}', ...). "
                    "In an emergency, use emergency_stop().")
        hops = _hops(conn, me, [to])
        if hops is None:
            return HOPS_REFUSAL
        store.queue_message(conn, target["id"], org._kind(conn, me["name"]), me["name"], message, hops=hops)
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
        roster = store.members(conn, ch["name"])
        if me["name"] not in roster and not _is_lead(me):
            return f"You aren't a member of #{ch['name']}."
        unknown = [n for n in notify if n != "all" and n not in roster]
        if not notify or unknown:
            return (f"notify must name members of #{ch['name']} ({', '.join(roster)}) or be [\"all\"]"
                    + (f"; not members: {', '.join(unknown)}" if unknown else "") + ".")
        if store.recent_posts(conn, ch["name"]) >= CHANNEL_RATE:
            return f"#{ch['name']} is busy ({CHANNEL_RATE} posts in the last minute). Wait, then try again."
        targets = [m for m in roster if m != me["name"]] if "all" in notify else notify
        hops = _hops(conn, me, targets)
        if hops is None:
            return HOPS_REFUSAL
        got = store.fan_out(conn, ch["name"], org._kind(conn, me["name"]), me["name"], message, notify, hops=hops)
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
    """List the group channels you're in, with their topic, members and unread count."""
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
    """List the other sessions in the fleet with their team, status and what they are working on."""
    with store.db() as conn:
        me = _me(conn)
        lines = []
        for s in store.live_sessions(conn):
            if s["id"] == me["id"]:
                continue
            role, team = store.rank(conn, s["name"])
            where = f"{role} of {team}" if team else role
            lines.append(f"- {s['name']} ({where}) [{s['status']}] {s['summary'] or ''}")
    return "\n".join(lines) or "No other live sessions."


@mcp.tool()
def team() -> str:
    """Your team: supervisor, devs, their status, and the team channel."""
    with store.db() as conn:
        me = _me(conn)
        t = store.team_of(conn, me["name"])
        if t is None:
            return "You're not on a team."
        lines = [f"Team {t['name']}: {t['topic'] or ''} (repos: {', '.join(json.loads(t['repos']))})",
                 f"Team channel: #{t['name']}"]
        for name in store.team_members_of(conn, t["name"]):
            s = store.session_by_name(conn, name)
            label = "supervisor" if name == t["supervisor"] else "dev"
            lines.append(f"- {name} ({label}) [{s['status'] if s else 'not started'}] {(s and s['summary']) or ''}")
    return "\n".join(lines)


@mcp.tool()
def inbox() -> str:
    """Check for new messages without waiting for your turn to end."""
    with store.db() as conn:
        me = _me(conn)
        rows = store.take_messages(conn, me["id"])
        if rows:
            conn.execute("UPDATE sessions SET current_hops=? WHERE id=?", (_hops_after(rows), me["id"]))
        return delivery(conn, me["id"], rows) if rows else "No new messages."


# --- supervisors and The ThunderHead ----------------------------------------

def _require(conn, *roles):
    me = _me(conn)
    role, team = store.rank(conn, me["name"])
    if role not in roles or (role == "lead" and not _is_lead(me)):
        raise PermissionError(f"Only {' or '.join(roles)} sessions can do that.")
    return me, role, team


def _outside_team(conn, names, team) -> list[str]:
    return [n for n in names if (store.team_of(conn, n) or {"name": None})["name"] != team]


def create_channel(name: str, members: list[str], topic: str = "") -> str:
    """Create a group channel (also a Discord text channel) and add sessions to it. You're added too.

    The ThunderHead can include anyone. A supervisor can create channels among its own team without
    asking (The ThunderHead is told); a channel with other teams' sessions needs request("channel", ...).
    """
    with store.db() as conn:
        me, role, team = _require(conn, "lead", "supervisor")
        if role == "supervisor":
            outside = _outside_team(conn, members, team)
            if outside:
                return (f"{', '.join(outside)} aren't on your team. For a cross-team channel, use "
                        "request('channel', {name, members, topic}, reason).")
        result = org.create_channel(conn, me["name"], name, members, topic,
                                    team=team if role == "supervisor" else None)
        if role == "supervisor" and result.startswith("Created"):
            org.fyi(conn, store.LEAD, f"Supervisor {me['name']} created team channel #{name.lower()} "
                                      f"with {', '.join(members)}" + (f": {topic}" if topic else "") + ".")
    return result


def add_to_channel(channel: str, sessions: list[str]) -> str:
    """Add sessions to a group channel. Each is told. Supervisors can only do this for their team's channels."""
    channel = channel.lstrip("#")
    with store.db() as conn:
        me, role, team = _require(conn, "lead", "supervisor")
        ch = store.get_channel(conn, channel)
        if ch is None or ch["closed"]:
            return f"No open channel #{channel}."
        if role == "supervisor" and (ch["team"] != team or _outside_team(conn, sessions, team)):
            return "Supervisors can only add their own devs to their own team's channels. Use request() otherwise."
        missing = org.unknown_sessions(conn, sessions)
        if missing:
            return f"No sessions named {', '.join(missing)}."
        new = org.add_to_channel(conn, me["name"], channel, sessions)
    return f"Added {', '.join(new) or 'nobody new'} to #{channel}."


def request(action: str, details: dict, reason: str) -> str:
    """Ask The ThunderHead to do something only it can do. It approves, rejects, or asks the human.

    action: "spawn" (details: directory, task, name) for a new dev on your team, "channel"
    (details: name, members, topic) for a channel with other teams' sessions, or "other"
    (details: anything) for everything else. reason: why the team needs it.
    """
    with store.db() as conn:
        me, _, _ = _require(conn, "supervisor")
        return org.create_request(conn, me, action, details, reason)


# --- The ThunderHead only ---------------------------------------------------

def _lead(conn):
    return _require(conn, "lead")[0]


def fleet() -> str:
    """Everything at a glance: teams with their sessions, sessions without a team, channels and open requests."""
    with store.db() as conn:
        _lead(conn)

        def line(name):
            s = store.session_by_name(conn, name)
            if s is None:
                return f"  - {name} [not started]"
            mail = store.pending_count(conn, s["id"])
            return f"  - {name} [{s['status']}] {s['summary'] or ''}" + (f" ({mail} queued)" if mail else "")

        lines, teamed = ["Teams:"], set()
        for t in store.all_teams(conn):
            members = store.team_members_of(conn, t["name"])
            teamed.update(members)
            lines.append(f"- {t['name']}: {t['topic'] or ''} (supervisor {t['supervisor']}; "
                         f"repos: {', '.join(json.loads(t['repos']))})")
            lines += [line(m) + (" (supervisor)" if m == t["supervisor"] else "") for m in members]
        if len(lines) == 1:
            lines.append("- none")
        loose = [s for s in store.live_sessions(conn) if s["name"] not in teamed and s["name"] != store.LEAD]
        lines.append("Sessions without a team:")
        lines += [line(s["name"]) for s in loose] or ["  - none"]
        lines.append("Channels:")
        lines += [f"- #{c['name']}: {c['topic'] or ''} (members: {', '.join(store.members(conn, c['name']))})"
                  for c in store.open_channels(conn)] or ["- none"]
        open_reqs = conn.execute("SELECT * FROM requests WHERE status IN ('pending','escalated')").fetchall()
        lines.append("Open requests:")
        lines += [org.describe(r) + f" [{r['status']}]" for r in open_reqs] or ["- none"]
    return "\n".join(lines)


def create_team(name: str, charter: str, repos: list[str], topic: str = "", supervisor: str = "") -> str:
    """Create a team for a project or domain and start its supervisor.

    charter: what the team owns, its goals and anything the human specified. The supervisor keeps it.
    repos: the folders the team works in (the supervisor can read them). supervisor: its session name
    (default '<name>-sup'). The team gets a Discord category, a desk channel for talking to the
    supervisor, and a team channel.
    """
    with store.db() as conn:
        _lead(conn)
        err, sup = org.create_team(conn, name, charter, repos, topic, supervisor or None)
        if err:
            return err
        t = store.get_team(conn, name.lower())
        cmd, cwd = launch.supervisor_command(t["name"], sup, json.loads(t["repos"]))
    code, text = launch.run(cmd, cwd=cwd)
    if code != 0:
        return f"Team registered, but the supervisor didn't start:\n{text}"
    return f"Team '{t['name']}' created. Its supervisor '{sup}' is starting and will introduce itself."


def join_team(session: str, team: str) -> str:
    """Put an existing session without a team onto a team as a dev. It and its supervisor are told."""
    with store.db() as conn:
        _lead(conn)
        if store.get_team(conn, team) is None:
            return f"No team '{team}'."
        if store.session_by_name(conn, session) is None:
            return f"No session named '{session}'."
        current = store.team_of(conn, session)
        if current is not None:
            return f"'{session}' is already on team '{current['name']}'."
        org.join_team(conn, team, session)
    return f"'{session}' joined team '{team}'."


def requests() -> str:
    """Open requests from supervisors, waiting for your decision or the human's."""
    with store.db() as conn:
        _lead(conn)
        rows = conn.execute("SELECT * FROM requests WHERE status IN ('pending','escalated') ORDER BY id").fetchall()
    return "\n\n".join(org.describe(r) + f"\n[{r['status']}]" for r in rows) or "No open requests."


def approve_request(request_id: int, note: str = "") -> str:
    """Approve a supervisor's request. It's carried out as asked (a spawn starts the dev on that team)."""
    with store.db() as conn:
        _lead(conn)
        msg, cmd, cwd = org.decide(conn, request_id, True, note, by=store.LEAD)
    if cmd:
        code, text = launch.run(cmd, cwd=cwd)
        if code != 0:
            return f"{msg} But the session didn't start:\n{text}"
    return msg


def reject_request(request_id: int, reason: str) -> str:
    """Reject a supervisor's request, saying why."""
    with store.db() as conn:
        _lead(conn)
        return org.decide(conn, request_id, False, reason, by=store.LEAD)[0]


def escalate_request(request_id: int, note: str) -> str:
    """Hand a request to the human with Approve/Reject buttons, with your note on it."""
    with store.db() as conn:
        lead = _lead(conn)
        req = org.get_request(conn, request_id)
        if req is None or req["status"] != "pending":
            return f"No pending request #{request_id}."
        conn.execute("UPDATE requests SET status='escalated', note=? WHERE id=?", (note, request_id))
        store.post(conn, lead["id"], "request_escalated", str(request_id))
    return f"Request #{request_id} is with the human now."


def remove_from_channel(channel: str, sessions: list[str]) -> str:
    """Remove sessions from a group channel. Each is told it was removed."""
    channel = channel.lstrip("#")
    with store.db() as conn:
        lead = _lead(conn)
        gone = [s for s in sessions if s in store.members(conn, channel)]
        conn.executemany("DELETE FROM channel_members WHERE channel=? AND session_name=?",
                         [(channel, s) for s in gone])
        org.notify(conn, lead["name"], gone, f"You've been removed from #{channel}. Don't post there any more.")
        if gone:
            store.post(conn, lead["id"], "channel_note", f"➖ Removed {', '.join(gone)}.", channel=channel)
    return f"Removed {', '.join(gone) or 'nobody'} from #{channel}."


def close_channel(channel: str) -> str:
    """Close a group channel. Members are told; the Discord channel is kept read-only for the record."""
    channel = channel.lstrip("#")
    with store.db() as conn:
        lead = _lead(conn)
        ch = store.get_channel(conn, channel)
        if ch is None or ch["closed"]:
            return f"No open channel #{channel}."
        if store.get_team(conn, channel):
            return f"#{channel} is a team's own channel; it stays open while the team exists."
        conn.execute("UPDATE channels SET closed=1 WHERE name=?", (channel,))
        org.notify(conn, lead["name"], store.members(conn, channel),
                   f"#{channel} has been closed. Don't post there any more.")
        store.post(conn, lead["id"], "channel_closed", "", channel=channel)
    return f"Closed #{channel}."


def emergency_stop(session: str, reason: str) -> str:
    """Last resort: stop any session right now. Only when something is actively going wrong (a runaway
    loop, burning tokens, doing damage) and its supervisor can't handle it. The human and the
    session's supervisor are told. A message later wakes it again."""
    if not reason.strip():
        return "Give the reason. The human and the supervisor will see it."
    with store.db() as conn:
        lead = _lead(conn)
        target = store.session_by_name(conn, session)
        if target is None or target["status"] in store.DEAD:
            return f"No running session named '{session}'."
        if target["id"] == lead["id"]:
            return "You can't stop yourself."
        store.set_status(conn, target["id"], "stopped")
    code, text = launch.run(["claude", "stop", target["id"][:8]])
    if code != 0 and not ("No job matching" in text and target["status"] == store.SLEEPING):
        with store.db() as conn:
            store.set_status(conn, target["id"], target["status"])
        return f"Couldn't stop {session}:\n{text}"
    with store.db() as conn:
        store.post(conn, target["id"], "stopped", f"🚨 Emergency stop by The ThunderHead: {reason}")
        store.post(conn, lead["id"], "report", f"🚨 I emergency-stopped **{session}**: {reason}")
        team = store.team_of(conn, session)
        if team is not None and team["supervisor"] != session:
            org.notify(conn, lead["name"], [team["supervisor"]],
                       f"I emergency-stopped your dev '{session}': {reason}. Decide what it should do next "
                       "before messaging it again (a message wakes it).")
    return f"Stopped {session}. The human and its supervisor have been told."


SUPERVISOR_TOOLS = (create_channel, add_to_channel, request)
LEAD_TOOLS = (fleet, create_team, join_team, requests, approve_request, reject_request, escalate_request,
              create_channel, add_to_channel, remove_from_channel, close_channel, emergency_stop)
for fn in {"lead": LEAD_TOOLS, "supervisor": SUPERVISOR_TOOLS}.get(os.environ.get("THUNDERHEAD_ROLE"), ()):
    mcp.tool()(fn)


def main():
    mcp.run("stdio")
