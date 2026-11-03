"""Pick the repo files the chatbot may learn from, as plain text documents."""

import re
from dataclasses import dataclass
from pathlib import Path

from setuppilot.tools.compose import COMPOSE_NAMES
from setuppilot.tools.scan import RepoScan

# Matched by how a file name starts, so LICENSE-MIT and CHANGES.rst count too.
_DOC_NAMES = ("readme", "contributing", "license", "licence", "code_of_conduct", "security",
              "changelog", "changes", "history", "authors", "copying", "notice")  # fmt: skip
_MANIFESTS = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "go.mod", "cargo.toml",
    "pom.xml", "build.gradle", "build.gradle.kts", "gemfile", "makefile", "dockerfile",
    ".env.example", "tox.ini", "noxfile.py", "manage.py", *COMPOSE_NAMES,
}  # fmt: skip
_LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
               "cargo.lock", "gemfile.lock", "composer.lock", "pipfile.lock", "go.sum"}  # fmt: skip
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_CONFIG_SUFFIXES = (".toml", ".yaml", ".yml", ".ini", ".cfg")
_CODE_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java", ".kt",
                  ".c", ".h", ".cpp", ".cs", ".php", ".swift", ".sh")  # fmt: skip
# For source code only the first lines are kept: they usually say what the file is for.
_CODE_HEADER_LINES = 12


@dataclass(frozen=True)
class Document:
    path: str
    text: str


def collect_documents(scan: RepoScan, max_file_bytes: int, tree_chars: int) -> list[Document]:
    documents = [Document("(file tree)", scan.tree_summary(tree_chars))]
    for path in scan.files:
        kind = _kind(path)
        if kind is None:
            continue
        text = _read_text(scan.root / path, max_file_bytes)
        if text is None:
            continue
        if kind == "code":
            text = "\n".join(text.splitlines()[:_CODE_HEADER_LINES])
        if text.strip():
            documents.append(Document(path, text))
    return documents


def is_code_path(path: str) -> bool:
    return _kind(path) == "code"


def _kind(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1].lower()
    if name in _LOCK_FILES:
        return None
    if name.endswith(_CODE_SUFFIXES) and name not in _MANIFESTS:
        return "code"
    # Doc names (README, LICENSE-MIT, CHANGES.rst) count at the top level, in docs/, and in
    # .github/, where GitHub keeps SECURITY.md and CONTRIBUTING.md.
    is_top_or_docs = "/" not in path or path.lower().startswith(("docs/", "doc/", ".github/"))
    if (
        (is_top_or_docs and name.startswith(_DOC_NAMES))
        or name in _MANIFESTS
        or re.fullmatch(r".*requirements.*\.txt", name)
    ):
        return "doc"
    if path.lower().startswith(("docs/", "doc/")) and name.endswith(_DOC_SUFFIXES):
        return "doc"
    if path.startswith(".github/workflows/") or (
        "/" not in path and name.endswith(_CONFIG_SUFFIXES)
    ):
        return "doc"
    return None


def _read_text(path: Path, max_bytes: int) -> str | None:
    """Text of a small, non-binary file, or None to skip it."""
    if path.stat().st_size > max_bytes:
        return None
    data = path.read_bytes()
    if b"\x00" in data:
        return None
    return data.decode("utf-8", errors="replace")
