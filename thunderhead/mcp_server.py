"""Per-session MCP server: the tools a Claude session uses to talk to the fleet.

Supervisors and The Thunderhead get extra tools on top. They're only registered
when this server runs inside a session with that role, and each one checks the
caller's place in the org chart again.
"""
import functools
import importlib
import json
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import config, hooks, launch, org
from . import db as store

mcp = MCPServer("thunderhead", instructions="Talk to the human (via Discord) and to other Claude sessions.")

STATES = ("working", "blocked", "done")
# Posts per channel per minute before post() refuses, so a busy channel can't flood everyone.
CHANNEL_RATE = 20
# A session loads this server once, when it starts, but the fleet's code changes while sessions run.
# Before each tool call, reload the modules the tools rely on if they changed on disk, so a
# long-running session (like The Thunderhead) never acts on stale logic. Changes to this file
# itself, such as a new tool or new parameters, still need the session to restart.
_RELOADABLE = (config, store, launch, hooks, org)  # in dependency order
_loaded = {m.__name__: Path(m.__file__).stat().st_mtime for m in _RELOADABLE}


def _refresh():
    changed = [m for m in _RELOADABLE if Path(m.__file__).stat().st_mtime != _loaded[m.__name__]]
    if changed:
        for m in _RELOADABLE:  # reload them all, in order, so each sees the others' new code
            importlib.reload(m)
            _loaded[m.__name__] = Path(m.__file__).stat().st_mtime


def tool(fn):
    @functools.wraps(fn)
    def fresh(*args, **kwargs):
        _refresh()
        return fn(*args, **kwargs)
    return mcp.tool()(fresh)


def hops_refusal() -> str:
    return (f"Refused: this chain of agent messages has gone {config.MAX_HOPS} hops without the human. "
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
    return None if hops > config.MAX_HOPS else hops


# --- every session ----------------------------------------------------------

@tool
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


@tool
def report(message: str) -> str:
    """Send a message to the human in your Discord thread: results, questions, or anything they should see."""
    with store.db() as conn:
        me = _me(conn)
        store.post(conn, me["id"], "report", message)
    return "Sent to the human."


@tool
def send(to: str, message: str, wake: bool | None = None) -> str:
    """Send a private message to another Claude session by name.

    A note: the session reads it the next time it wakes, and isn't woken for it. Use that for
    context and updates. A call: it gets the message when its current turn ends, or is woken if
    asleep. Use a call when it must act now: handing it a task, or a question you need answered.
    By default, messages up the chain (to your supervisor, or to The Thunderhead) are calls, since
    someone is usually waiting for them, and everything else is a note. wake=True or wake=False
    overrides that.
    """
    with store.db() as conn:
        me = _me(conn)
        target = store.session_by_name(conn, to)
        lead = _is_lead(me)
        if target is None or (target["status"] in store.DEAD and not lead):
            names = ", ".join(s["name"] for s in store.live_sessions(conn) if s["id"] != me["id"])
            why = "was stopped" if target and target["status"] == "stopped" else "isn't running"
            return (f"'{to}' {why}; only the human or The Thunderhead can restart it. "
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
            return hops_refusal()
        if wake is None:
            wake = store.is_up(conn, me["name"], to)
        store.queue_message(conn, target["id"], org._kind(conn, me["name"]), me["name"], message, hops=hops,
                            urgent=wake)
        kind = "call" if wake else "note"
        store.post(conn, me["id"], "agent_msg", f"📤 to **{target['name']}** (hop {hops}, {kind}): {message}")
        store.post(conn, target["id"], "agent_msg_in", f"📥 from **{me['name']}** (hop {hops}, {kind}): {message}")
    if not wake:
        return f"Left a note for {to}. It reads it the next time it wakes; use wake=True if it must act now."
    if target["status"] in store.DEAD + (store.SLEEPING,):
        with store.db() as conn:
            t = store.get_team(conn, team) if role == "dev" else None
            if t is not None and len(store.awake_devs(conn, team)) >= t["max_awake"]:
                return (f"Called {to}, but team {team} already has {t['max_awake']} devs busy, so it waits for a "
                        "slot and wakes when one frees up.")
        return f"Called {to}. It isn't running, so it's being woken to receive it (about 20 seconds)."
    return f"Called {to}. It gets this when its current turn ends."


@tool
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
            return hops_refusal()
        got = store.fan_out(conn, ch["name"], org._kind(conn, me["name"]), me["name"], message, notify, hops=hops)
        pinged = "everyone" if "all" in notify else ", ".join(f"@{n}" for n in got) or "nobody"
        store.post(conn, me["id"], "channel_post", f"→ {pinged}\n{message}", channel=ch["name"])
    return f"Posted to #{ch['name']}; notified {', '.join(got) or 'nobody'}."


@tool
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


@tool
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


@tool
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


@tool
def team() -> str:
    """Your team: supervisor, devs, their status, and the team channel."""
    with store.db() as conn:
        me = _me(conn)
        t = store.team_of(conn, me["name"])
        if t is None:
            return "You're not on a team."
        lines = [f"Team {t['name']}: {t['topic'] or ''} (repos: {', '.join(json.loads(t['repos']))})",
                 f"Settings: autonomy={t['autonomy']}, max_awake={t['max_awake']} "
                 f"({len(store.awake_devs(conn, t['name']))} awake now), max_model={t['max_model']}, "
                 f"charter {t['charter_status']}",
                 f"Team channel: #{t['name']}"]
        for name in store.team_members_of(conn, t["name"]):
            s = store.session_by_name(conn, name)
            label = "supervisor" if name == t["supervisor"] else f"dev, {store.get_model(conn, name)[0] or '?'}"
            lines.append(f"- {name} ({label}) [{s['status'] if s else 'not started'}] {(s and s['summary']) or ''}")
    return "\n".join(lines)


@tool
def history(target: str = "", hours: float = 24, limit: int = 60) -> str:
    """What happened: reports, status updates, messages sent, channel posts, starts and stops, and the
    human's messages, newest last. Use it to catch up after a wipe or restart, or to answer "what did
    X do this week?" from the record rather than memory.

    target: a session name or a team name. The Thunderhead may look at anything (empty means the whole
    fleet); a supervisor at its own team and devs; a dev at itself. hours: how far back. limit: at most
    this many entries.
    """
    with store.db() as conn:
        me = _me(conn)
        role, my_team = store.rank(conn, me["name"])
        if store.get_team(conn, target):
            names = store.team_members_of(conn, target)
        elif target:
            names = [target]
        else:
            names = ([r[0] for r in conn.execute("SELECT DISTINCT name FROM sessions")] if role == "lead"
                     else store.team_members_of(conn, my_team) if role == "supervisor" else [me["name"]])
        if role != "lead":
            allowed = set(store.team_members_of(conn, my_team)) if role == "supervisor" else {me["name"]}
            if not set(names) <= allowed:
                return ("You can see your own team's history." if role == "supervisor"
                        else "You can see your own history.")
        lines = org.history(conn, names, max(0.1, hours), max(1, min(limit, 300)))
    return "\n".join(lines) or f"Nothing recorded in the last {hours:g} hours."


@tool
def tasks(team: str = "", all: bool = False) -> str:
    """Your team's task board: open tasks with their owner, status and branch. all=True includes done
    and dropped ones. The Thunderhead may name any team; everyone else sees their own."""
    with store.db() as conn:
        me = _me(conn)
        role, my_team = store.rank(conn, me["name"])
        team = team if (team and role == "lead") else my_team
        if not team:
            return "You're not on a team." if role != "lead" else "Name a team."
        rows = org.team_tasks(conn, team, include_done=all)
    if not rows:
        return f"No {'' if all else 'open '}tasks for team {team}."
    return "\n".join(f"[{t['status']}] {org.task_line(t).replace('**', '')}" + (f" — {t['note']}" if t["note"] else "")
                     for t in rows)


@tool
def task_update(task_id: int, status: str = "", owner: str = "", branch: str = "", note: str = "") -> str:
    """Update a task on your team's board. status: todo, doing, review, blocked, done or dropped.

    Devs can update the tasks they own (status, branch, note): set 'doing' when you start, 'review'
    with the branch when it's ready, 'blocked' with a note if you're stuck. Supervisors can update any
    of their team's tasks, including the owner.
    """
    with store.db() as conn:
        me = _me(conn)
        role, my_team = store.rank(conn, me["name"])
        t = org.get_task(conn, task_id)
        if t is None or t["team"] != my_team:
            return f"No task #{task_id} on your team."
        if role == "dev" and t["owner"] != me["name"]:
            return f"Task #{task_id} isn't yours. Ask your supervisor."
        if status and status not in org.TASK_STATUSES:
            return f"status must be one of {', '.join(org.TASK_STATUSES)}."
        if owner and role != "supervisor":
            return "Only your supervisor can reassign a task."
        if owner and (store.rank(conn, owner)[1] != my_team or store.rank(conn, owner)[0] != "dev"):
            return f"'{owner}' isn't a dev on your team."
        org.update_task(conn, task_id, status=status or None, owner=owner or None, branch=branch or None,
                        note=note or None)
        if role == "dev" and status in ("review", "blocked"):
            sup = store.get_team(conn, my_team)["supervisor"]
            org.fyi(conn, sup, f"Task #{task_id} ({t['title']}) is now {status}"
                               + (f" on branch {branch or t['branch']}" if (branch or t["branch"]) else "")
                               + (f": {note}" if note else "."))
    return f"Task #{task_id} updated."


@tool
def inbox() -> str:
    """Check for new messages without waiting for your turn to end."""
    with store.db() as conn:
        me = _me(conn)
        rows = store.take_messages(conn, me["id"])
        if rows:
            conn.execute("UPDATE sessions SET current_hops=? WHERE id=?", (hooks._hops_after(rows), me["id"]))
        return hooks.delivery(conn, me["id"], rows) if rows else "No new messages."


# --- supervisors and The Thunderhead ----------------------------------------

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

    The Thunderhead can include anyone. A supervisor can create channels among its own team without
    asking (The Thunderhead is told); a channel with other teams' sessions needs request("channel", ...).
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


def archive_dev(name: str) -> str:
    """File away one of your devs whose work is done for now: it goes to sleep and its thread is archived
    (listed in #archived). It isn't lost: any message, from you or anyone, wakes it with its full context.
    Not for devs that are mid-task. Deleting a dev is the human's call; ask with request("other", ...).
    """
    with store.db() as conn:
        me, _, team = _require(conn, "supervisor")
        role, their_team = store.rank(conn, name)
        target = store.session_by_name(conn, name)
        if target is None or role != "dev" or their_team != team:
            return f"'{name}' isn't one of your devs. team() lists them."
        if target["status"] in ("working", "needs_you", "waking"):
            return f"'{name}' is {target['status']} right now. Let it finish first."
        if target["thread_id"] is None:
            return f"'{name}' has no thread to archive yet."
        store.post(conn, target["id"], "archive", me["name"])
    return f"Archiving '{name}'. It'll show in #archived, and a message wakes it again."


def propose_charter(text: str, summary: str) -> str:
    """Send your team's charter to the human for approval, once you've refined the draft.

    text: the full charter, keeping the draft's sections. summary: what you changed and why, in a line or two.
    Only the human can approve it; until then, keep working from the draft.
    """
    with store.db() as conn:
        me, _, _ = _require(conn, "supervisor")
        return org.propose_charter(conn, me, text, summary)


def spawn_dev(name: str, task: str, directory: str = "", model: str = "", effort: str = "") -> str:
    """Add a tool to your toolbox: a new dev session with its own context, for a kind of work.

    Name it for its specialty (billing-api, not dev2). task: what it's for and what to do first; it builds
    its context from this. directory: the product repo it works in, or leave it empty for a fresh
    workspace (never your own folder). model: "haiku", "sonnet" (default) or "opus", up to your team's
    max_model; for complex planning or architecture, ask for opus with request("model", ...). effort:
    "low" to "max", optional. No approval needed; your team's max_awake limits how many run at once.
    """
    with store.db() as conn:
        me, _, team = _require(conn, "supervisor")
        err, cmd, cwd = org.spawn_dev(conn, team, directory, task, name, model, effort, by=me["name"])
        if err:
            return err
        org.fyi(conn, store.LEAD, f"{me['name']} added '{name}' to team {team} ({model or config.DEFAULT_DEV_MODEL}): "
                                  f"{task[:300]}")
    code, text = launch.run(cmd, cwd=cwd)
    if code != 0:
        with store.db() as conn:
            org.undo_spawn(conn, team, name)
        return f"'{name}' didn't start:\n{text}"
    return f"'{name}' is starting. It'll report back to you with a call when it's done or stuck."


def set_model(dev: str, model: str = "", effort: str = "") -> str:
    """Change which model (and effort) one of your devs runs on, from its next wake-up.

    Match the model to the work: haiku for simple lookups, sonnet for most coding, opus for complex
    planning and architecture. Above your team's max_model, ask with request("model", ...). A switch
    makes the dev re-read its whole context once at full price, so choose for its role, not per task.
    """
    with store.db() as conn:
        me, _, team = _require(conn, "supervisor")
        role, their_team = store.rank(conn, dev)
        if role != "dev" or their_team != team:
            return f"'{dev}' isn't one of your devs."
        err = org.model_error(model or None, effort or None)
        if err:
            return err
        t = store.get_team(conn, team)
        if model and org.above(model, t["max_model"]):
            return (f"Your team's max_model is {t['max_model']}. Ask for {model} with "
                    f"request('model', {{'dev': '{dev}', 'model': '{model}'}}, reason).")
        store.set_model(conn, dev, model or None, effort or None)
        now_model, now_effort = store.get_model(conn, dev)
    return f"'{dev}' will run on {now_model}" + (f" at {now_effort} effort" if now_effort else "") + " from its next wake-up."


def task_add(title: str, detail: str = "", owner: str = "", call: bool = False) -> str:
    """Add a task to your team's board (a pinned message in your team channel the human can see).

    owner: the dev to give it to, if any. call=True also calls that dev with the task now; otherwise
    it's just recorded. Keep the board current: it's how the human sees what your team is doing.
    """
    with store.db() as conn:
        me, _, team = _require(conn, "supervisor")
        if owner:
            role, their_team = store.rank(conn, owner)
            if role != "dev" or their_team != team:
                return f"'{owner}' isn't one of your devs."
        task_id = org.add_task(conn, team, title, detail, owner, me["name"])
        if owner:
            org.update_task(conn, task_id, status="doing" if call else "todo")
            if call:
                target = store.session_by_name(conn, owner)
                store.queue_message(conn, target["id"], "supervisor", me["name"],
                                    f"Task #{task_id}: {title}\n\n{detail}\n\nUpdate it with task_update({task_id}, ...) "
                                    "as you go: status 'review' with the branch when it's ready.", hops=me["current_hops"])
                store.post(conn, me["id"], "agent_msg", f"📤 to **{owner}** (hop {me['current_hops']}, call): Task #{task_id}: {title}")
                store.post(conn, target["id"], "agent_msg_in", f"📥 from **{me['name']}** (hop {me['current_hops']}, call): Task #{task_id}: {title}")
    return f"Added task #{task_id}" + (f", given to {owner}" + (" and called" if call else "") if owner else "") + "."


def request(action: str, details: dict, reason: str, replaces: int = 0) -> str:
    """Ask The Thunderhead for something only it can grant. It approves, rejects, or asks the human.

    action: "model" (details: dev, model, effort) to run one of your devs above your team's max_model,
    for example opus for complex planning or architecture; "channel" (details: name, members, topic)
    for a channel with other teams' sessions; or "other" (details: anything). reason: why.
    replaces: the number of an earlier request of yours that this one supersedes; it's withdrawn.
    You don't need a request to spawn a dev: use spawn_dev().
    """
    with store.db() as conn:
        me, _, _ = _require(conn, "supervisor")
        return org.create_request(conn, me, action, details, reason, replaces)


def withdraw_request(request_id: int, reason: str) -> str:
    """Take back one of your requests that hasn't been decided yet (plans changed, it crossed with news)."""
    with store.db() as conn:
        me, _, _ = _require(conn, "supervisor")
        return org.withdraw_request(conn, me, request_id, reason)


# --- The Thunderhead only ---------------------------------------------------

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
                         f"repos: {', '.join(json.loads(t['repos']))}; autonomy={t['autonomy']}, "
                         f"max_awake={t['max_awake']}, max_model={t['max_model']}, charter {t['charter_status']})")
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


def create_team(name: str, charter: str, repos: list[str], topic: str = "", supervisor: str = "",
                autonomy: str = "", max_awake: int = -1, max_model: str = "") -> str:
    """Create a team for a project or domain and start its supervisor.

    charter: a first draft of the team's mandate: what it owns (and doesn't), goals, definition of
    done, constraints, interfaces with other teams, and anything the human specified. The supervisor
    refines it after exploring the code and proposes it to the human, who approves the final version.
    repos: the folders the team works in (the supervisor can read them). supervisor: its session name
    (default '<name>-sup'). The team gets a Discord category, a desk channel for talking to the
    supervisor, and a team channel.
    autonomy ("propose" or "act"), max_awake (devs awake at once, default 3) and max_model (the strongest
    model the supervisor may pick itself, default "sonnet"): set them here if the human said, rather
    than writing them into the charter. Giving more room than the defaults goes to the human's buttons.
    """
    with store.db() as conn:
        _lead(conn)
        err, sup = org.create_team(conn, name, charter, repos, topic, supervisor or None)
        if err:
            return err
        t = store.get_team(conn, name.lower())
        settings_note = ""
        if autonomy or max_awake >= 0 or max_model:
            lead = _lead(conn)
            settings_note = " " + org.request_config(conn, lead, t["name"], {
                "autonomy": autonomy or None, "max_awake": max_awake if max_awake >= 0 else None,
                "max_model": max_model or None}, "set when the team was created")
            t = store.get_team(conn, t["name"])
        cmd, cwd = launch.supervisor_command(t)
    code, text = launch.run(cmd, cwd=cwd)
    if code != 0:
        return f"Team registered, but the supervisor didn't start:\n{text}"
    return f"Team '{t['name']}' created. Its supervisor '{sup}' is starting and will introduce itself.{settings_note}"


def spawn_oneoff(directory: str, task: str, name: str = "", model: str = "") -> str:
    """Start a one-off session outside any team for a small, self-contained job that no team owns and
    that won't need follow-up. directory: the folder it works in, or "" for a fresh empty workspace.
    model: "haiku", "sonnet" (default) or "opus"; pick the cheapest that can do the job. It reports its result to you and is deleted automatically once done
    (its thread stays as the record). At most a couple can run at once. Anything that belongs to a
    team's product, or is ongoing, goes to that team's supervisor instead.
    """
    with store.db() as conn:
        lead = _lead(conn)
        err, cmd, cwd, name = org.spawn_oneoff(conn, lead, directory, task, name, model)
        if err:
            return err
    code, text = launch.run(cmd, cwd=cwd)
    if code != 0:
        with store.db() as conn:
            conn.execute("DELETE FROM oneoffs WHERE name=?", (name,))
        return f"The one-off didn't start:\n{text}"
    return f"Started one-off '{name}'. It will send you its result, then be deleted."


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


def set_team_config(team: str, reason: str, autonomy: str = "", max_awake: int = -1, max_model: str = "") -> str:
    """Change a team's settings, as the human's instructions call for.

    autonomy: "propose" (only works on what it's given, proposes what's next) or "act" (picks up its
    own backlog). max_awake: how many of its devs may be awake at once (it may keep any number).
    max_model: the strongest model ("haiku", "sonnet", "opus") its supervisor may give a dev without
    asking. Tightening applies at once. Loosening (act, more awake, a stronger model) goes to the
    human's Approve/Reject buttons: quote their words in reason if they asked for it.
    """
    with store.db() as conn:
        lead = _lead(conn)
        return org.request_config(conn, lead, team, {"autonomy": autonomy or None,
                                                    "max_awake": max_awake if max_awake >= 0 else None,
                                                    "max_model": max_model or None}, reason)


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
        store.post(conn, target["id"], "stopped", f"🚨 Emergency stop by The Thunderhead: {reason}")
        store.post(conn, lead["id"], "report", f"🚨 I emergency-stopped **{session}**: {reason}")
        team = store.team_of(conn, session)
        if team is not None and team["supervisor"] != session:
            org.notify(conn, lead["name"], [team["supervisor"]],
                       f"I emergency-stopped your dev '{session}': {reason}. Decide what it should do next "
                       "before messaging it again (a message wakes it).")
    return f"Stopped {session}. The human and its supervisor have been told."


SUPERVISOR_TOOLS = (spawn_dev, set_model, task_add, create_channel, add_to_channel, request, withdraw_request, archive_dev,
                    propose_charter)
LEAD_TOOLS = (fleet, create_team, spawn_oneoff, join_team, set_team_config, requests, approve_request, reject_request, escalate_request,
              create_channel, add_to_channel, remove_from_channel, close_channel, emergency_stop)
for fn in {"lead": LEAD_TOOLS, "supervisor": SUPERVISOR_TOOLS}.get(os.environ.get("THUNDERHEAD_ROLE"), ()):
    tool(fn)


def main():
    mcp.run("stdio")
