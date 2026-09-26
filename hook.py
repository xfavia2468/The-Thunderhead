#!/usr/bin/env python3
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from thunderhead.hooks import main  # noqa: E402
except Exception:
    # A broken import must not break the session either: log it where hook errors go, exit quietly.
    log = ROOT / "data" / "hook-errors.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(f"--- {time.ctime()} import failed ({' '.join(sys.argv[1:])})\n{traceback.format_exc()}\n")
    sys.exit(0)

main()
