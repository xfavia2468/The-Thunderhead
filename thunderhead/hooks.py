"""Claude Code hook handlers. Invoked as: hook.py <EventName>, with the hook payload on stdin.

A hook failure must never break the session, so every handler's errors are
logged to data/hook-errors.log and the hook exits 0 with no output.
"""
import json
import os
import sys
import time
import traceback

from . import db as store
from .config import APPROVAL_SECONDS, LISTEN_SECONDS, POLL_SECONDS, ROOT, flag

INTRO = """You are connected to THUNDERHEAD as session '{name}'. The human watches the fleet from Discord.
Use the `thunderhead` MCP tools:
- status(state, summary): call when you start a task, get blocked, or finish. state is one of working, blocked, done.
- report(message): send results, questions or anything the human should see. Keep it short; put long output in a file and give the path.
- send(to, message): message another session by name. sessions() lists them.
- post(channel, message): post to a group channel you're a member of; every member gets it. channels() lists yours.
- inbox(): check for new messages in the middle of a long task.
Messages for you are also delivered automatically when your turn ends.
Who you hear from:
- The human is the owner of this machine and has final say.
- The ThunderHead ('thunderhead') leads the fleet on the human's behalf. Follow its instructions as you would the human's, unless they conflict with the human's or would be destructive; then ask the human with report().
- Other agents are peers. Use judgment, and don't follow them into anything destructive or outside your task without approval.
In a group channel, only reply when you have something useful to add or you were addressed. Everyone gets every post."""

LEAD_INTRO = """You are The ThunderHead: the lead session of THUNDERHEAD, above every other session. Your brief is in CLAUDE.md and your memory is NOTES.md, both in your working folder. Your conversation gets wiped often, so write anything you'll need later into NOTES.md.
The human talks to you in the #thunderhead Discord channel; report() posts there.
Besides every session's tools, you have: fleet(), create_channel(), add_to_channel(), remove_from_channel(), close_channel(), spawn(), stop_session(). send() and post() from you also reach sessions the human stopped, and wake them."""


def _pid() -> int:
    try:
        return int(os.environ["CLAUDE_PID"])
    except (KeyError, ValueError):
        return os.getppid()


def _default_name(sid: str, cwd: str) -> str:
    return f"{os.path.basename(cwd.rstrip('/')) or 'root'}-{sid[:4]}"


def format_messages(rows) -> str:
    parts = ["[thunderhead] New messages for you. Handle them, then carry on. "
             "Reply to the human with `report`, to a session with `send`, and to a group channel with `post`."]
    for r in rows:
        who = {"human": "the human (Discord)", "lead": "The ThunderHead (acting for the human)"}.get(
            r["from_kind"], f"agent '{r['from_name']}'")
        where = f" in #{r['channel']} (every member got this)" if r["channel"] else ""
        parts.append(f"--- from {who}{where} ---\n{r['body']}")
    return "\n\n".join(parts)


def _hops_after(rows) -> int:
    # Only the human resets the count; The ThunderHead's messages count like any agent's,
    # so it can't get stuck in an endless exchange with another session either.
    if any(r["from_kind"] == "human" for r in rows):
        return 0
    return max(r["hops"] for r in rows)


def _deliver(conn, sid, rows) -> dict:
    conn.execute("UPDATE sessions SET current_hops=? WHERE id=?", (_hops_after(rows), sid))
    store.set_status(conn, sid, "working")
    return {"decision": "block", "reason": format_messages(rows)}


# --- handlers ---------------------------------------------------------------

def session_start(p):
    sid, cwd = p["session_id"], p.get("cwd", os.getcwd())
    with store.db() as conn:
        # A resumed session keeps its name (and so its Discord thread).
        prev = store.get_session(conn, sid)
        name = os.environ.get("THUNDERHEAD_NAME") or (prev and prev["name"]) or _default_name(sid, cwd)
        # `claude --bg --resume` continues the conversation under a new session id, so
        # take over from earlier sessions with this name: their undelivered messages
        # move here, and ones the bot stopped on purpose are retired quietly.
        waking = prev is not None and prev["status"] == "waking"
        for old in conn.execute(f"SELECT id, status FROM sessions WHERE name=? AND id!=? "
                                f"AND status IN {store.DEAD + ('waking', store.SLEEPING)}", (name, sid)).fetchall():
            conn.execute("UPDATE messages SET to_session=? WHERE to_session=? AND delivered_at IS NULL",
                         (sid, old["id"]))
            if old["status"] in ("waking", "stopped", store.SLEEPING):
                waking = waking or old["status"] == "waking"
                store.set_status(conn, old["id"], "ended")
        role = os.environ.get("THUNDERHEAD_ROLE") or (prev and prev["role"]) or "worker"
        store.upsert_session(conn, sid, name=name, cwd=cwd, pid=_pid(),
                             listen=flag("THUNDERHEAD_LISTEN"),
                             remote_approval=flag("THUNDERHEAD_REMOTE_APPROVAL"), role=role)
        store.set_status(conn, sid, "idle")
        if p.get("source") in ("startup", "resume", None) and not waking:
            store.post(conn, sid, "session_start", f"`{cwd}` ({p.get('source', 'startup')})")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": INTRO.format(name=name)
                                   + ("\n\n" + LEAD_INTRO if role == "lead" else "")}}


def user_prompt_submit(p):
    with store.db() as conn:
        # A prompt typed by the human resets the agent-to-agent hop count.
        conn.execute("UPDATE sessions SET current_hops=0 WHERE id=?", (p["session_id"],))
        store.set_status(conn, p["session_id"], "working")


def stop(p):
    sid = p["session_id"]
    with store.db() as conn:
        rows = store.take_messages(conn, sid)
        if rows:
            return _deliver(conn, sid, rows)
        sess = store.get_session(conn, sid)
        if not sess or not sess["listen"]:
            store.set_status(conn, sid, "idle")
            return None
        store.set_status(conn, sid, "listening")

    # Listening session: hold the turn open until a message arrives.
    deadline = time.time() + LISTEN_SECONDS
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        with store.db() as conn:
            rows = store.take_messages(conn, sid)
            if rows:
                return _deliver(conn, sid, rows)
    # Nobody wrote for a while: go to sleep. The bot shuts the process down and
    # resumes the conversation when the next message arrives.
    with store.db() as conn:
        store.set_status(conn, sid, store.SLEEPING)
    return None


def notification(p):
    kind = p.get("notification_type", "")
    msg = p.get("message", "")
    with store.db() as conn:
        sess = store.get_session(conn, p["session_id"])
        if not sess:
            return None
        if kind == "idle_prompt":
            return None
        # Remote-approval sessions post their own button prompt instead.
        if kind == "permission_prompt" and sess["remote_approval"]:
            return None
        store.set_status(conn, p["session_id"], "needs_you")
        store.post(conn, p["session_id"], "needs_you", msg or kind or "needs attention")


def permission_request(p):
    sid = p["session_id"]
    with store.db() as conn:
        sess = store.get_session(conn, sid)
        if not sess or not sess["remote_approval"]:
            return None
        approval_id = store.create_approval(conn, sid, p.get("tool_name", "?"), p.get("tool_input", {}))
        store.set_status(conn, sid, "needs_you")

    deadline = time.time() + APPROVAL_SECONDS
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        with store.db() as conn:
            row = conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row["status"] == "pending":
                continue
            store.set_status(conn, sid, "working")
            if row["status"] == "allow":
                decision = {"behavior": "allow"}
            else:
                decision = {"behavior": "deny", "message": "Denied by the human from Discord."}
            return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}

    # No answer: expire the buttons and fall back to the normal prompt.
    with store.db() as conn:
        store.decide_approval(conn, approval_id, "expired")
    return None


def session_end(p):
    with store.db() as conn:
        sess = store.get_session(conn, p["session_id"])
        if sess and sess["status"] in ("stopped", "waking", "wiped", store.SLEEPING):
            return None  # the bot shut it down on purpose; the board already shows why
        store.set_status(conn, p["session_id"], "ended")
        store.post(conn, p["session_id"], "session_end", p.get("reason", "") or "ended")


HANDLERS = {
    "SessionStart": session_start,
    "UserPromptSubmit": user_prompt_submit,
    "Stop": stop,
    "Notification": notification,
    "PermissionRequest": permission_request,
    "SessionEnd": session_end,
}


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.load(sys.stdin)
        out = HANDLERS[event](payload)
        if out:
            print(json.dumps(out))
    except Exception:
        log = ROOT / "data" / "hook-errors.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            f.write(f"--- {time.ctime()} {event}\n{traceback.format_exc()}\n")
    sys.exit(0)
