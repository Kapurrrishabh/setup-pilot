"""Keep every path inside one workspace folder."""

import re
from pathlib import Path, PureWindowsPath

from setuppilot.errors import PathBlocked

# On Windows, `nul` or `con.txt` in any folder opens a device, not a file. Refused on every
# system (os.path.isreserved needs Python 3.13), so this rule is tested everywhere.
_DEVICE_NAME = re.compile(r"con|prn|aux|nul|com[0-9]|lpt[0-9]|conin\$|conout\$", re.IGNORECASE)


class PathGuard:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve(strict=True)

    def check(self, path: str | Path) -> Path:
        """Return the real path, following symlinks, or raise if it leaves the workspace."""
        text, raw = str(path), Path(path)
        # Refused here on every system: Windows does not reject a NUL byte the way POSIX does.
        if "\x00" in text:
            raise self._blocked(path, "Paths cannot contain a NUL byte.")
        # Windows rules split on both / and \, so ..\.. is refused on every system too.
        windows = PureWindowsPath(text)
        # Windows reads any two leading slashes, either way round, as a network path.
        if text[:2].replace("/", "\\") == "\\\\":
            raise self._blocked(path, "Network and device paths are not allowed.")
        if ".." in windows.parts or text.startswith("~"):
            raise self._blocked(path, "'..' and '~' are not allowed in paths.")
        if any(_DEVICE_NAME.fullmatch(p.split(".", 1)[0].rstrip(" ")) for p in windows.parts):
            raise self._blocked(path, "Windows device names like nul and con are not allowed.")
        if windows.drive and not raw.is_absolute():
            raise self._blocked(path, "It names a drive but is not a full path on this computer.")
        try:
            real = (raw if raw.is_absolute() else self.workspace / raw).resolve()
        except (OSError, ValueError) as error:
            raise self._blocked(path, f"It could not be read as a path: {error}.") from error
        if not real.is_relative_to(self.workspace):
            raise self._blocked(path, f"It points to {real}, outside the workspace.")
        return real

    def links_that_escape(self, links: tuple[str, ...]) -> list[str]:
        """Repo symlinks that point outside it. Tools may follow them, so the repo is refused."""
        escaping = []
        for link in links:
            target = (self.workspace / link).resolve()
            if not target.is_relative_to(self.workspace):
                escaping.append(f"{link} -> {target}")
        return escaping

    def _blocked(self, path: str | Path, reason: str) -> PathBlocked:
        return PathBlocked(
            "That path is outside the setup folder, so it was blocked.",
            details=f"{str(path)!r}: {reason} Workspace: {self.workspace}",
        )
