"""Per-session MCP server: the tools a Claude session uses to talk to the fleet."""
import os

from mcp.server.mcpserver import MCPServer

from . import db as store
from .config import MAX_HOPS
from .hooks import _hops_after, format_messages

mcp = MCPServer("thunderhead", instructions="Talk to the human (via Discord) and to other Claude sessions.")

STATES = ("working", "blocked", "done")


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
    """Send a message to another Claude session by name.

    It is delivered when that session's turn ends. A sleeping session is woken to receive it.
    """
    with store.db() as conn:
        me = _me(conn)
        target = store.session_by_name(conn, to)
        if target is None or target["status"] in store.DEAD:
            names = ", ".join(s["name"] for s in store.live_sessions(conn) if s["id"] != me["id"])
            why = "was stopped by the human" if target and target["status"] == "stopped" else "isn't running"
            return (f"'{to}' {why}; only the human can restart it. "
                    f"Sessions you can message: {names or 'none'}.")
        if target["id"] == me["id"]:
            return "That is you."
        hops = me["current_hops"] + 1
        if hops > MAX_HOPS:
            return (f"Refused: this conversation between agents has gone {MAX_HOPS} hops without the human. "
                    "Use report() to ask the human how to proceed.")
        store.queue_message(conn, target["id"], "agent", me["name"], message, hops=hops)
        store.post(conn, me["id"], "agent_msg", f"📤 to **{target['name']}** (hop {hops}): {message}")
        store.post(conn, target["id"], "agent_msg_in", f"📥 from **{me['name']}** (hop {hops}): {message}")
    if target["status"] == store.SLEEPING:
        return f"Queued for {to}. It's asleep and will be woken to receive it (about 20 seconds)."
    return f"Queued for {to}."


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
    return format_messages(rows) if rows else "No new messages."


def main():
    mcp.run("stdio")
