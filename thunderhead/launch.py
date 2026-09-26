"""Building `claude` command lines for fleet sessions. Used by the bot and The ThunderHead's tools."""
import json
import re
import subprocess

from . import db as store
from .config import BRIEFS, MCP_FILE, MEMORY_ROOT, SETTINGS_FILE

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
HQ = MEMORY_ROOT / "hq"
NOTES = HQ / "NOTES.md"

GENERATED = ("<!-- Generated from {src} each time this session starts. "
             "Edit that file in the THUNDERHEAD repo, not this copy. -->\n\n")
MEMORY_GITIGNORE = "# Generated copies of the briefs in the THUNDERHEAD repo\nCLAUDE.md\n"

LEAD_BOOT = """[thunderhead] You are The ThunderHead, starting fresh. Your earlier conversation was wiped.
Read NOTES.md now: it's your memory. Then call fleet() to see the current state, post a two-line \
"back online" summary with report(), and handle the message below if there is one."""

NOTES_TEMPLATE = """# ThunderHead notes

Your memory across wipes. Keep it short and current: rewrite stale parts instead of only appending.

## Session roster

<!-- One entry per session:
### name (folder)
- Doing: current task and state
- Knows: what it has in context (repo, files, systems, decisions)
- Give it: the kinds of tasks to delegate to it
-->

## Standing instructions from the human

## Channels and what they're for

## Decisions and open threads
"""


def bg_command(name: str, resume: str | None = None, role: str = "worker") -> list[str]:
    """`claude --bg` connected to the fleet, listening, with approvals sent to Discord.

    `--resume` has to come straight after `--bg`, and `--mcp-config` takes several values,
    so it's passed as `--mcp-config=` to stop it swallowing the prompt that follows.
    """
    settings = json.loads(SETTINGS_FILE.read_text())
    settings["env"] = {"THUNDERHEAD_NAME": name, "THUNDERHEAD_LISTEN": "1",
                       "THUNDERHEAD_REMOTE_APPROVAL": "1", "THUNDERHEAD_ROLE": role}
    if role == "lead":
        # Its notebook is the one file The ThunderHead edits without asking; any other edit
        # goes to the human for approval.
        notes = f"//{NOTES.as_posix().lstrip('/')}"
        settings["permissions"]["allow"] += [f"Edit({notes})", f"Write({notes})"]
    cmd = ["claude", "--bg"] + (["--resume", resume] if resume else [])
    return cmd + ["-n", name, "--settings", json.dumps(settings), f"--mcp-config={MCP_FILE}"]


def ensure_memory() -> None:
    """Create the memory repo on first use."""
    MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    if not (MEMORY_ROOT / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=MEMORY_ROOT, check=True)
        (MEMORY_ROOT / ".gitignore").write_text(MEMORY_GITIGNORE)
        (MEMORY_ROOT / "README.md").write_text(
            "# THUNDERHEAD memory\n\nNotes and team charters written by the fleet's lead sessions. "
            "The THUNDERHEAD bot commits snapshots here; roll back with git if notes get garbled.\n")


def install_brief(folder, brief: str) -> None:
    """Write the brief as the folder's CLAUDE.md, so Claude Code loads it for a session working there."""
    src = BRIEFS / brief
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "CLAUDE.md").write_text(GENERATED.format(src=src) + src.read_text())


def lead_command(first_message: str | None = None) -> tuple[list[str], str]:
    """A fresh ThunderHead session: (command, working directory)."""
    ensure_memory()
    install_brief(HQ, "thunderhead.md")
    if not NOTES.exists():
        NOTES.write_text(NOTES_TEMPLATE)
    prompt = LEAD_BOOT + (f"\n\n--- from the human (Discord) ---\n{first_message}" if first_message else "")
    return bg_command(store.LEAD, role="lead") + [prompt], str(HQ)


def run(cmd: list[str], cwd=None, timeout=60) -> tuple[int, str]:
    """Synchronous run for the MCP server (the bot has an async twin)."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, f"`{' '.join(cmd[:2])}` didn't return within {timeout}s."
    return p.returncode, (p.stdout + p.stderr).strip()[-1500:]
