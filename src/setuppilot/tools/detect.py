"""Find every stack a repo could use from its top-level files. Flag conflicts, do not guess."""

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from setuppilot.tools.compose import COMPOSE_NAMES, MAX_READ_BYTES
from setuppilot.tools.scan import RepoScan

Stack = Literal["python", "node", "go", "rust", "ruby", "java", "docker"]

# Lock files name the package manager. Two of them for one stack is a conflict.
_LOCKS: dict[Stack, tuple[tuple[str, str], ...]] = {
    "python": (
        ("uv.lock", "uv"),
        ("poetry.lock", "poetry"),
        ("Pipfile.lock", "pipenv"),
        ("pdm.lock", "pdm"),
    ),
    "node": (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lock", "bun"),
        ("bun.lockb", "bun"),
        ("package-lock.json", "npm"),
        ("npm-shrinkwrap.json", "npm"),
    ),
}
_PYTHON_MANIFESTS = ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile")
_CONDA_FILES = ("environment.yml", "environment.yaml")
_NODE_MANAGERS = ("npm", "yarn", "pnpm", "bun")
# A repo's own build script for Java; it replaces an installed Maven or Gradle.
JAVA_WRAPPERS = {"maven": "mvnw", "gradle": "gradlew"}
_GRADLE_FILES = ("build.gradle.kts", "build.gradle", "settings.gradle.kts", "settings.gradle")
_TOP_MANIFESTS = (
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Dockerfile",
    *COMPOSE_NAMES,
)
# A version hint is short and plain, like "3.11", ">=18" or "1.80"; anything else is dropped.
_VERSION_HINT = re.compile(r"[0-9A-Za-z.*^~<>=!][0-9A-Za-z.*^~<>=!, _-]{0,39}")


@dataclass(frozen=True)
class StackCandidate:
    stack: Stack
    manager: str
    evidence: tuple[str, ...]
    version_hint: str | None


@dataclass(frozen=True)
class Detection:
    candidates: tuple[StackCandidate, ...]
    conflicts: tuple[str, ...]
    notes: tuple[str, ...]


def detect_stacks(scan: RepoScan) -> Detection:
    top = {f for f in scan.files if "/" not in f}
    notes: list[str] = []
    reader = _ManifestReader(scan.root, notes)
    candidates: list[StackCandidate] = []
    conflicts: list[str] = []

    requirements = sorted(
        f
        for f in scan.files
        if re.fullmatch(r"[^/]*requirements[^/]*\.txt|requirements/[^/]+\.txt", f)
    )
    if requirements or top & set(_PYTHON_MANIFESTS + _CONDA_FILES):
        found = _python(top, tuple(requirements), reader)
        candidates += found
        conflicts += _lock_conflict("Python", found)
    if "package.json" in top:
        found, conflict = _node(top, reader)
        candidates += found
        conflicts += conflict
    if "go.mod" in top:
        go_line = re.search(r"^go\s+(\S+)", reader.text("go.mod") or "", re.MULTILINE)
        hint = reader.hint(go_line.group(1) if go_line else None, "go.mod")
        candidates.append(StackCandidate("go", "go", ("go.mod",), hint))
    if "Cargo.toml" in top:
        candidates.append(StackCandidate("rust", "cargo", ("Cargo.toml",), _rust_hint(top, reader)))
    if "Gemfile" in top:
        hint = (
            reader.hint(reader.line(".ruby-version"), ".ruby-version")
            if ".ruby-version" in top
            else None
        )
        candidates.append(StackCandidate("ruby", "bundler", ("Gemfile",), hint))
    java = _java(top)
    candidates += java
    conflicts += _lock_conflict("Java", java)
    compose = sorted(top & set(COMPOSE_NAMES), key=COMPOSE_NAMES.index)
    if compose:
        candidates.append(StackCandidate("docker", "compose", tuple(compose), None))
    elif "Dockerfile" in top:
        candidates.append(StackCandidate("docker", "dockerfile", ("Dockerfile",), None))

    notes += _repo_notes(scan, top, reader)
    return Detection(tuple(candidates), tuple(conflicts), tuple(notes))


def _repo_notes(scan: RepoScan, top: set[str], reader: "_ManifestReader") -> list[str]:
    notes = []
    deeper = sorted(
        f for f in scan.files if f.count("/") == 1 and f.split("/")[1] in _TOP_MANIFESTS
    )
    if deeper:
        notes.append("Sub-projects in folders (not set up automatically): " + ", ".join(deeper))
    linked = sorted(f for f in scan.skipped_symlinks if f in _TOP_MANIFESTS)
    if linked:
        notes.append("These setup files are links and were not read: " + ", ".join(linked))
    if scan.skipped_dirs:
        notes.append(
            "The repo itself contains " + ", ".join(scan.skipped_dirs) + "; they were not read "
            "and should not be trusted."
        )
    if ".gitmodules" in top:
        notes.append("This repo uses git submodules; they were not downloaded.")
    if ".gitattributes" in top and "filter=lfs" in (reader.text(".gitattributes") or ""):
        notes.append("This repo uses Git LFS; those large files were not downloaded.")
    return notes


def _python(
    top: set[str], requirements: tuple[str, ...], reader: "_ManifestReader"
) -> list[StackCandidate]:
    pyproject = reader.toml("pyproject.toml") if "pyproject.toml" in top else {}
    locked = [(lock, manager) for lock, manager in _LOCKS["python"] if lock in top]
    if isinstance(_dig(pyproject, "tool", "poetry"), dict) and "poetry.lock" not in top:
        locked.append(("pyproject.toml", "poetry"))
    if "Pipfile" in top and "Pipfile.lock" not in top:
        locked.append(("Pipfile", "pipenv"))
    hint = reader.line(".python-version") if ".python-version" in top else None
    hint = hint or _dig(pyproject, "project", "requires-python")
    hint = hint or _dig(pyproject, "tool", "poetry", "dependencies", "python")
    hint = reader.hint(hint, "the Python version")
    manifests = tuple(f for f in _PYTHON_MANIFESTS if f in top) + requirements
    found = [StackCandidate("python", m, (lock, *manifests), hint) for lock, m in locked]
    if not found and manifests:
        found.append(StackCandidate("python", "pip", manifests, hint))
    env_file = _first_present(top, _CONDA_FILES)
    if env_file:
        found.append(StackCandidate("python", "conda", (env_file,), hint))
    return found


def _node(top: set[str], reader: "_ManifestReader") -> tuple[list[StackCandidate], list[str]]:
    package = reader.json("package.json")
    hint = _dig(package, "engines", "node")
    for version_file in (".nvmrc", ".node-version"):
        if version_file in top:
            hint = reader.line(version_file)
    hint = reader.hint(hint, "the Node version")
    found: list[StackCandidate] = []
    for lock, manager in _LOCKS["node"]:
        if lock in top and manager not in {c.manager for c in found}:
            found.append(StackCandidate("node", manager, (lock, "package.json"), hint))
    declared = str(package.get("packageManager", "")).split("@")[0]
    if declared and declared not in _NODE_MANAGERS:
        reader.note("package.json names an unknown packageManager; it was ignored.")
        declared = ""
    conflicts = _lock_conflict("Node", found)
    if declared and found and declared not in {c.manager for c in found}:
        conflicts.append(
            f"package.json says packageManager is {declared}, but the lock file is for "
            f"{found[0].manager}. Pick one."
        )
    if declared and declared not in {c.manager for c in found}:
        found.insert(0, StackCandidate("node", declared, ("package.json",), hint))
    if not found:
        found.append(StackCandidate("node", "npm", ("package.json",), hint))
    return found, conflicts


def _rust_hint(top: set[str], reader: "_ManifestReader") -> str | None:
    name = _first_present(top, ("rust-toolchain.toml", "rust-toolchain"))
    if name is None:
        return None
    # The plain rust-toolchain file may be TOML too; old ones hold just the channel.
    text = reader.text(name) or ""
    channel = (
        _dig(reader.toml(name), "toolchain", "channel")
        if text.lstrip().startswith("[")
        else reader.line(name)
    )
    return reader.hint(channel, name)


def _java(top: set[str]) -> list[StackCandidate]:
    found = []
    if "pom.xml" in top:
        found.append(
            StackCandidate("java", "maven", ("pom.xml", *(top & {JAVA_WRAPPERS["maven"]})), None)
        )
    gradle = _first_present(top, _GRADLE_FILES)
    if gradle:
        wrapper = top & {JAVA_WRAPPERS["gradle"]}
        found.append(StackCandidate("java", "gradle", (gradle, *wrapper), None))
    return found


def _lock_conflict(label: str, found: list[StackCandidate]) -> list[str]:
    managers = sorted({c.manager for c in found if c.manager != "conda"})
    if len(managers) < 2:
        return []
    return [f"{label}: files for {' and '.join(managers)} were all found. Pick one."]


def _first_present(top: set[str], names: tuple[str, ...]) -> str | None:
    return next((n for n in names if n in top), None)


def _dig(data: Any, *keys: str) -> Any:
    """data[k1][k2]... or None, without trusting the shape of a repo's file."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


class _ManifestReader:
    """Read small top-level files as data. A bad file becomes a note, not a crash."""

    def __init__(self, root: Path, notes: list[str]) -> None:
        self._root = root
        self._notes = notes

    def note(self, text: str) -> None:
        self._notes.append(text)

    def text(self, name: str) -> str | None:
        path = self._root / name
        if path.stat().st_size > MAX_READ_BYTES:
            self.note(f"{name} is too big to read; skipped.")
            return None
        try:
            return path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            self.note(f"{name} is not UTF-8 text; skipped.")
            return None

    def line(self, name: str) -> str | None:
        text = self.text(name)
        return text.strip().splitlines()[0].strip() if text and text.strip() else None

    def hint(self, value: Any, source: str) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and _VERSION_HINT.fullmatch(value):
            return value
        self.note(f"The version in {source} does not look like a version; ignored.")
        return None

    def toml(self, name: str) -> dict:
        try:
            return tomllib.loads(self.text(name) or "")
        except (tomllib.TOMLDecodeError, RecursionError) as error:
            self.note(f"{name} is not valid TOML: {type(error).__name__}")
            return {}

    def json(self, name: str) -> dict:
        try:
            data = json.loads(self.text(name) or "{}")
        except (json.JSONDecodeError, RecursionError) as error:
            self.note(f"{name} is not valid JSON: {type(error).__name__}")
            return {}
        return data if isinstance(data, dict) else {}
