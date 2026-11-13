"""Pick the repo files the chatbot may learn from, as plain text documents, from a clone or from
files read from GitHub."""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from setuppilot.github.api import TreeEntry
from setuppilot.tools.compose import COMPOSE_NAMES
from setuppilot.tools.scan import SKIP_DIRS, RepoScan, decode_text

DocumentKind = Literal["doc", "manifest", "ci", "code", "test", "tree"]
TREE_PATH = "(file tree)"

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
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test[_.]|_test\.\w+$|\.(test|spec)\.\w+$"
)
# Code copied in from other projects says nothing about this repo.
_VENDORED = frozenset({"vendor", "third_party"})
# Minified or generated text: lines this long on average are not worth reading.
_GENERATED_LINE_CHARS = 1000


@dataclass(frozen=True)
class Document:
    path: str
    text: str
    kind: DocumentKind


def collect_documents(scan: RepoScan, max_file_bytes: int, tree_chars: int) -> list[Document]:
    documents = [Document(TREE_PATH, scan.tree_summary(tree_chars), "tree")]
    for path in scan.files:
        kind = file_kind(path)
        if kind is None or (scan.root / path).stat().st_size > max_file_bytes:
            continue
        if (document := _document(path, kind, (scan.root / path).read_bytes())) is not None:
            documents.append(document)
    return documents


def documents_from_snapshot(
    entries: Iterable[TreeEntry], files: Mapping[str, str], tree_chars: int
) -> list[Document]:
    """The documents collect_documents makes from a clone: the tree lists what scan_repo would
    (no links, nothing in skipped folders)."""
    paths = tuple(
        e.path for e in entries if not e.is_link and SKIP_DIRS.isdisjoint(e.path.split("/")[:-1])
    )
    tree = RepoScan(Path(), paths, (), ()).tree_summary(tree_chars)
    documents = [Document(TREE_PATH, tree, "tree")]
    for path, text in sorted(files.items()):
        kind = file_kind(path)
        if kind is not None and (document := _document(path, kind, text.encode())) is not None:
            documents.append(document)
    return documents


def file_kind(path: str) -> DocumentKind | None:
    """What a repo file is to the chatbot, or None to leave it out."""
    lower = path.lower()
    *folders, name = lower.split("/")
    if name in _LOCK_FILES or ".min." in name or not _VENDORED.isdisjoint(folders):
        return None
    if name.endswith(_CODE_SUFFIXES) and name not in _MANIFESTS:
        return "test" if _TEST_PATH.search(lower) else "code"
    if path.startswith(".github/workflows/"):
        return "ci"
    if (
        name in _MANIFESTS
        or re.fullmatch(r".*requirements.*\.txt", name)
        or (not folders and name.endswith(_CONFIG_SUFFIXES))
    ):
        return "manifest"
    # Doc names (README, LICENSE-MIT, CHANGES.rst) count at the top level, in docs/, and in
    # .github/, where GitHub keeps SECURITY.md and CONTRIBUTING.md.
    is_top_or_docs = not folders or lower.startswith(("docs/", "doc/", ".github/"))
    if (is_top_or_docs and name.startswith(_DOC_NAMES)) or (
        lower.startswith(("docs/", "doc/")) and name.endswith(_DOC_SUFFIXES)
    ):
        return "doc"
    return None


def _document(path: str, kind: DocumentKind, data: bytes) -> Document | None:
    """The file as text, or None when it is binary, empty or generated."""
    text = decode_text(data)
    if (
        text is None
        or not text.strip()
        or len(text) / (text.count("\n") + 1) > _GENERATED_LINE_CHARS
    ):
        return None
    return Document(path, text, kind)
