"""Keep every path inside one workspace folder."""

from pathlib import Path

from setuppilot.errors import PathBlocked


class PathGuard:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve(strict=True)

    def check(self, path: str | Path) -> Path:
        """Return the real path, following symlinks, or raise if it leaves the workspace."""
        raw = Path(path)
        if ".." in raw.parts or str(path).startswith("~"):
            raise self._blocked(path, "'..' and '~' are not allowed in paths.")
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
