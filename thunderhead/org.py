"""The org chart: teams, group channels and requests.

Shared by the MCP tools and the bot, so an approved request does the same thing
whether The Thunderhead approves it or the human does.
"""
import json
import re
from pathlib import Path

from . import db as store
from . import launch
import secrets

from .config import DEFAULT_DEV_MODEL, EFFORTS, MAX_ONEOFFS, MAX_SESSIONS, MODELS

# What supervisors can ask The Thunderhead for: a model above their team's max_model for one
# dev, a channel with other teams, or anything else. 'charter' and 'config' only the human
# approves. Spawning needs no request: the team's max_awake limits what it can cost.
REQUEST_ACTIONS = ("model", "channel", "other")


def model_error(model: str | None, effort: str | None = None) -> str | None:
    if model and model not in MODELS:
        return f"model must be one of {', '.join(MODELS)}."
    if effort and effort not in EFFORTS:
        return f"effort must be one of {', '.join(EFFORTS)}."
    return None


def above(model: str, ceiling: str) -> bool:
    return MODELS.index(model) > MODELS.index(ceiling)
AUTONOMY = ("propose", "act")


def ceiling_error(conn, adding=1) -> str | None:
    if store.running_count(conn) + adding > MAX_SESSIONS:
        return (f"The fleet is at its ceiling of {MAX_SESSIONS} running sessions "
                "(THUNDERHEAD_MAX_SESSIONS, set by the human). Let some sleep or stop first.")
    return None


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
    err = ceiling_error(conn)
    if err:
        return err, ""
    conn.execute("INSERT INTO teams (name, topic, repos, supervisor, created_at) VALUES (?, ?, ?, ?, ?)",
                 (name, topic, json.dumps(folders), sup, store.now()))
    store.add_team_member(conn, name, sup)
    # The team's own channel, for the supervisor and devs. No Thunderhead: it talks to the supervisor.
    conn.execute("INSERT INTO channels (name, topic, created_by, created_at, team) VALUES (?, ?, ?, ?, ?)",
                 (name, topic or f"Team {name}", store.LEAD, store.now(), name))
    conn.execute("INSERT INTO channel_members (channel, session_name, added_at) VALUES (?, ?, ?)",
                 (name, sup, store.now()))
    if "## " not in charter:
        # Give the supervisor the template's sections to fill in as it refines the draft.
        charter = f"## Summary from The Thunderhead\n\n{charter.strip()}\n\n{launch.CHARTER_TEMPLATE}"
    launch.write_charter(name, charter)
    launch.prepare_supervisor(store.get_team(conn, name))
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


def spawn_dev(conn, team: str, directory: str, task: str, name: str, model: str = "", effort: str = "",
              by: str = "") -> tuple[str | None, list[str], str]:
    """Validate and register a new dev. Returns (error, command, folder); the caller runs the command.

    An empty directory gives the dev a fresh workspace of its own. by="human" may pick any model;
    anyone else is held to the team's max_model.
    """
    model = model or DEFAULT_DEV_MODEL
    err = model_error(model, effort)
    if err:
        return err, [], ""
    if not launch.NAME_RE.match(name) or name == store.LEAD:
        return "Names can only use letters, digits, - and _ (and not 'thunderhead').", [], ""
    if store.session_by_name(conn, name) is not None:
        return f"The name '{name}' is taken.", [], ""
    if directory:
        cwd = Path(directory).expanduser()
        err = launch.forbidden_dir(cwd)
        if err:
            return err, [], ""
        if not cwd.is_dir():
            return f"{cwd} is not a directory.", [], ""
    else:
        cwd = launch.workspace_dir(name)
    t = store.get_team(conn, team)
    if t["archived"]:
        return f"Team '{team}' was disbanded.", [], ""
    if by != "human" and above(model, t["max_model"]):
        return (f"Your team's max_model is {t['max_model']}. Spawn it on {t['max_model']}, then ask for {model} "
                f"with request('model', {{'dev': '{name}', 'model': '{model}'}}, reason).", [], "")
    awake = store.awake_devs(conn, team)
    if by != "human" and len(awake) >= t["max_awake"]:
        return (f"Team '{team}' already has {len(awake)} devs busy (its limit is {t['max_awake']}): "
                f"{', '.join(awake)}. Wait for one to finish its turn.", [], "")
    err = ceiling_error(conn)
    if err:
        return err, [], ""
    store.add_team_member(conn, team, name)
    store.set_model(conn, name, model, effort or None)
    conn.execute("INSERT OR IGNORE INTO channel_members (channel, session_name, added_at) VALUES (?, ?, ?)",
                 (team, name, store.now()))
    sup = t["supervisor"]
    brief = (f"[thunderhead] You're a specialist on team '{team}', running on {model}. Your supervisor '{sup}' "
             f"keeps you for your context and calls on you for work. When you finish or get stuck, report back "
             f"with send('{sup}', ..., wake=True). Team channel: #{team}.\n\nYour task:\n{task}")
    return None, launch.bg_command(name, dev=True, model=model, effort=effort or None) + [brief], str(cwd)


def undo_spawn(conn, team: str, name: str):
    conn.execute("DELETE FROM team_members WHERE team=? AND session_name=?", (team, name))
    conn.execute("DELETE FROM channel_members WHERE channel=? AND session_name=?", (team, name))
    conn.execute("DELETE FROM session_models WHERE name=?", (name,))


ONEOFF_BRIEF = """[thunderhead] You're a one-off session, started by The Thunderhead for a single task. You're not on a team.
When you're done, send the result to The Thunderhead with send('thunderhead', ...): what you found or did, and how you know it's right (the commands you ran, their results). Keep it short, and point to files rather than pasting them. Then end your turn.
Don't take on other work. Soon after you finish, you'll be deleted automatically; your Discord thread stays as the record.

Your task:
{task}"""


def spawn_oneoff(conn, lead, directory: str, task: str, name: str = "", model: str = "") -> tuple[str | None, list[str], str, str]:
    """Validate and register a one-off for The Thunderhead. Returns (error, command, folder, name)."""
    if directory:
        cwd = Path(directory).expanduser()
        err = launch.forbidden_dir(cwd)
        if err:
            return err, [], "", ""
        if not cwd.is_dir():
            return f"{cwd} is not a directory.", [], "", ""
    else:
        cwd = None  # a workspace, once the name is settled
    if not task.strip():
        return "Give the task.", [], "", ""
    name = name or f"oneoff-{secrets.token_hex(2)}"
    if not launch.NAME_RE.match(name) or name == store.LEAD or store.session_by_name(conn, name):
        return f"The name '{name}' is taken or invalid.", [], "", ""
    running = conn.execute("SELECT COUNT(*) FROM oneoffs WHERE by_lead=1").fetchone()[0]
    if running >= MAX_ONEOFFS:
        return (f"You already have {running} one-offs running (the limit is {MAX_ONEOFFS}). Wait for one to "
                "finish, or ask the human.", [], "", "")
    err = ceiling_error(conn)
    if err:
        return err, [], "", ""
    model = model or DEFAULT_DEV_MODEL
    err = model_error(model)
    if err:
        return err, [], "", ""
    cwd = cwd or launch.workspace_dir(name)
    conn.execute("INSERT INTO oneoffs (name, by_lead) VALUES (?, 1)", (name,))
    store.set_model(conn, name, model)
    store.post(conn, lead["id"], "report", f"🧩 Started one-off **{name}** in `{cwd}`: {task[:300]}")
    return None, launch.bg_command(name, model=model) + [ONEOFF_BRIEF.format(task=task)], str(cwd), name


def delete_check(conn, name) -> str | None:
    """Why a session can't be deleted, or None if it can."""
    if name == store.LEAD:
        return "The Thunderhead can't be deleted. Use /wipe to give it a fresh start."
    role, team = store.rank(conn, name)
    if role == "supervisor" and not store.team_archived(conn, team):
        return (f"'{name}' supervises team '{team}'. Deleting it would leave the team without an owner; "
                f"/disband the team first.")
    if store.session_by_name(conn, name) is None:
        return f"No session named '{name}'."
    return None


def forget_session(conn, name, keep_thread: bool) -> dict:
    """Remove a session from the fleet's records. Returns what the caller must clean up outside the
    store: its session ids (Claude jobs) and thread id.

    keep_thread leaves one 'deleted' row holding the thread, so #archived and /cleanup still work.
    """
    rows = conn.execute("SELECT id, thread_id FROM sessions WHERE name=?", (name,)).fetchall()
    ids = [r["id"] for r in rows]
    thread_id = next((r["thread_id"] for r in rows if r["thread_id"]), None)
    role, team = store.rank(conn, name)
    marks = ",".join("?" * len(ids)) or "''"
    conn.execute(f"DELETE FROM messages WHERE to_session IN ({marks})", ids)
    conn.execute(f"UPDATE approvals SET status='expired', closed=1 WHERE status='pending' AND session_id IN ({marks})", ids)
    for table in ("team_members", "channel_members", "channel_reads", "oneoffs"):
        conn.execute(f"DELETE FROM {table} WHERE {'name' if table == 'oneoffs' else 'session_name'}=?", (name,))
    if keep_thread and thread_id:
        keep = next(r["id"] for r in rows if r["thread_id"] == thread_id)
        conn.execute(f"DELETE FROM sessions WHERE name=? AND id != ?", (name, keep))
        store.set_status(conn, keep, "deleted")
    else:
        conn.execute("DELETE FROM sessions WHERE name=?", (name,))
    if role == "dev":
        fyi(conn, store.get_team(conn, team)["supervisor"],
            f"The human deleted your dev '{name}'. Drop it from your roster.")
    return {"ids": ids, "thread_id": thread_id}


# --- requests ---------------------------------------------------------------

def describe(req) -> str:
    params = json.loads(req["params"])
    body = ", ".join(f"{k}={v!r}" for k, v in params.items())
    return f"#{req['id']} from {req['from_name']} (team {req['team']}): {req['action']}({body})\nWhy: {req['reason']}"


def withdraw_request(conn, me, req_id: int, reason: str) -> str:
    """A supervisor taking back one of its own requests before it's decided."""
    req = get_request(conn, req_id)
    if req is None or req["from_name"] != me["name"]:
        return f"#{req_id} isn't one of your requests."
    if req["status"] not in ("pending", "escalated"):
        return f"#{req_id} is already {req['status']}."
    conn.execute("UPDATE requests SET status='withdrawn', note=?, decided_at=? WHERE id=?",
                 (reason, store.now(), req_id))
    fyi(conn, store.LEAD, f"{me['name']} withdrew request #{req_id} ({req['action']}): {reason}")
    return f"Withdrew request #{req_id}."


def create_request(conn, me, action: str, params: dict, reason: str, replaces: int = 0) -> str:
    team = store.team_of(conn, me["name"])
    if team is None or team["supervisor"] != me["name"]:
        return "Only a team's supervisor can make requests."
    if replaces:
        result = withdraw_request(conn, me, replaces, "replaced by a new request")
        if not result.startswith("Withdrew"):
            return f"Can't replace #{replaces}: {result}"
    if action not in REQUEST_ACTIONS:
        return f"action must be one of {', '.join(REQUEST_ACTIONS)}."
    if not reason.strip():
        return "Say why: The Thunderhead needs a reason to approve it."
    if action == "spawn":
        return "Spawning doesn't need a request any more: use spawn_dev() directly."
    need = {"model": ("dev", "model"), "channel": ("name", "members")}.get(action, ())
    missing = [k for k in need if not params.get(k)]
    if missing:
        return f"A {action} request needs: {', '.join(missing)}."
    if action == "model":
        err = model_error(params["model"], params.get("effort"))
        role, their_team = store.rank(conn, params["dev"])
        if err or role != "dev" or their_team != team["name"]:
            return err or f"'{params['dev']}' isn't one of your devs."
    cur = conn.execute("INSERT INTO requests (from_name, team, action, params, reason, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (me["name"], team["name"], action, json.dumps(params), reason, store.now()))
    req = conn.execute("SELECT * FROM requests WHERE id=?", (cur.lastrowid,)).fetchone()
    lead = store.session_by_name(conn, store.LEAD)
    if lead is not None:
        replaced = f"\nThis replaces request #{replaces}, which is withdrawn." if replaces else ""
        store.queue_message(conn, lead["id"], "supervisor", me["name"],
                            f"New request {describe(req)}{replaced}\n\nDecide with approve_request({req['id']}), "
                            f"reject_request({req['id']}, why) or escalate_request({req['id']}, note).", hops=1)
    note = f" (replaces #{replaces})" if replaces else ""
    return f"Request #{req['id']}{note} sent to The Thunderhead. You'll hear back when it's decided."


def get_request(conn, req_id):
    return conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()


def decide(conn, req_id: int, approve: bool, note: str, by: str) -> tuple[str, list[str] | None, str]:
    """Record a decision. Returns (message, command to run or None, folder).

    Approving a spawn returns the command so the caller can run it outside the transaction.
    """
    req = get_request(conn, req_id)
    if req is None or req["status"] not in ("pending", "escalated"):
        return f"No open request #{req_id}.", None, ""
    if req["action"] in ("charter", "config") and by != "human":
        return f"Request #{req_id} ({req['action']}) is the human's to decide.", None, ""
    params = json.loads(req["params"])
    cmd, cwd, result = None, "", ""
    if approve and req["action"] == "config":
        result = apply_config(conn, req["team"], params, by="human")
    elif approve and req["action"] == "charter":
        launch.write_charter(req["team"], params["text"])
        conn.execute("UPDATE teams SET charter_status='approved' WHERE name=?", (req["team"],))
        result = "The charter is now in force; the supervisor loads it at its next start."
    if approve:
        if req["action"] == "model":
            store.set_model(conn, params["dev"], params["model"], params.get("effort"))
            result = f"'{params['dev']}' runs on {params['model']} from its next wake-up."
        elif req["action"] == "channel":
            result = create_channel(conn, req["from_name"], params["name"], params.get("members", []),
                                    params.get("topic", ""))
            if not result.startswith("Created"):
                approve, note = False, f"{note} (couldn't do it: {result})".strip()
    status = "approved" if approve else "rejected"
    conn.execute("UPDATE requests SET status=?, note=?, decided_at=? WHERE id=?",
                 (status, note, store.now(), req_id))
    who = "the human" if by == "human" else "The Thunderhead"
    outcome = f"Your request #{req_id} ({req['action']}) was {status} by {who}."
    if approve and result:
        outcome += f" {result}"
    if note:
        outcome += f"\nNote: {note}"
    if req["from_name"] != store.LEAD:
        notify(conn, store.LEAD if by != "human" else "human", [req["from_name"]], outcome)
    if by == "human":
        fyi(conn, store.LEAD, f"The human {status} request #{req_id} from {req['from_name']}.")
    return f"Request #{req_id} {status}. {result}".strip(), cmd, cwd


# --- team settings ----------------------------------------------------------

SETTINGS = ("autonomy", "max_awake", "max_model")


def loosens(team, changes: dict) -> list[str]:
    """Which changes give a team more room (these need the human's yes)."""
    out = []
    if changes.get("autonomy") == "act" and team["autonomy"] != "act":
        out.append("autonomy → act")
    if changes.get("max_awake") is not None and changes["max_awake"] > team["max_awake"]:
        out.append(f"max_awake {team['max_awake']} → {changes['max_awake']}")
    if changes.get("max_model") and above(changes["max_model"], team["max_model"]):
        out.append(f"max_model {team['max_model']} → {changes['max_model']}")
    return out


def apply_config(conn, team_name: str, changes: dict, by: str) -> str:
    """Set a team's settings, then tell the human and the supervisor."""
    team = store.get_team(conn, team_name)
    sets = {k: v for k, v in changes.items() if k in SETTINGS and v is not None}
    for k, v in sets.items():
        conn.execute(f"UPDATE teams SET {k}=? WHERE name=?", (v, team_name))
    summary = ", ".join(f"{k}={v}" for k, v in sets.items())
    who = "the human" if by == "human" else "The Thunderhead"
    lead = store.session_by_name(conn, store.LEAD)
    store.post(conn, lead["id"] if lead else None, "report", f"⚙️ Team **{team_name}** settings changed by {who}: {summary}")
    notify(conn, "human" if by == "human" else store.LEAD, [team["supervisor"]],
           f"Your team's settings changed: {summary}. "
           + ("With autonomy=act you may pick up backlog work on your own, within your charter."
              if sets.get("autonomy") == "act" else
              "With autonomy=propose, only work on what you're given and propose what's next."
              if sets.get("autonomy") == "propose" else ""))
    if by == "human":
        fyi(conn, store.LEAD, f"The human changed team {team_name}'s settings: {summary}.")
    return f"Team '{team_name}': {summary}."


def request_config(conn, lead, team_name: str, changes: dict, reason: str) -> str:
    """The Thunderhead changing a team's settings: tightening applies now, loosening goes to the human."""
    team = store.get_team(conn, team_name)
    if team is None:
        return f"No team '{team_name}'."
    if changes.get("autonomy") not in (None, *AUTONOMY):
        return f"autonomy must be one of {', '.join(AUTONOMY)}."
    if changes.get("max_awake") is not None and not 0 <= changes["max_awake"] <= 50:
        return "max_awake must be between 0 and 50."
    err = model_error(changes.get("max_model"))
    if err:
        return err.replace("model", "max_model", 1)
    changes = {k: v for k, v in changes.items() if k in SETTINGS and v is not None}
    looser = loosens(team, changes)
    loose_keys = {k for k, v in changes.items()
                  if (k == "autonomy" and v == "act") or (k == "max_awake" and v > team["max_awake"])
                  or (k == "max_model" and above(v, team["max_model"]))}
    tighter = {k: v for k, v in changes.items() if k not in loose_keys}
    out = []
    if tighter:
        out.append(apply_config(conn, team_name, tighter, by=store.LEAD))
    if looser:
        wide = {k: changes[k] for k in loose_keys}
        cur = conn.execute("INSERT INTO requests (from_name, team, action, params, reason, status, created_at) "
                           "VALUES (?, ?, 'config', ?, ?, 'escalated', ?)",
                           (lead["name"], team_name, json.dumps(wide), reason, store.now()))
        store.post(conn, lead["id"], "request_escalated", str(cur.lastrowid))
        out.append(f"Asked the human to approve {', '.join(looser)} (request #{cur.lastrowid}); "
                   "giving a team more room needs their yes.")
    return " ".join(out) or "Nothing to change."


def propose_charter(conn, me, text: str, summary: str) -> str:
    """A supervisor's charter, to the human for approval."""
    team = store.team_of(conn, me["name"])
    if team is None or team["supervisor"] != me["name"]:
        return "Only a team's supervisor can propose its charter."
    if len(text.strip()) < 40:
        return "Give the full charter text, following the sections in your current draft."
    conn.execute("UPDATE requests SET status='rejected', note='superseded' WHERE team=? AND action='charter' "
                 "AND status='escalated'", (team["name"],))
    cur = conn.execute("INSERT INTO requests (from_name, team, action, params, reason, status, created_at) "
                       "VALUES (?, ?, 'charter', ?, ?, 'escalated', ?)",
                       (me["name"], team["name"], json.dumps({"text": text}), summary, store.now()))
    store.post(conn, me["id"], "request_escalated", str(cur.lastrowid))
    fyi(conn, store.LEAD, f"{me['name']} proposed a charter for team {team['name']} to the human: {summary}")
    return f"Charter sent to the human for approval (request #{cur.lastrowid}). Keep working from the draft meanwhile."


# --- history ----------------------------------------------------------------

HISTORY_KINDS = {"report": "report", "status": "status", "agent_msg": "sent", "channel_post": "posted",
                 "session_start": "started", "session_end": "ended", "stopped": "stopped", "woken": "woken",
                 "needs_you": "needed the human", "compacted": "compacted", "usage_limit": "hit a usage limit"}


def history(conn, names: list[str], hours: float, limit: int) -> list[str]:
    """What these sessions did and were told, newest last: from the outbox (what they reported,
    sent and posted) and the human's messages to them."""
    since = store.now() - hours * 3600
    ids = {r["id"]: r["name"] for r in conn.execute(
        f"SELECT id, name FROM sessions WHERE name IN ({','.join('?' * len(names))})", names)} if names else {}
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = []
    for r in conn.execute(f"SELECT session_id, kind, body, channel, created_at FROM outbox WHERE session_id IN ({marks}) "
                          f"AND created_at >= ? AND kind IN ({','.join('?' * len(HISTORY_KINDS))})",
                          [*ids, since, *HISTORY_KINDS]):
        where = f" in #{r['channel']}" if r["channel"] else ""
        rows.append((r["created_at"], f"{ids[r['session_id']]} {HISTORY_KINDS[r['kind']]}{where}: {r['body']}"))
    for r in conn.execute(f"SELECT to_session, body, created_at FROM messages WHERE to_session IN ({marks}) "
                          f"AND from_kind='human' AND created_at >= ?", [*ids, since]):
        rows.append((r["created_at"], f"the human told {ids[r['to_session']]}: {r['body']}"))
    rows.sort()
    import datetime
    out = []
    for ts, text in rows[-limit:]:
        when = datetime.datetime.fromtimestamp(ts).strftime("%a %H:%M")
        text = " ".join(text.split())
        out.append(f"[{when}] {text[:300]}{'…' if len(text) > 300 else ''}")
    return out


# --- tasks ------------------------------------------------------------------

TASK_STATUSES = ("todo", "doing", "review", "blocked", "done", "dropped")


def add_task(conn, team: str, title: str, detail: str, owner: str | None, by: str) -> int:
    cur = conn.execute("INSERT INTO tasks (team, title, detail, owner, created_by, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)", (team, title, detail, owner or None, by, store.now(), store.now()))
    return cur.lastrowid


def get_task(conn, task_id: int):
    return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def update_task(conn, task_id: int, **changes):
    sets = {k: v for k, v in changes.items() if v not in (None, "")}
    if sets:
        conn.execute(f"UPDATE tasks SET {', '.join(f'{k}=?' for k in sets)}, updated_at=? WHERE id=?",
                     (*sets.values(), store.now(), task_id))


def team_tasks(conn, team: str, include_done: bool = False):
    q = "SELECT * FROM tasks WHERE team=?" + ("" if include_done else " AND status NOT IN ('done','dropped')")
    return conn.execute(q + " ORDER BY id", (team,)).fetchall()


def task_line(t) -> str:
    owner = f" · @{t['owner']}" if t["owner"] else " · unassigned"
    branch = f" · `{t['branch']}`" if t["branch"] else ""
    return f"#{t['id']} **{t['title']}**{owner}{branch}"


# --- disbanding -------------------------------------------------------------

def disband(conn, team: str) -> dict:
    """Retire a team in the store: archived, its channels closed, its memory folder moved to
    teams/_archived/. Returns what the caller must do outside the store."""
    import shutil
    import time as _time
    t = store.get_team(conn, team)
    names = store.team_members_of(conn, team)
    sessions = [s for s in (store.session_by_name(conn, n) for n in names) if s is not None]
    for s in sessions:
        store.set_status(conn, s["id"], "stopped")  # quiet SessionEnd; only the human could wake it
    conn.execute("UPDATE teams SET archived=1 WHERE name=?", (team,))
    channels = conn.execute("SELECT * FROM channels WHERE team=?", (team,)).fetchall()
    conn.execute("UPDATE channels SET closed=1 WHERE team=?", (team,))
    folder = launch.team_folder(team)
    moved = None
    if folder.exists():
        dest = launch.TEAMS / "_archived" / team
        if dest.exists():
            dest = dest.with_name(f"{team}-{_time.strftime('%Y%m%d-%H%M')}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(dest))
        moved = dest
    lead = store.session_by_name(conn, store.LEAD)
    if lead is not None:
        store.queue_message(conn, lead["id"], "fyi", "thunderhead-system",
                            f"The human disbanded team '{team}' (supervisor {t['supervisor']}, "
                            f"{len(names) - 1} devs). Its memory is archived at {moved or folder}. "
                            "Remove it from your routing notes.", urgent=False)
    return {"sessions": sessions, "channels": [c["discord_id"] for c in channels if c["discord_id"]],
            "desk": t["desk_id"], "category": t["category_id"], "folder": moved}
