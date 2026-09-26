"""The org chart: teams, group channels and requests.

Shared by the MCP tools and the bot, so an approved request does the same thing
whether The ThunderHead approves it or the human does.
"""
import json
import re
from pathlib import Path

from . import db as store
from . import launch

REQUEST_ACTIONS = ("spawn", "channel", "other")


def _kind(conn, name) -> str:
    role, _ = store.rank(conn, name)
    return {"lead": "lead", "supervisor": "supervisor"}.get(role, "agent")


def notify(conn, from_name, to_names, text, urgent=True):
    """A direct note from one session (or 'human') to others."""
    kind = "human" if from_name == "human" else _kind(conn, from_name)
    for name in to_names:
        target = store.session_by_name(conn, name)
        if target is not None and name != from_name:
            store.queue_message(conn, target["id"], kind, from_name, text, hops=1, urgent=urgent)


def fyi(conn, to_name, text):
    """Tell a session something it should know, without waking it for it."""
    target = store.session_by_name(conn, to_name)
    if target is not None:
        store.queue_message(conn, target["id"], "fyi", "thunderhead-system", text, urgent=False)


def unknown_sessions(conn, names) -> list[str]:
    return [n for n in names if store.session_by_name(conn, n) is None]


# --- channels ---------------------------------------------------------------

def check_channel_name(name: str) -> str | None:
    if not re.match(store.CHANNEL_RE, name) or name in store.RESERVED_CHANNELS:
        return ("Pick another name: lowercase letters, digits and dashes, and not one of "
                f"{', '.join(store.RESERVED_CHANNELS)}.")
    return None


def create_channel(conn, creator: str, name: str, members: list[str], topic: str = "",
                   team: str | None = None) -> str:
    """Create a group channel. The creator joins it; members are told."""
    name = name.lstrip("#").lower()
    err = check_channel_name(name)
    if err:
        return err
    if store.get_channel(conn, name) or store.get_team(conn, name):
        return f"#{name} is taken."
    missing = unknown_sessions(conn, members)
    if missing:
        return f"No sessions named {', '.join(missing)}."
    everyone = list(dict.fromkeys([creator, *members]))
    conn.execute("INSERT INTO channels (name, topic, created_by, created_at, team) VALUES (?, ?, ?, ?, ?)",
                 (name, topic, creator, store.now(), team))
    conn.executemany("INSERT INTO channel_members (channel, session_name, added_at) VALUES (?, ?, ?)",
                     [(name, m, store.now()) for m in everyone])
    notify(conn, creator, members,
           f"You've been added to the group channel #{name}" + (f" (topic: {topic})" if topic else "")
           + f". Members: {', '.join(everyone)}. Use post('{name}', message, notify=[...]) to reach them.")
    creator_row = store.session_by_name(conn, creator)
    store.post(conn, creator_row["id"] if creator_row else None, "channel_created",
               f"Created by {creator}. Members: {', '.join(everyone)}", channel=name)
    return f"Created #{name} with {', '.join(everyone)}."


def add_to_channel(conn, by: str, channel: str, sessions: list[str]) -> list[str]:
    ch = store.get_channel(conn, channel)
    current = store.members(conn, channel)
    new = [s for s in dict.fromkeys(sessions) if s not in current]
    conn.executemany("INSERT INTO channel_members (channel, session_name, added_at) VALUES (?, ?, ?)",
                     [(channel, m, store.now()) for m in new])
    notify(conn, by, new, f"You've been added to #{channel}" + (f" (topic: {ch['topic']})" if ch["topic"] else "")
           + f". Members: {', '.join(current + new)}. Use post('{channel}', message, notify=[...]) to reach them.")
    if new:
        by_row = store.session_by_name(conn, by)
        store.post(conn, by_row["id"] if by_row else None, "channel_note", f"➕ Added {', '.join(new)}.",
                   channel=channel)
    return new


# --- teams ------------------------------------------------------------------

def create_team(conn, name: str, charter: str, repos: list[str], topic: str = "",
                supervisor: str | None = None) -> tuple[str | None, str]:
    """Register a team with its memory folder, channel and supervisor.

    Returns (error, supervisor name). The caller spawns the supervisor with
    launch.supervisor_command() once this has committed.
    """
    name = name.lower()
    err = check_channel_name(name)
    if err:
        return err, ""
    if store.get_team(conn, name) or store.get_channel(conn, name):
        return f"'{name}' is taken.", ""
    folders = [str(Path(r).expanduser().resolve()) for r in repos]
    missing = [f for f in folders if not Path(f).is_dir()]
    if not folders or missing:
        return f"Give at least one existing folder; not found: {', '.join(missing) or 'none given'}.", ""
    sup = supervisor or f"{name}-sup"
    if not launch.NAME_RE.match(sup) or store.session_by_name(conn, sup) or sup == store.LEAD:
        return f"The supervisor name '{sup}' is taken or invalid.", ""
    conn.execute("INSERT INTO teams (name, topic, repos, supervisor, created_at) VALUES (?, ?, ?, ?, ?)",
                 (name, topic, json.dumps(folders), sup, store.now()))
    store.add_team_member(conn, name, sup)
    # The team's own channel, for the supervisor and devs. No ThunderHead: it talks to the supervisor.
    conn.execute("INSERT INTO channels (name, topic, created_by, created_at, team) VALUES (?, ?, ?, ?, ?)",
                 (name, topic or f"Team {name}", store.LEAD, store.now(), name))
    conn.execute("INSERT INTO channel_members (channel, session_name, added_at) VALUES (?, ?, ?)",
                 (name, sup, store.now()))
    launch.prepare_supervisor(name, charter)
    lead = store.session_by_name(conn, store.LEAD)
    store.post(conn, lead["id"] if lead else None, "team_created", sup, channel=name)
    return None, sup


def join_team(conn, team: str, session_name: str):
    """Put a session on a team as a dev, in the team channel, and tell it and its supervisor."""
    t = store.get_team(conn, team)
    store.add_team_member(conn, team, session_name)
    if session_name not in store.members(conn, team):
        conn.execute("INSERT INTO channel_members (channel, session_name, added_at) VALUES (?, ?, ?)",
                     (team, session_name, store.now()))
    notify(conn, store.LEAD, [session_name],
           f"You're now on team '{team}'. Your supervisor is '{t['supervisor']}': take work from it and report "
           f"to it. Team channel: #{team}.")
    fyi(conn, t["supervisor"], f"'{session_name}' has joined your team as a dev.")


def spawn_dev(conn, team: str, directory: str, task: str, name: str) -> tuple[str | None, list[str], str]:
    """Validate and register a new dev. Returns (error, command, folder); the caller runs the command."""
    if not launch.NAME_RE.match(name) or name == store.LEAD:
        return "Names can only use letters, digits, - and _ (and not 'thunderhead').", [], ""
    if store.session_by_name(conn, name) is not None:
        return f"The name '{name}' is taken.", [], ""
    cwd = Path(directory).expanduser()
    if not cwd.is_dir():
        return f"{cwd} is not a directory.", [], ""
    store.add_team_member(conn, team, name)
    conn.execute("INSERT OR IGNORE INTO channel_members (channel, session_name, added_at) VALUES (?, ?, ?)",
                 (team, name, store.now()))
    sup = store.get_team(conn, team)["supervisor"]
    brief = (f"[thunderhead] You're a dev on team '{team}'. Your supervisor is '{sup}': it gives you work, and "
             f"you report back to it with send('{sup}', ...) when you finish or get stuck. Team channel: #{team}.\n\n"
             f"Your task:\n{task}")
    return None, launch.bg_command(name) + [brief], str(cwd)


def undo_spawn(conn, team: str, name: str):
    conn.execute("DELETE FROM team_members WHERE team=? AND session_name=?", (team, name))
    conn.execute("DELETE FROM channel_members WHERE channel=? AND session_name=?", (team, name))


# --- requests ---------------------------------------------------------------

def describe(req) -> str:
    params = json.loads(req["params"])
    body = ", ".join(f"{k}={v!r}" for k, v in params.items())
    return f"#{req['id']} from {req['from_name']} (team {req['team']}): {req['action']}({body})\nWhy: {req['reason']}"


def create_request(conn, me, action: str, params: dict, reason: str) -> str:
    team = store.team_of(conn, me["name"])
    if team is None or team["supervisor"] != me["name"]:
        return "Only a team's supervisor can make requests."
    if action not in REQUEST_ACTIONS:
        return f"action must be one of {', '.join(REQUEST_ACTIONS)}."
    if not reason.strip():
        return "Say why: The ThunderHead needs a reason to approve it."
    need = {"spawn": ("directory", "task", "name"), "channel": ("name", "members")}.get(action, ())
    missing = [k for k in need if not params.get(k)]
    if missing:
        return f"A {action} request needs: {', '.join(missing)}."
    cur = conn.execute("INSERT INTO requests (from_name, team, action, params, reason, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (me["name"], team["name"], action, json.dumps(params), reason, store.now()))
    req = conn.execute("SELECT * FROM requests WHERE id=?", (cur.lastrowid,)).fetchone()
    lead = store.session_by_name(conn, store.LEAD)
    if lead is not None:
        store.queue_message(conn, lead["id"], "supervisor", me["name"],
                            f"New request {describe(req)}\n\nDecide with approve_request({req['id']}), "
                            f"reject_request({req['id']}, why) or escalate_request({req['id']}, note).", hops=1)
    return f"Request #{req['id']} sent to The ThunderHead. You'll hear back when it's decided."


def get_request(conn, req_id):
    return conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()


def decide(conn, req_id: int, approve: bool, note: str, by: str) -> tuple[str, list[str] | None, str]:
    """Record a decision. Returns (message, command to run or None, folder).

    Approving a spawn returns the command so the caller can run it outside the transaction.
    """
    req = get_request(conn, req_id)
    if req is None or req["status"] not in ("pending", "escalated"):
        return f"No open request #{req_id}.", None, ""
    params = json.loads(req["params"])
    cmd, cwd, result = None, "", ""
    if approve:
        if req["action"] == "spawn":
            err, cmd, cwd = spawn_dev(conn, req["team"], params["directory"], params["task"], params["name"])
            if err:
                approve, note = False, f"{note} (couldn't do it: {err})".strip()
        elif req["action"] == "channel":
            result = create_channel(conn, req["from_name"], params["name"], params.get("members", []),
                                    params.get("topic", ""))
            if not result.startswith("Created"):
                approve, note = False, f"{note} (couldn't do it: {result})".strip()
    status = "approved" if approve else "rejected"
    conn.execute("UPDATE requests SET status=?, note=?, decided_at=? WHERE id=?",
                 (status, note, store.now(), req_id))
    who = "the human" if by == "human" else "The ThunderHead"
    outcome = f"Your request #{req_id} ({req['action']}) was {status} by {who}."
    if approve and req["action"] == "spawn":
        outcome += f" '{params['name']}' is starting and will report to you."
    if note:
        outcome += f"\nNote: {note}"
    notify(conn, store.LEAD if by != "human" else "human", [req["from_name"]], outcome)
    if by == "human":
        fyi(conn, store.LEAD, f"The human {status} request #{req_id} from {req['from_name']}.")
    return f"Request #{req_id} {status}. {result}".strip(), cmd, cwd
