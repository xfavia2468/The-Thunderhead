"""SQLite store shared by every THUNDERHEAD process.

Hooks and MCP servers run inside Claude sessions and write here; the Discord
bot polls the `outbox` and `approvals` tables and writes user messages into
`messages`. WAL mode lets all of them work on the file at once.
"""
import json
import sqlite3
import time
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,   -- Claude Code session id
    name            TEXT NOT NULL,
    cwd             TEXT,
    pid             INTEGER,
    status          TEXT NOT NULL DEFAULT 'starting',
    summary         TEXT DEFAULT '',
    listen          INTEGER DEFAULT 0,
    remote_approval INTEGER DEFAULT 0,
    current_hops    INTEGER DEFAULT 0,
    thread_id       INTEGER,            -- Discord thread for this session
    created_at      REAL,
    updated_at      REAL
);
CREATE INDEX IF NOT EXISTS sessions_name ON sessions(name);
CREATE INDEX IF NOT EXISTS sessions_pid ON sessions(pid);

-- Messages waiting to be delivered into a session.
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    to_session   TEXT NOT NULL,
    from_kind    TEXT NOT NULL,         -- 'human' or 'agent'
    from_name    TEXT NOT NULL,
    body         TEXT NOT NULL,
    hops         INTEGER DEFAULT 0,
    created_at   REAL,
    delivered_at REAL
);
CREATE INDEX IF NOT EXISTS messages_pending ON messages(to_session, delivered_at);

-- Events for the bot to post to Discord.
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY,
    session_id TEXT,
    kind       TEXT NOT NULL,           -- report | status | needs_you | agent_msg | session_start | session_end
    body       TEXT NOT NULL,
    created_at REAL,
    posted_at  REAL
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(posted_at);

CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    tool_input  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | allow | deny | expired
    message_id  INTEGER,                          -- Discord message with the buttons
    closed      INTEGER DEFAULT 0,                -- 1 once the Discord message shows the outcome
    created_at  REAL,
    decided_at  REAL
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

-- Group channels: every member session gets every post. Members are session
-- names, which survive a session being resumed under a new id.
CREATE TABLE IF NOT EXISTS channels (
    name        TEXT PRIMARY KEY,
    topic       TEXT DEFAULT '',
    discord_id  INTEGER,                -- Discord text channel, once the bot has made it
    created_by  TEXT,
    created_at  REAL,
    closed      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS channel_members (
    channel      TEXT NOT NULL,
    session_name TEXT NOT NULL,
    added_at     REAL,
    PRIMARY KEY (channel, session_name)
);
-- Every post, whoever was notified. Members not notified can read it here.
CREATE TABLE IF NOT EXISTS channel_log (
    id         INTEGER PRIMARY KEY,
    channel    TEXT NOT NULL,
    from_kind  TEXT NOT NULL,
    from_name  TEXT NOT NULL,
    body       TEXT NOT NULL,
    notified   TEXT NOT NULL,           -- JSON list of session names that were pinged
    created_at REAL
);
CREATE TABLE IF NOT EXISTS channel_reads (
    channel      TEXT NOT NULL,
    session_name TEXT NOT NULL,
    last_id      INTEGER NOT NULL,
    PRIMARY KEY (channel, session_name)
);

-- Teams: a supervisor (the product owner) plus its dev sessions. By session name.
CREATE TABLE IF NOT EXISTS teams (
    name            TEXT PRIMARY KEY,
    topic           TEXT DEFAULT '',
    repos           TEXT NOT NULL,      -- JSON list of folders the team works in
    supervisor      TEXT NOT NULL,
    category_id     INTEGER,            -- Discord category for the team
    desk_id         INTEGER,            -- Discord channel for talking to the supervisor
    created_at      REAL
);
CREATE TABLE IF NOT EXISTS team_members (
    team         TEXT NOT NULL,
    session_name TEXT NOT NULL UNIQUE,  -- a session is on at most one team
    added_at     REAL,
    PRIMARY KEY (team, session_name)
);

-- #needs-you notices, removed once their session no longer needs the human.
CREATE TABLE IF NOT EXISTS needs_you_posts (
    message_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at REAL
);

-- Sessions spawned as one-offs: deleted automatically once they finish and fall asleep.
CREATE TABLE IF NOT EXISTS oneoffs (name TEXT PRIMARY KEY);

-- #archived: one listing per archived session thread, removed when the thread comes back.
CREATE TABLE IF NOT EXISTS archived_posts (
    thread_id  INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL
);

-- Things a supervisor asked The Thunderhead to do.
CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY,
    from_name   TEXT NOT NULL,
    team        TEXT NOT NULL,
    action      TEXT NOT NULL,          -- spawn | channel | other
    params      TEXT NOT NULL,          -- JSON
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | escalated | approved | rejected | failed
    note        TEXT DEFAULT '',
    message_id  INTEGER,                -- Discord message with the human's buttons, once escalated
    created_at  REAL,
    decided_at  REAL
);
"""

# Columns added after the first release. Each runs once; "duplicate column" means done.
MIGRATIONS = [
    "ALTER TABLE sessions ADD COLUMN role TEXT DEFAULT 'worker'",
    "ALTER TABLE messages ADD COLUMN channel TEXT",
    "ALTER TABLE outbox ADD COLUMN channel TEXT",
    # 0 = FYI: delivered with the next real message, never wakes the session on its own.
    "ALTER TABLE messages ADD COLUMN urgent INTEGER DEFAULT 1",
    "ALTER TABLE channels ADD COLUMN team TEXT",  # set for a team's own channels
    # Team settings. autonomy: 'propose' (only works on what it's given, suggests what's next)
    # or 'act' (picks up its own backlog). charter_status: 'draft' until the human approves it.
    "ALTER TABLE teams ADD COLUMN autonomy TEXT DEFAULT 'propose'",
    "ALTER TABLE teams ADD COLUMN max_devs INTEGER DEFAULT 3",
    "ALTER TABLE teams ADD COLUMN charter_status TEXT DEFAULT 'draft'",
    "ALTER TABLE oneoffs ADD COLUMN by_lead INTEGER DEFAULT 0",  # spawned by The Thunderhead
    "ALTER TABLE approvals ADD COLUMN reason TEXT",  # the human's reason when denying
]

# The lead session: its name, and the role that unlocks its tools.
LEAD = "thunderhead"

# Statuses that mean the session is no longer running. 'stopped' was paused on
# purpose and a message wakes it; 'wiped' is a Thunderhead cleared by /wipe, which
# nothing may resume.
DEAD = ("ended", "gone", "stopped", "wiped", "deleted")
# 'deleted' rows only remain to keep a deleted session's thread attached to its record (for
# #archived and /cleanup). Name lookups skip them, so the name is free again.
# Asleep after sitting idle: its process is shut down, but any message wakes it.
# Not in DEAD, so it still counts as part of the fleet.
SLEEPING = "sleeping"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def now() -> float:
    return time.time()


# --- sessions ---------------------------------------------------------------

def upsert_session(conn, sid, *, name, cwd, pid, listen, remote_approval, role="worker"):
    ts = now()
    # A resumed or cleared session keeps its name; reuse that name's thread.
    prev = conn.execute(
        "SELECT thread_id FROM sessions WHERE name=? AND thread_id IS NOT NULL AND status != 'deleted' "
        "ORDER BY updated_at DESC LIMIT 1", (name,)).fetchone()
    conn.execute(
        """INSERT INTO sessions (id, name, cwd, pid, status, listen, remote_approval,
                                 thread_id, created_at, updated_at, role)
           VALUES (?, ?, ?, ?, 'starting', ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               name=excluded.name, cwd=excluded.cwd, pid=excluded.pid,
               status='starting', listen=excluded.listen,
               remote_approval=excluded.remote_approval, role=excluded.role,
               thread_id=COALESCE(sessions.thread_id, excluded.thread_id),
               updated_at=excluded.updated_at""",
        (sid, name, cwd, pid, int(listen), int(remote_approval),
         prev["thread_id"] if prev else None, ts, ts, role))


def get_session(conn, sid):
    return conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()


def session_by_pid(conn, pid):
    return conn.execute(
        "SELECT * FROM sessions WHERE pid=? ORDER BY updated_at DESC LIMIT 1", (pid,)).fetchone()


# A resumed session gets a new id, so one name or thread can have several rows.
# The current one is the live row, else the newest.
_CURRENT = f"ORDER BY status IN {DEAD}, created_at DESC LIMIT 1"


def session_by_name(conn, name):
    return conn.execute(f"SELECT * FROM sessions WHERE name=? AND status != 'deleted' {_CURRENT}",
                        (name,)).fetchone()


def is_current(conn, row) -> bool:
    """Whether this row is its name's current session (not an older one, and not deleted)."""
    cur = session_by_name(conn, row["name"])
    return cur is not None and cur["id"] == row["id"]


def session_by_thread(conn, thread_id):
    return conn.execute(f"SELECT * FROM sessions WHERE thread_id=? {_CURRENT}", (thread_id,)).fetchone()


def live_sessions(conn):
    return conn.execute(
        f"SELECT * FROM sessions WHERE status NOT IN {DEAD} ORDER BY created_at").fetchall()


def set_status(conn, sid, status, summary=None):
    if summary is None:
        conn.execute("UPDATE sessions SET status=?, updated_at=? WHERE id=?", (status, now(), sid))
    else:
        conn.execute("UPDATE sessions SET status=?, summary=?, updated_at=? WHERE id=?",
                     (status, summary, now(), sid))


# --- messages ---------------------------------------------------------------

def queue_message(conn, to_session, from_kind, from_name, body, hops=0, channel=None, urgent=True):
    """from_kind is 'human', 'lead' (The Thunderhead), 'supervisor' or 'agent'.

    urgent=False makes it an FYI: it rides along with the next urgent message.
    """
    conn.execute(
        "INSERT INTO messages (to_session, from_kind, from_name, body, hops, channel, urgent, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (to_session, from_kind, from_name, body, hops, channel, int(urgent), now()))


def has_urgent(conn, sid) -> bool:
    return conn.execute("SELECT 1 FROM messages WHERE to_session=? AND delivered_at IS NULL AND urgent=1",
                        (sid,)).fetchone() is not None


def take_messages(conn, sid):
    """Atomically claim every undelivered message for a session."""
    rows = conn.execute(
        "UPDATE messages SET delivered_at=? WHERE to_session=? AND delivered_at IS NULL "
        "RETURNING id, from_kind, from_name, body, hops, channel, urgent", (now(), sid)).fetchall()
    return sorted(rows, key=lambda r: r["id"])


def pending_count(conn, sid):
    return conn.execute("SELECT COUNT(*) FROM messages WHERE to_session=? AND delivered_at IS NULL",
                        (sid,)).fetchone()[0]


# --- outbox -----------------------------------------------------------------

def post(conn, sid, kind, body, channel=None):
    conn.execute("INSERT INTO outbox (session_id, kind, body, channel, created_at) VALUES (?, ?, ?, ?, ?)",
                 (sid, kind, body, channel, now()))


# --- approvals --------------------------------------------------------------

def create_approval(conn, sid, tool_name, tool_input) -> int:
    cur = conn.execute(
        "INSERT INTO approvals (session_id, tool_name, tool_input, created_at) VALUES (?, ?, ?, ?)",
        (sid, tool_name, json.dumps(tool_input, indent=1)[:4000], now()))
    return cur.lastrowid


def decide_approval(conn, approval_id, decision, reason: str = "") -> bool:
    """Record a decision; False if it was already decided or expired."""
    cur = conn.execute(
        "UPDATE approvals SET status=?, reason=?, decided_at=? WHERE id=? AND status='pending'",
        (decision, reason or None, now(), approval_id))
    return cur.rowcount == 1


# --- group channels ---------------------------------------------------------

CHANNEL_RE = r"^[a-z0-9][a-z0-9-]{0,39}$"
# Discord channels the bot already uses.
RESERVED_CHANNELS = ("fleet", "needs-you", "agent-chatter", LEAD, "groups", "archived")


def get_channel(conn, name):
    return conn.execute("SELECT * FROM channels WHERE name=?", (name,)).fetchone()


def channel_by_discord(conn, discord_id):
    return conn.execute("SELECT * FROM channels WHERE discord_id=? AND closed=0", (discord_id,)).fetchone()


def open_channels(conn):
    return conn.execute("SELECT * FROM channels WHERE closed=0 ORDER BY created_at").fetchall()


def members(conn, channel) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT session_name FROM channel_members WHERE channel=? ORDER BY added_at", (channel,))]


def channels_of(conn, session_name) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT m.channel FROM channel_members m JOIN channels c ON c.name=m.channel "
        "WHERE m.session_name=? AND c.closed=0 ORDER BY c.created_at", (session_name,))]


def fan_out(conn, channel, from_kind, from_name, body, notify, hops=0) -> list[str]:
    """Log a channel post, and deliver it to the members in `notify` ("all" means every member).

    Members who weren't notified aren't interrupted; they see it as unread. Returns who was notified.
    """
    everyone = [m for m in members(conn, channel) if from_kind == "human" or m != from_name]
    wanted = everyone if "all" in notify else [m for m in everyone if m in notify]
    got = []
    for name in wanted:
        target = session_by_name(conn, name)
        if target is not None:
            queue_message(conn, target["id"], from_kind, from_name, body, hops=hops, channel=channel)
            got.append(name)
    conn.execute("INSERT INTO channel_log (channel, from_kind, from_name, body, notified, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (channel, from_kind, from_name, body, json.dumps(got), now()))
    return got


def unread(conn, session_name) -> list[tuple[str, int]]:
    """(channel, count) of posts this session wasn't notified of and hasn't read."""
    out = []
    for ch in channels_of(conn, session_name):
        row = conn.execute("SELECT last_id FROM channel_reads WHERE channel=? AND session_name=?",
                           (ch, session_name)).fetchone()
        posts = conn.execute("SELECT from_name, notified FROM channel_log WHERE channel=? AND id>?",
                             (ch, row["last_id"] if row else 0)).fetchall()
        n = sum(1 for p in posts if p["from_name"] != session_name and session_name not in json.loads(p["notified"]))
        if n:
            out.append((ch, n))
    return out


def read_log(conn, channel, session_name, limit=20):
    """The channel's last `limit` posts, marking everything up to now as read."""
    rows = conn.execute("SELECT * FROM channel_log WHERE channel=? ORDER BY id DESC LIMIT ?",
                        (channel, limit)).fetchall()[::-1]
    if rows:
        conn.execute("INSERT INTO channel_reads (channel, session_name, last_id) VALUES (?, ?, ?) "
                     "ON CONFLICT(channel, session_name) DO UPDATE SET last_id=excluded.last_id",
                     (channel, session_name, rows[-1]["id"]))
    return rows


def recent_posts(conn, channel, seconds=60) -> int:
    return conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='channel_post' AND channel=? AND created_at>?",
                        (channel, now() - seconds)).fetchone()[0]


# --- teams ------------------------------------------------------------------

def get_team(conn, name):
    return conn.execute("SELECT * FROM teams WHERE name=?", (name,)).fetchone()


def team_of(conn, session_name):
    """The team this session is on (as supervisor or dev), or None."""
    return conn.execute("SELECT t.* FROM teams t JOIN team_members m ON m.team=t.name "
                        "WHERE m.session_name=?", (session_name,)).fetchone()


def team_members_of(conn, team) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT session_name FROM team_members WHERE team=? ORDER BY added_at", (team,))]


def all_teams(conn):
    return conn.execute("SELECT * FROM teams ORDER BY created_at").fetchall()


def dev_count(conn, team) -> int:
    t = get_team(conn, team)
    return sum(1 for m in team_members_of(conn, team) if m != t["supervisor"])


def running_count(conn) -> int:
    """Sessions that are up (sleeping ones cost nothing, so they don't count)."""
    return conn.execute(f"SELECT COUNT(DISTINCT name) FROM sessions WHERE status NOT IN {DEAD} "
                        f"AND status != '{SLEEPING}'").fetchone()[0]


def add_team_member(conn, team, session_name):
    conn.execute("INSERT OR REPLACE INTO team_members (team, session_name, added_at) VALUES (?, ?, ?)",
                 (team, session_name, now()))


def rank(conn, session_name) -> tuple[str, str | None]:
    """(role, team) where role is lead, supervisor, dev or unteamed."""
    if session_name == LEAD:
        return "lead", None
    team = team_of(conn, session_name)
    if team is None:
        return "unteamed", None
    return ("supervisor" if team["supervisor"] == session_name else "dev"), team["name"]


def is_down(conn, sender, recipient) -> bool:
    """True when a message goes down the org chart: from the lead, or from a supervisor to its own dev."""
    s_role, s_team = rank(conn, sender)
    r_role, r_team = rank(conn, recipient)
    return s_role == "lead" or (s_role == "supervisor" and r_team == s_team and r_role == "dev")


# --- kv ---------------------------------------------------------------------

def kv_get(conn, key):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn, key, value):
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
