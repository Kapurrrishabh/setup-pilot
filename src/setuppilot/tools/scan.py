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
            if (here / name).is_symlink():
                symlinks.append((here / name).relative_to(root).as_posix())
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        files += [
            (here / n).relative_to(root).as_posix()
            for n in sorted(names)
            if not (here / n).is_symlink()
        ]
    committed = tuple(sorted(d for d in _UNTRUSTED_IF_COMMITTED if (root / d).is_dir()))
    return RepoScan(root, tuple(files), tuple(sorted(symlinks)), committed)
