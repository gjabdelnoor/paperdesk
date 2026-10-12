"""Read the existing Linux archive timer's status without operating it."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def read_archive_status(directory: Path | None = None, *, now: float | None = None) -> dict:
    if directory is None:
        if sys.platform != "linux":
            return {"available": False, "active": False,
                    "message": "The existing archive-sync timer is a Linux integration."}
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        if not base.is_absolute():
            return {"available": False, "active": False, "message": "The state directory is not absolute."}
        directory = base / "remarkable-archive-sync"
    path = directory / "status.json"
    if not path.exists():
        return {"available": False, "active": False,
                "message": "No existing archive-sync status was found. This does not start or install the timer."}
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError()
        with path.open("rb") as handle:
            data = json.loads(handle.read(65537))
        if not isinstance(data, dict):
            raise ValueError()
        updated = data.get("updated_at", 0)
        if isinstance(updated, bool) or not isinstance(updated, (int, float)):
            raise ValueError()
        current = time.time() if now is None else now
        stale = not 0 <= current - updated <= 600
        message = data.get("message", "")
        if not isinstance(message, str):
            raise ValueError()
        result = {"available": True, "active": data.get("active") is True and not stale,
                  "stale": stale, "large": data.get("large") is True and not stale,
                  "updated_at": updated, "message": message[:1000],
                  "bytes_done": 0, "bytes_total": 0}
        for key in ("bytes_done", "bytes_total"):
            value = data.get(key) or 0
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 2**63:
                raise ValueError()
            result[key] = int(value)
        if stale:
            result["message"] = "The last archive-sync status is more than 10 minutes old; current activity is unknown."
        elif result["active"] and result["large"] and not result["message"]:
            result["message"] = "Large sync — connect USB-C to speed up transfers."
        elif not result["message"]:
            result["message"] = "Archive sync is working." if result["active"] else "Archive sync is idle."
        return result
    except (OSError, ValueError, TypeError, OverflowError):
        return {"available": True, "active": False, "invalid": True,
                "message": "The archive-sync status is unreadable. Its timer and files were left alone."}
