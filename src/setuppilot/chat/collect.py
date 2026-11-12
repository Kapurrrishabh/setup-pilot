"""Pick the repo files the chatbot may learn from, as plain text documents, from a clone or from a
GitHub tarball held in memory."""

import gzip
import io
import re
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from setuppilot.errors import CloneFailed
from setuppilot.tools.compose import COMPOSE_NAMES
from setuppilot.tools.scan import SKIP_DIRS, RepoScan

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


@dataclass(frozen=True)
class TarballLimits:
    max_file_bytes: int
    # Caps the unpacked size of the files kept, so a small archive cannot unpack into gigabytes.
    max_total_bytes: int
    tree_chars: int


def collect_documents(scan: RepoScan, max_file_bytes: int, tree_chars: int) -> list[Document]:
    documents = [Document(TREE_PATH, scan.tree_summary(tree_chars), "tree")]
    for path in scan.files:
        kind = file_kind(path)
        if kind is None or (scan.root / path).stat().st_size > max_file_bytes:
            continue
        if (document := _document(path, kind, (scan.root / path).read_bytes())) is not None:
            documents.append(document)
    return documents


def documents_from_tarball(data: bytes, limits: TarballLimits) -> list[Document]:
    """The same documents as collect_documents, read in one pass from a GitHub tarball (.tar.gz)
    in memory. Nothing is written to disk, and links are never followed."""
    paths: list[str] = []
    documents: list[Document] = []
    total = 0
    try:
        # Through GzipFile, not mode r|gz: tarfile alone takes a cut-short download as a
        # normal end, and would index half the repo without a word.
        with (
            gzip.GzipFile(fileobj=io.BytesIO(data)) as unzipped,
            tarfile.open(fileobj=unzipped, mode="r|") as archive,
        ):
            for member in archive:
                if (path := _archive_path(member)) is None:
                    continue
                paths.append(path)
                kind = file_kind(path)
                if kind is None or member.size > limits.max_file_bytes:
                    continue
                total += member.size
                if total > limits.max_total_bytes:
                    raise CloneFailed(
                        "This repo has too much text for the chatbot to read.",
                        details=f"Its files add up to more than {limits.max_total_bytes:,} bytes, "
                        "the limit for one download. Pick a smaller repo, or raise that limit.",
                    )
                file = archive.extractfile(member)
                if file is not None and (document := _document(path, kind, file.read())):
                    documents.append(document)
    except (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile) as error:
        raise CloneFailed(
            "GitHub's download of this repo could not be read.",
            details=f"The .tar.gz archive is broken or cut short: {error}. Try again.",
        ) from error
    # Only the file list is used to write the tree; the archive has no folder on disk.
    tree = RepoScan(Path(), tuple(paths), (), ()).tree_summary(limits.tree_chars)
    return [Document(TREE_PATH, tree, "tree"), *documents]


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
    if b"\x00" in data:
        return None
    text = data.decode("utf-8", errors="replace")
    if not text.strip() or len(text) / (text.count("\n") + 1) > _GENERATED_LINE_CHARS:
        return None
    return Document(path, text, kind)


def _archive_path(member: tarfile.TarInfo) -> str | None:
    """The member's path in the repo, or None for links, folders, odd names and skipped folders.
    GitHub puts every file under one top folder named owner-repo-commit."""
    parts = member.name.split("/")[1:]
    is_kept = (
        member.isreg()
        and parts
        and {"", ".", ".."}.isdisjoint(parts)
        and SKIP_DIRS.isdisjoint(parts[:-1])
    )
    return "/".join(parts) if is_kept else None
