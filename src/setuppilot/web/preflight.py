"""The pre-flight checklist on the setup page: is everything ready before a run starts?"""

import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from setuppilot.db.stores import Stores
from setuppilot.errors import DatabaseUnavailable
from setuppilot.tools.prereqs import TOOLS, ToolProbe


def run_preflight(stores: Stores, probe: ToolProbe, folder: str) -> list[dict[str, Any]]:
    checks = [
        _check("Internet (GitHub)", _can_reach("https://api.github.com"), "Check your connection."),
        _check(
            "Groq API key",
            bool(os.environ.get("GROQ_API_KEY")),
            "Add GROQ_API_KEY to .env. Without it, README tips and the AI report are skipped.",
        ),
        _database_check(stores),
        _check(
            "Docker running (only for Docker mode)",
            bool(probe.run(TOOLS["docker engine"][0]).output),
            "Start Docker (Docker Desktop, or colima start) if you want Docker mode.",
        ),
    ]
    if folder:
        path = Path(folder).expanduser()
        is_writable = path.is_dir() and os.access(path, os.W_OK)
        checks.append(
            _check("Folder can be written", is_writable, f"{path} must exist and be writable.")
        )
    return checks


def _database_check(stores: Stores) -> dict[str, Any]:
    if stores.database_warning:
        return _check("Supabase database", False, stores.database_warning)
    try:
        stores.runs.list_runs(1)
    except DatabaseUnavailable as error:
        return _check("Supabase database", False, f"{error.message} {error.details}")
    return _check("Supabase database", True, "")


def _check(name: str, is_ok: bool, fix: str) -> dict[str, Any]:
    return {"name": name, "is_ok": is_ok, "fix": "" if is_ok else fix}


def _can_reach(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=3).close()  # noqa: S310 - fixed https URL
    except urllib.error.HTTPError:
        return True  # any HTTP answer (even "rate limited") means the internet works
    except OSError:
        return False
    return True
