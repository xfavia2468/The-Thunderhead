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
PERSONALITY = HQ / "PERSONALITY.md"
TEAMS = MEMORY_ROOT / "teams"

GENERATED = ("<!-- Generated from {src} each time this session starts. "
             "Edit that file in the THUNDERHEAD repo, not this copy. -->\n\n")
MEMORY_GITIGNORE = "# Generated copies of the briefs in the THUNDERHEAD repo\nCLAUDE.md\n"

LEAD_BOOT = """[thunderhead] You are The ThunderHead, starting fresh. Your earlier conversation was wiped.
Read NOTES.md now: it's your memory. Then call fleet() to see the current state, post a two-line \
"back online" summary with report(), and handle the message below if there is one."""

SUPERVISOR_BOOT = """[thunderhead] You are the supervisor of team '{team}', starting fresh.
Your charter (in your CLAUDE.md and CHARTER.md) is a draft The ThunderHead wrote before anyone looked \
at the code. Get to know the product at the level of its architecture: read the repositories' own \
CLAUDE.md and READMEs and the code's structure ({repos}), not every file, and write a map of the \
product into NOTES.md. Then refine the charter against what you found and propose it to the human with \
propose_charter(). Finally, report() a short introduction: what the product is and what you'd do first."""

CHARTER_TEMPLATE = """## Scope
What this team owns, and just as important, what it doesn't.

## Goals and priorities

## Definition of done
The quality bar: which tests must pass, what review a change needs.

## Constraints
Things to never do or touch (production, other teams' code, spending, ...).

## Interfaces
Which other teams this one depends on, and which depend on it.

## The human's preferences for this product
"""

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

PERSONALITY_HEADER = """# The ThunderHead's personality

<!-- System personality: written by the human in briefs/thunderhead-personality.md (THUNDERHEAD
     repo) and copied here at every start, so edits to it here are overwritten.
     Dynamic personality: yours to grow as you learn about the human and yourself. -->
"""

SYSTEM_HEADING = "## System personality"
DYNAMIC_HEADING = "## Dynamic personality"

DYNAMIC_TEMPLATE = """
<!-- How to talk with the human, learned over time: what they respond well to, what grates,
     their sense of humor, how much detail they want, and who you've become in working with
     them. Facts about work and instructions go in NOTES.md instead. Keep it under ~40 lines. -->
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
               team: str | None = None, add_dirs=(), dev: bool = False) -> list[str]:
    """`claude --bg` connected to the fleet, listening, with approvals sent to Discord.

    `--resume` has to come straight after `--bg`, and `--mcp-config`/`--add-dir` take several
    values, so they're passed with `=` to stop them swallowing the prompt that follows.
    """
    settings = json.loads(SETTINGS_FILE.read_text())
    settings["env"] = {"THUNDERHEAD_NAME": name, "THUNDERHEAD_LISTEN": "1",
                       "THUNDERHEAD_REMOTE_APPROVAL": "1", "THUNDERHEAD_ROLE": role}
    if role == "lead":
        _allow_edits(settings, NOTES, PERSONALITY)
    elif role == "supervisor" and team:
        _allow_edits(settings, team_folder(team) / "NOTES.md")
        # The product repos' own CLAUDE.md is the best description of the product; Claude Code
        # only loads it from --add-dir folders with this set.
        settings["env"]["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] = "1"
    cmd = ["claude", "--bg"] + (["--resume", resume] if resume else [])
    cmd += ["-n", name, "--settings", json.dumps(settings), f"--mcp-config={MCP_FILE}"]
    if dev:
        # How to be a dev in the fleet. A system-prompt addition rather than a CLAUDE.md, so the
        # product repo stays untouched and it survives compaction.
        cmd.append(f"--append-system-prompt-file={BRIEFS / 'dev.md'}")
    return cmd + [f"--add-dir={d}" for d in add_dirs]


def relaunch_command(conn, sess, resume: str | None = None) -> list[str]:
    """The command that brings an existing session back with the same role, team and folders."""
    team = store.team_of(conn, sess["name"])
    if team and team["supervisor"] == sess["name"]:
        prepare_supervisor(team)
        return bg_command(sess["name"], resume, role="supervisor", team=team["name"],
                          add_dirs=json.loads(team["repos"]))
    if team:
        return bg_command(sess["name"], resume, role=sess["role"], dev=True)
    if sess["role"] == "lead":
        install_lead()
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


def install_brief(folder, brief: str, extra: str = "") -> None:
    """Write the brief as the folder's CLAUDE.md, so Claude Code loads it for a session working there."""
    src = BRIEFS / brief
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "CLAUDE.md").write_text(GENERATED.format(src=src) + src.read_text() + extra)


def sync_personality() -> str:
    """Refresh PERSONALITY.md: the system section from the brief, the dynamic section kept as written.

    Returns the file's text, for inlining into The ThunderHead's CLAUDE.md.
    """
    system = re.sub(r"<!--.*?-->", "", (BRIEFS / "thunderhead-personality.md").read_text(), flags=re.S).strip()
    dynamic = DYNAMIC_TEMPLATE
    if PERSONALITY.exists():
        current = PERSONALITY.read_text()
        if DYNAMIC_HEADING in current:
            dynamic = current.split(DYNAMIC_HEADING, 1)[1]
    text = f"{PERSONALITY_HEADER}\n{SYSTEM_HEADING}\n\n{system}\n\n{DYNAMIC_HEADING}\n\n{dynamic.strip()}\n"
    PERSONALITY.write_text(text)
    return text


def install_lead() -> None:
    """The ThunderHead's folder: brief with its personality inlined, and its notes."""
    ensure_memory()
    HQ.mkdir(parents=True, exist_ok=True)
    personality = sync_personality()
    install_brief(HQ, "thunderhead.md",
                  extra="\n\n---\n\n# Your personality (from PERSONALITY.md, as of this start)\n\n"
                        + personality.split("\n", 1)[1])
    if not NOTES.exists():
        NOTES.write_text(NOTES_TEMPLATE)


def write_charter(team: str, text: str) -> None:
    folder = team_folder(team)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "CHARTER.md").write_text(f"# Team {team}: charter\n\n{text.strip()}\n")


def prepare_supervisor(team) -> Path:
    """Set up a team's memory folder: the brief with the charter and settings inlined, and notes.

    `team` is its row in the store. The charter and settings go into CLAUDE.md so they're always
    in context; NOTES.md stays a file the supervisor reads, since it changes constantly.
    """
    ensure_memory()
    folder = team_folder(team["name"])
    charter_file = folder / "CHARTER.md"
    charter = charter_file.read_text().split("\n", 1)[-1].strip() if charter_file.exists() else "(none yet)"
    status = ("APPROVED by the human" if team["charter_status"] == "approved"
              else "DRAFT: refine it and propose it to the human with propose_charter()")
    install_brief(folder, "supervisor.md", extra=(
        f"\n\n---\n\n# Your team: {team['name']}\n\n"
        f"## Settings (set by the human or The ThunderHead)\n\n"
        f"- **Autonomy: {team['autonomy']}.** " + (
            "Pick up work from your backlog on your own, within your charter."
            if team["autonomy"] == "act" else
            "Only work on what you're given. When a task is done, propose what to do next and wait for a yes.")
        + f"\n- **Max devs: {team['max_devs']}.** Requests beyond this are refused; ask The ThunderHead "
          "with a reason if the team needs more.\n\n"
        f"## Charter ({status})\n\n{charter}\n"))
    if not (folder / "NOTES.md").exists():
        (folder / "NOTES.md").write_text(SUPERVISOR_NOTES_TEMPLATE.format(team=team["name"]))
    return folder


def lead_command(first_message: str | None = None) -> tuple[list[str], str]:
    """A fresh ThunderHead session: (command, working directory)."""
    install_lead()
    prompt = LEAD_BOOT + (f"\n\n--- from the human (Discord) ---\n{first_message}" if first_message else "")
    return bg_command(store.LEAD, role="lead") + [prompt], str(HQ)


def supervisor_command(team) -> tuple[list[str], str]:
    """A fresh supervisor session for a team (its row in the store): (command, working directory)."""
    folder = prepare_supervisor(team)
    repos = json.loads(team["repos"])
    prompt = SUPERVISOR_BOOT.format(team=team["name"], repos=", ".join(repos))
    return (bg_command(team["supervisor"], role="supervisor", team=team["name"], add_dirs=repos) + [prompt],
            str(folder))


def run(cmd: list[str], cwd=None, timeout=60) -> tuple[int, str]:
    """Synchronous run for the MCP server (the bot has an async twin)."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, f"`{' '.join(cmd[:2])}` didn't return within {timeout}s."
    return p.returncode, (p.stdout + p.stderr).strip()[-1500:]
