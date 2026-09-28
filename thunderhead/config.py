"""Paths and settings shared by the hooks, the MCP server and the bot."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "bin" / "python"
SETTINGS_FILE = ROOT / "config" / "settings.json"
MCP_FILE = ROOT / "config" / "mcp.json"


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DB_PATH = Path(os.environ.get("THUNDERHEAD_DB", ROOT / "data" / "thunderhead.db"))

# Briefs (how each kind of lead session behaves) are code and live here, versioned
# with it. Memory (notes, team charters) is state, written by agents, and lives in
# its own git repo. The bot snapshots that repo; agents never commit to it.
BRIEFS = ROOT / "briefs"
MEMORY_ROOT = Path(os.environ.get("THUNDERHEAD_MEMORY", Path.home() / "thunderhead-memory")).expanduser()
# Empty working folders for devs and one-offs with no repository to work in. It must be outside
# any repo or folder with a CLAUDE.md, so they load nobody else's brief.
WORKSPACES = Path(os.environ.get("THUNDERHEAD_WORKSPACES", Path.home() / "thunderhead-workspaces")).expanduser()
MEMORY_SNAPSHOT_SECONDS = int(os.environ.get("THUNDERHEAD_MEMORY_SNAPSHOT_SECONDS", 10 * 60))

# How long a listening session's Stop hook waits for a message before the
# session goes to sleep. The bot then shuts it down and wakes it on the next message.
LISTEN_SECONDS = int(os.environ.get("THUNDERHEAD_LISTEN_SECONDS", 30 * 60))
# How long a PermissionRequest waits for a Discord button before falling back
# to the normal terminal prompt.
APPROVAL_SECONDS = int(os.environ.get("THUNDERHEAD_APPROVAL_SECONDS", 15 * 60))
# Messages carry a hop count; going up or sideways in the org chart adds one, going down
# adds none. Past this, send() and post() refuse until the human writes.
MAX_HOPS = int(os.environ.get("THUNDERHEAD_MAX_HOPS", 10))
POLL_SECONDS = 1.5

# Team defaults, and a fleet-wide ceiling on running sessions that only the human sets.
# Per-team caps alone don't bound the total, since The Thunderhead can create teams.
MAX_SESSIONS = int(os.environ.get("THUNDERHEAD_MAX_SESSIONS", 10))
# Models, by alias so they track the latest release. Supervisors hold the durable product
# knowledge and make the design calls, so they run the strongest. The Thunderhead mostly routes
# and tracks, with a small context it loses on every wipe, so it defaults to a cheaper one.
# Devs and one-offs default to the cheaper one too.
MODELS = ("haiku", "sonnet", "opus")  # cheapest first
LEAD_MODEL = os.environ.get("THUNDERHEAD_LEAD_MODEL", "sonnet")
SUPERVISOR_MODEL = os.environ.get("THUNDERHEAD_SUPERVISOR_MODEL", "opus")
DEFAULT_DEV_MODEL = os.environ.get("THUNDERHEAD_DEV_MODEL", "sonnet")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Permission mode for sessions the fleet launches. In auto mode Claude Code approves routine
# actions itself and asks only about risky ones, which reach the human as Discord buttons.
PERMISSION_MODE = os.environ.get("THUNDERHEAD_PERMISSION_MODE", "auto")
# One-off sessions The Thunderhead may have running at once (the human's own don't count).
MAX_ONEOFFS = int(os.environ.get("THUNDERHEAD_MAX_ONEOFFS", 2))


def flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")
