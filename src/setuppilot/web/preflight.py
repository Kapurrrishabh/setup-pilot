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
    # The internet, the folder and a configured database stop a run; Docker only in Docker mode.
    checks = [
        _check(
            "internet",
            "Internet (GitHub)",
            _can_reach("https://api.github.com"),
            "Check your connection.",
            is_needed=True,
        ),
        _check(
            "groq",
            "Groq API key",
            bool(os.environ.get("GROQ_API_KEY")),
            "Add GROQ_API_KEY to .env. Without it, README tips and the AI report are skipped.",
            is_needed=False,
        ),
        _database_check(stores),
        _check(
            "docker",
            "Docker (for Docker mode)",
            bool(probe.run(TOOLS["docker engine"][0]).output),
            "Start Docker (Docker Desktop, or colima start) if you want Docker mode.",
            is_needed=False,
        ),
    ]
    if folder:
        path = Path(folder).expanduser()
        is_writable = path.is_dir() and os.access(path, os.W_OK)
        fix = f"{path} must exist and be writable."
        checks.append(_check("folder", "Folder can be written", is_writable, fix, is_needed=True))
    return checks


def _database_check(stores: Stores) -> dict[str, Any]:
    name = "Supabase database"
    if stores.database_warning:
        return _check("database", name, False, stores.database_warning, is_needed=False)
    try:
        stores.runs.list_runs(1)
    except DatabaseUnavailable as error:
        fix = f"{error.message} {error.details}"
        return _check("database", name, False, fix, is_needed=True)
    return _check("database", name, True, "", is_needed=False)


def _check(check_id: str, name: str, is_ok: bool, fix: str, *, is_needed: bool) -> dict[str, Any]:
    return {
        "id": check_id,
        "name": name,
        "is_ok": is_ok,
        "is_needed": is_needed,
        "fix": "" if is_ok else fix,
    }


def _can_reach(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=3).close()  # noqa: S310 - fixed https URL
    except urllib.error.HTTPError:
        return True  # any HTTP answer (even "rate limited") means the internet works
    except OSError:
        return False
    return True
