#!/usr/bin/env python3
"""Write config/settings.json and config/mcp.json with this checkout's absolute paths."""
import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from thunderhead.config import APPROVAL_SECONDS, LISTEN_SECONDS, MCP_FILE, PYTHON, ROOT, SETTINGS_FILE  # noqa: E402

# Event -> hook timeout in seconds. Stop and PermissionRequest wait on you, so they get long ones.
TIMEOUTS = {
    "SessionStart": 30,
    "UserPromptSubmit": 30,
    "Stop": LISTEN_SECONDS + 300,
    "Notification": 30,
    "PermissionRequest": APPROVAL_SECONDS + 120,
    "SessionEnd": 30,
}

hook = f"{shlex.quote(str(PYTHON))} {shlex.quote(str(ROOT / 'hook.py'))}"
settings = {
    "permissions": {"allow": ["mcp__thunderhead"]},
    "hooks": {
        event: [{"hooks": [{"type": "command", "command": f"{hook} {event}", "timeout": timeout}]}]
        for event, timeout in TIMEOUTS.items()
    },
}
mcp = {"mcpServers": {"thunderhead": {"command": str(PYTHON), "args": [str(ROOT / "mcp_server.py")]}}}

SETTINGS_FILE.parent.mkdir(exist_ok=True)
SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n")
MCP_FILE.write_text(json.dumps(mcp, indent=2) + "\n")
print(f"Wrote {SETTINGS_FILE}\nWrote {MCP_FILE}")
