"""The pre-flight checklist on the setup page: is everything ready before a run starts?"""

import os
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from setuppilot.db.stores import Stores
from setuppilot.errors import DatabaseUnavailable
from setuppilot.guardrails.runner import HOST_TARGET
from setuppilot.tools.detect import StackCandidate
from setuppilot.tools.prereqs import ToolProbe, check_prerequisites

_LONG_PATHS_KEY = r"SYSTEM\CurrentControlSet\Control\FileSystem"


@dataclass(frozen=True)
class WindowsFacts:
    """Windows settings the checks read. Passed in, so the checks can be tested anywhere."""

    onedrive_folders: tuple[Path, ...]
    is_long_paths_enabled: bool


def windows_facts() -> WindowsFacts | None:
    """None on macOS and Linux, where the Windows checks do not apply."""
    if sys.platform != "win32":
        return None
    import winreg

    # OneDrive sets these for a personal account and for a work or school account.
    names = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
    folders = tuple(Path(os.environ[n]) for n in names if os.environ.get(n))
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _LONG_PATHS_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
    except FileNotFoundError:
        value = 0  # Windows leaves the value out until someone turns long paths on.
    return WindowsFacts(folders, value == 1)


def is_writable_folder(path: Path) -> bool:
    """Writes a real file: on Windows os.access says yes for every folder, even read-only ones."""
    if not path.is_dir():
        return False
    try:
        with tempfile.TemporaryFile(dir=path):
            return True
    except OSError:
        return False


def run_preflight(
    stores: Stores, probe: ToolProbe, folder: str, windows: WindowsFacts | None
) -> list[dict[str, Any]]:
    """windows is None when SetupPilot does not run on Windows."""
    # The internet, the folder and a configured database stop a run; Docker only in Docker mode.
    # The same check that stops a Docker mode run, so the two cannot disagree.
    docker = StackCandidate("docker", "dockerfile", (), None)
    docker_checks = check_prerequisites(docker, probe, HOST_TARGET)
    docker_problems = [c.problem for c in docker_checks if not c.is_installed and c.problem]
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
            not docker_problems,
            # Only the first: Docker that is not installed cannot be running either.
            next(iter(docker_problems), ""),
            is_needed=False,
        ),
    ]
    if windows is not None:
        fix = (
            "Windows limits paths to 260 characters, and deep node_modules folders go past it. "
            "Turn long paths on (needs admin): "
            "https://docs.python.org/3/using/windows.html#removing-the-max-path-limitation"
        )
        is_on = windows.is_long_paths_enabled
        checks.append(_check("long_paths", "Long file paths", is_on, fix, is_needed=False))
    if folder:
        path = Path(folder).expanduser()
        fix = f"{path} must exist and be writable."
        is_writable = is_writable_folder(path)
        checks.append(_check("folder", "Folder can be written", is_writable, fix, is_needed=True))
        if windows is not None:
            real = path.resolve()
            is_synced = any(real.is_relative_to(f.resolve()) for f in windows.onedrive_folders)
            fix = (
                "OneDrive locks files while it syncs them, which makes installs fail. Pick a "
                "folder outside OneDrive, like C:\\dev."
            )
            checks.append(
                _check(
                    "onedrive", "Folder is outside OneDrive", not is_synced, fix, is_needed=False
                )
            )
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
