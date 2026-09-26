"""Building `claude` command lines for fleet sessions. Used by the bot and the lead sessions' tools."""
import json
import re
import subprocess
from pathlib import Path

from . import db as store
from .config import BRIEFS, MCP_FILE, MEMORY_ROOT, SETTINGS_FILE

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
HQ = MEMORY_ROOT / "hq"
NOTES = HQ / "NOTES.md"
TEAMS = MEMORY_ROOT / "teams"

GENERATED = ("<!-- Generated from {src} each time this session starts. "
             "Edit that file in the THUNDERHEAD repo, not this copy. -->\n\n")
MEMORY_GITIGNORE = "# Generated copies of the briefs in the THUNDERHEAD repo\nCLAUDE.md\n"

LEAD_BOOT = """[thunderhead] You are The ThunderHead, starting fresh. Your earlier conversation was wiped.
Read NOTES.md now: it's your memory. Then call fleet() to see the current state, post a two-line \
"back online" summary with report(), and handle the message below if there is one."""

SUPERVISOR_BOOT = """[thunderhead] You are the supervisor of team '{team}', starting fresh.
Read CHARTER.md and NOTES.md in your folder. Then look over your team's repositories ({repos}) until you \
understand the product well enough to plan work, and write what you learned into NOTES.md. Finally, \
report() a short introduction: what the product is and what you'd do first."""

NOTES_TEMPLATE = """# ThunderHead notes

Your memory across wipes. Keep it short and current: rewrite stale parts instead of only appending.

## Teams

<!-- One entry per team:
### team (supervisor: name)
- Owns: the product or domain, and its repos
- Working on: what the team is doing now
- Route here: the kinds of requests that belong to this team
-->

## Sessions without a team

## Standing instructions from the human

## Channels and what they're for

## Decisions and open threads
"""

SUPERVISOR_NOTES_TEMPLATE = """# {team} supervisor notes

Your memory across wipes and restarts. Keep it current: rewrite stale parts instead of only appending.

## The product

<!-- What it is, how it's built, where things live, what matters. -->

## Dev roster

<!-- One entry per dev:
### name
- Doing: current task and state
- Knows: what it has in context (parts of the code, files, decisions it was part of)
- Give it: the kinds of tasks to delegate to it
-->

## Backlog

## Decisions

<!-- One line each, with a pointer to the documentation that records it. -->
"""


def _allow_edits(settings, *paths: Path) -> None:
    """Let a lead session edit its own notes without an approval prompt."""
    for p in paths:
        rule = f"//{p.as_posix().lstrip('/')}"
        settings["permissions"]["allow"] += [f"Edit({rule})", f"Write({rule})"]


def team_folder(team: str) -> Path:
    return TEAMS / team


def bg_command(name: str, resume: str | None = None, role: str = "worker",
               team: str | None = None, add_dirs=()) -> list[str]:
    """`claude --bg` connected to the fleet, listening, with approvals sent to Discord.

    `--resume` has to come straight after `--bg`, and `--mcp-config`/`--add-dir` take several
    values, so they're passed with `=` to stop them swallowing the prompt that follows.
    """
    settings = json.loads(SETTINGS_FILE.read_text())
    settings["env"] = {"THUNDERHEAD_NAME": name, "THUNDERHEAD_LISTEN": "1",
                       "THUNDERHEAD_REMOTE_APPROVAL": "1", "THUNDERHEAD_ROLE": role}
    if role == "lead":
        _allow_edits(settings, NOTES)
    elif role == "supervisor" and team:
        _allow_edits(settings, team_folder(team) / "NOTES.md")
    cmd = ["claude", "--bg"] + (["--resume", resume] if resume else [])
    cmd += ["-n", name, "--settings", json.dumps(settings), f"--mcp-config={MCP_FILE}"]
    return cmd + [f"--add-dir={d}" for d in add_dirs]


def relaunch_command(conn, sess, resume: str | None = None) -> list[str]:
    """The command that brings an existing session back with the same role, team and folders."""
    team = store.team_of(conn, sess["name"])
    if sess["role"] == "supervisor" and team:
        prepare_supervisor(team["name"])
        return bg_command(sess["name"], resume, role="supervisor", team=team["name"],
                          add_dirs=json.loads(team["repos"]))
    if sess["role"] == "lead":
        ensure_memory()
        install_brief(HQ, "thunderhead.md")
    return bg_command(sess["name"], resume, role=sess["role"])


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


def prepare_supervisor(team: str, charter: str | None = None) -> Path:
    """Set up a team's memory folder: brief, charter and notes."""
    ensure_memory()
    folder = team_folder(team)
    install_brief(folder, "supervisor.md")
    if charter is not None:
        (folder / "CHARTER.md").write_text(f"# Team {team}: charter\n\n{charter.strip()}\n")
    if not (folder / "NOTES.md").exists():
        (folder / "NOTES.md").write_text(SUPERVISOR_NOTES_TEMPLATE.format(team=team))
    return folder


def lead_command(first_message: str | None = None) -> tuple[list[str], str]:
    """A fresh ThunderHead session: (command, working directory)."""
    ensure_memory()
    install_brief(HQ, "thunderhead.md")
    if not NOTES.exists():
        NOTES.write_text(NOTES_TEMPLATE)
    prompt = LEAD_BOOT + (f"\n\n--- from the human (Discord) ---\n{first_message}" if first_message else "")
    return bg_command(store.LEAD, role="lead") + [prompt], str(HQ)


def supervisor_command(team: str, name: str, repos: list[str]) -> tuple[list[str], str]:
    """A fresh supervisor session for a team: (command, working directory)."""
    folder = prepare_supervisor(team)
    prompt = SUPERVISOR_BOOT.format(team=team, repos=", ".join(repos))
    return bg_command(name, role="supervisor", team=team, add_dirs=repos) + [prompt], str(folder)


def run(cmd: list[str], cwd=None, timeout=60) -> tuple[int, str]:
    """Synchronous run for the MCP server (the bot has an async twin)."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, f"`{' '.join(cmd[:2])}` didn't return within {timeout}s."
    return p.returncode, (p.stdout + p.stderr).strip()[-1500:]
