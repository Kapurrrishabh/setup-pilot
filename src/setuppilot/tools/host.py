"""Facts about the computer SetupPilot runs on: which system it is, and Windows' saved PATH."""

import ntpath
import os
import sys
from pathlib import Path
from typing import Literal

from setuppilot.guardrails.commands import ExecTarget

# Linux is split by family because that picks the install command (apt or dnf).
HostOs = Literal["windows", "macos", "debian", "fedora", "linux"]
OS_LABELS: dict[HostOs, str] = {
    "windows": "Windows",
    "macos": "macOS",
    "debian": "Linux (Debian or Ubuntu)",
    "fedora": "Linux (Fedora or Red Hat)",
    "linux": "Linux",
}
_ENVIRONMENT_KEYS = (
    ("HKEY_LOCAL_MACHINE", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ("HKEY_CURRENT_USER", "Environment"),
)


def host_os(host: ExecTarget) -> HostOs:
    if host == "windows":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    try:
        return linux_family(Path("/etc/os-release").read_text(encoding="utf-8"))
    except OSError:
        return "linux"


def linux_family(os_release: str) -> HostOs:
    ids: set[str] = set()
    for line in os_release.splitlines():
        key, _, value = line.partition("=")
        if key in ("ID", "ID_LIKE"):
            ids |= set(value.strip().strip("\"'").lower().split())
    if ids & {"debian", "ubuntu"}:
        return "debian"
    if ids & {"fedora", "rhel", "centos"}:
        return "fedora"
    return "linux"


def with_new_entries(current: str, saved: list[str]) -> str:
    """Windows PATH as it is now, then the saved entries it lacks, so a tool found now is still
    the one found. Only full paths are kept: a relative one would search the repo."""
    entries = [p for p in current.split(";") if p]
    seen = {p.lower() for p in entries}
    for entry in saved:
        if entry and entry.lower() not in seen and ntpath.isabs(entry) and entry[:1] not in "\\/":
            entries.append(entry)
            seen.add(entry.lower())
    return ";".join(entries)


def refresh_path() -> None:
    """winget saves PATH changes in the registry only, so this process would not find a tool it
    has just installed. Windows only: elsewhere the installers use folders already on PATH."""
    if sys.platform != "win32":
        return
    import winreg

    saved: list[str] = []
    for root, key in _ENVIRONMENT_KEYS:
        try:
            with winreg.OpenKey(getattr(winreg, root), key) as opened:
                value, _ = winreg.QueryValueEx(opened, "Path")
        except FileNotFoundError:
            continue
        saved += winreg.ExpandEnvironmentStrings(value).split(";")
    os.environ["PATH"] = with_new_entries(os.environ.get("PATH", ""), saved)


def add_to_path(folder: str) -> None:
    """Add a folder to the end of this process's PATH, so the next checks and commands find what
    was just installed there. Only a full path is added: a relative one would search the repo."""
    entries = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if os.path.isabs(folder) and folder not in entries:
        os.environ["PATH"] = os.pathsep.join([*entries, folder])
