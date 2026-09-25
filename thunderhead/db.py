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
"""

# Columns added after the first release. Each runs once; "duplicate column" means done.
MIGRATIONS = [
    "ALTER TABLE sessions ADD COLUMN role TEXT DEFAULT 'worker'",
    "ALTER TABLE messages ADD COLUMN channel TEXT",
    "ALTER TABLE outbox ADD COLUMN channel TEXT",
]

# The lead session: its name, and the role that unlocks its tools.
LEAD = "thunderhead"

# Statuses that mean the session is no longer running. 'stopped' was paused on
# purpose and a message wakes it; 'wiped' is a ThunderHead cleared by /wipe, which
# nothing may resume.
DEAD = ("ended", "gone", "stopped", "wiped")
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
        "SELECT thread_id FROM sessions WHERE name=? AND thread_id IS NOT NULL "
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
    return conn.execute(f"SELECT * FROM sessions WHERE name=? {_CURRENT}", (name,)).fetchone()


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

def queue_message(conn, to_session, from_kind, from_name, body, hops=0, channel=None):
    """from_kind is 'human', 'lead' (The ThunderHead) or 'agent'."""
    conn.execute(
        "INSERT INTO messages (to_session, from_kind, from_name, body, hops, channel, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (to_session, from_kind, from_name, body, hops, channel, now()))


def take_messages(conn, sid):
    """Atomically claim every undelivered message for a session."""
    rows = conn.execute(
        "UPDATE messages SET delivered_at=? WHERE to_session=? AND delivered_at IS NULL "
        "RETURNING id, from_kind, from_name, body, hops, channel", (now(), sid)).fetchall()
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


def decide_approval(conn, approval_id, decision) -> bool:
    """Record a decision; False if it was already decided or expired."""
    cur = conn.execute(
        "UPDATE approvals SET status=?, decided_at=? WHERE id=? AND status='pending'",
        (decision, now(), approval_id))
    return cur.rowcount == 1


# --- group channels ---------------------------------------------------------

CHANNEL_RE = r"^[a-z0-9][a-z0-9-]{0,39}$"
# Discord channels the bot already uses.
RESERVED_CHANNELS = ("fleet", "needs-you", "agent-chatter", LEAD)


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


def fan_out(conn, channel, from_kind, from_name, body, hops=0) -> list[str]:
    """Queue a channel post for every member except the sender. Returns who got it."""
    got = []
    for name in members(conn, channel):
        if from_kind != "human" and name == from_name:
            continue
        target = session_by_name(conn, name)
        if target is None:
            continue
        queue_message(conn, target["id"], from_kind, from_name, body, hops=hops, channel=channel)
        got.append(name)
    return got


def recent_posts(conn, channel, seconds=60) -> int:
    return conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='channel_post' AND channel=? AND created_at>?",
                        (channel, now() - seconds)).fetchone()[0]


# --- kv ---------------------------------------------------------------------

def kv_get(conn, key):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn, key, value):
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
