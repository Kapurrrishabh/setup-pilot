"""List a repo's files. Never follows symlinks; skips folders of installed or built files."""

import os
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".gradle",
        ".next",
        "dist",
        "build",
        "target",
    }
)
# Folders a repo should never ship, because tools would run what is inside them.
_UNTRUSTED_IF_COMMITTED = SKIP_DIRS & {"node_modules", ".venv", "venv"}


@dataclass(frozen=True)
class RepoScan:
    root: Path
    files: tuple[str, ...]
    skipped_symlinks: tuple[str, ...]
    skipped_dirs: tuple[str, ...]

    def tree_summary(self, max_chars: int) -> str:
        """Shallow files first, cut at max_chars, so it fits in a small prompt."""
        ordered = sorted(self.files, key=lambda f: (f.count("/"), f))
        lines: list[str] = []
        used = 0
        for path in ordered:
            # Odd characters in file names (like a newline) are shown escaped, not obeyed.
            shown = path if path.isprintable() else repr(path)
            if used + len(shown) + 1 > max_chars:
                lines.append(f"... and {len(ordered) - len(lines)} more files")
                break
            lines.append(shown)
            used += len(shown) + 1
        return "\n".join(lines)


def scan_repo(root: Path) -> RepoScan:
    if not root.is_dir():
        raise NotADirectoryError(f"Cannot scan {root}: it is not a folder.")
    files, symlinks = [], []
    for folder, dirs, names in os.walk(root):
        here = Path(folder)
        for name in dirs + names:
            if _is_link(here / name):
                symlinks.append((here / name).relative_to(root).as_posix())
        # Skipped folders are still searched for links: git checks out a link in build/ too.
        # os.walk goes into Windows junctions, so they are taken out here.
        dirs[:] = sorted(d for d in dirs if d != ".git" and not (here / d).is_junction())
        if not SKIP_DIRS.isdisjoint(here.relative_to(root).parts):
            continue
        files += [
            (here / n).relative_to(root).as_posix() for n in sorted(names) if not _is_link(here / n)
        ]
    committed = tuple(sorted(d for d in _UNTRUSTED_IF_COMMITTED if (root / d).is_dir()))
    return RepoScan(root, tuple(files), tuple(sorted(symlinks)), committed)


def _is_link(path: Path) -> bool:
    """A Windows junction points elsewhere like a symlink, but is_symlink() says no to it."""
    return path.is_symlink() or path.is_junction()
