"""The facts about a repo that go to the LLM: small, in priority order, and with secrets hidden.
Only raw facts go in, never the detector's answer, so the AI's reading stays independent."""

import hashlib
import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from setuppilot.redact import redact
from setuppilot.tools.compose import COMPOSE_NAMES
from setuppilot.tools.detect import DOTNET_SUFFIXES, LOCK_FILES, MANIFEST_NAMES
from setuppilot.tools.scan import RepoScan, decode_text

# Reads a repo file as text, at most this many characters; None when it is binary.
Reader = Callable[[str, int], str | None]

_VERSION_FILES = frozenset(
    {".python-version", ".nvmrc", ".node-version", ".tool-versions", "rust-toolchain",
     "rust-toolchain.toml", "global.json", ".ruby-version", ".java-version"}
)  # fmt: skip
# Dockerfiles and compose files have their own, shorter blocks below.
_MANIFESTS = frozenset(MANIFEST_NAMES) - {"Dockerfile", *COMPOSE_NAMES} | {
    "setup.cfg", "Pipfile", "environment.yml", "settings.gradle", "settings.gradle.kts",
    "mix.exs", "Package.swift", "CMakeLists.txt", "Makefile",
}  # fmt: skip
_ENV_EXAMPLES = frozenset({".env.example", ".env.sample", ".env.template", "env.example"})
_README = re.compile(r"readme(\.(md|rst|txt|markdown))?", re.IGNORECASE)
_README_TOPICS = re.compile(
    r"install|setup|set up|requirement|prerequisite|build|usage|run|getting started|quick ?start"
    r"|develop",
    re.IGNORECASE,
)
_CI_LINE = re.compile(r"runs-on:|uses: *actions/setup-|-version:|run:")
# Folders whose manifests say little about how the project itself is set up.
_SIDE_FOLDERS = re.compile(
    r"(docs?|examples?|samples?|tests?|fixtures?|benchmarks?)/.*", re.IGNORECASE
)
_BUILD_WORKFLOW = re.compile(r"(.*[-_.])?(tests?|ci|build|checks?|main)([-_.].*)?", re.I)
_DOCKER_LINE = re.compile(r"(FROM|RUN|EXPOSE|CMD|ENTRYPOINT)\s", re.IGNORECASE)
# PROVISIONAL caps per block, from research-planning.md: they fit the 15 eval repos.
_MAX_MANIFESTS, _MANIFEST_CHARS, _README_CHARS, _README_INTRO = 6, 1200, 3000, 600
_CI_FILES, _CI_CHARS, _DOCKER_FILES, _DOCKER_CHARS = 2, 1500, 2, 800
_COMPOSE_CHARS, _ENV_CHARS, _MAX_DEPTH = 400, 400, 2


@dataclass(frozen=True)
class RepoEvidence:
    # (label, text), most useful first.
    blocks: tuple[tuple[str, str], ...]


def local_reader(root: Path) -> Reader:
    """Reads the files a scan listed: they are real files inside the clone, never links."""

    def read(path: str, max_chars: int) -> str | None:
        with (root / path).open("rb") as file:
            text = decode_text(file.read(max_chars * 4))
        return None if text is None else text[:max_chars]

    return read


def build_evidence(scan: RepoScan, read: Reader, max_chars: int) -> RepoEvidence:
    files = sorted(scan.files, key=lambda f: (f.count("/"), f))
    shallow = [f for f in files if f.count("/") <= _MAX_DEPTH]
    blocks: list[tuple[str, str]] = []
    used = 0

    def add(label: str, text: str | None) -> None:
        nonlocal used
        # Each block also costs its "## label" line and the blank line between blocks.
        room = max_chars - used - len(label) - 6
        if not text or room <= 0:
            return
        blocks.append((label, redact(text)[:room]))
        used += len(label) + 6 + len(blocks[-1][1])

    for path in (f for f in shallow if _name(f) in _VERSION_FILES):
        add(f"version file {path}", _first_lines(read(path, 300)))
    add("lock files", "\n".join(f for f in shallow if _name(f) in LOCK_FILES))
    manifests = [f for f in shallow if _name(f) in _MANIFESTS or f.endswith(DOTNET_SUFFIXES)]
    manifests.sort(key=lambda f: bool(_SIDE_FOLDERS.fullmatch(f)))
    for path in manifests[:_MAX_MANIFESTS]:
        add(f"manifest {path}", _manifest(path, read(path, 20_000)))
    readme = next((f for f in files if "/" not in f and _README.fullmatch(f)), None)
    if readme:
        add(f"readme {readme}", _readme(read(readme, 60_000)))
    workflows = [f for f in files if re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", f)]
    workflows.sort(key=lambda f: not _BUILD_WORKFLOW.fullmatch(_name(f)))
    for path in workflows[:_CI_FILES]:
        add(f"ci {path}", _lines(read(path, 20_000), _CI_LINE, _CI_CHARS))
    dockerfiles = [f for f in shallow if _name(f) == "Dockerfile"]
    for path in dockerfiles[:_DOCKER_FILES]:
        add(f"dockerfile {path}", _lines(read(path, 20_000), _DOCKER_LINE, _DOCKER_CHARS))
    for path in (f for f in files if "/" not in f and re.fullmatch(r".*compose.*\.ya?ml", f)):
        add(f"compose {path}", _compose(read(path, 20_000)))
    for path in (f for f in shallow if _name(f) in _ENV_EXAMPLES):
        add(f"env names in {path}", _env_names(read(path, 20_000)))
    add("file tree", scan.tree_summary(max(max_chars - used - 20, 0)))
    return RepoEvidence(tuple(blocks))


def evidence_text(evidence: RepoEvidence) -> tuple[str, str]:
    """The body and its tag. The tag is a hash of the body, so a file in the repo cannot hold
    the line that closes its own data block, and the same repo still makes the same prompt."""
    body = "\n\n".join(f"## {label}\n{text}" for label, text in evidence.blocks)
    return body, hashlib.sha256(body.encode()).hexdigest()[:16]


def data_block(body: str, tag: str) -> str:
    return f"<<<DATA {tag}\n{body}\nDATA {tag}>>>"


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _first_lines(text: str | None) -> str | None:
    if text is None:
        return None
    return " ".join(line.strip() for line in text.splitlines() if line.strip())[:160]


def _manifest(path: str, text: str | None) -> str | None:
    if text is None:
        return None
    head = "\n".join(text.splitlines()[:40])[:_MANIFEST_CHARS]
    try:
        if _name(path) == "package.json":
            return _package_json(json.loads(text))[:_MANIFEST_CHARS]
        if _name(path) == "pyproject.toml":
            return _pyproject(tomllib.loads(text))[:_MANIFEST_CHARS]
    except (ValueError, RecursionError, AttributeError, TypeError):
        return head  # not the usual shape: its first lines still say something
    return head


def _package_json(data: dict) -> str:
    keys = ("name", "packageManager", "engines", "workspaces")
    facts = {k: data[k] for k in keys if k in data}
    facts["scripts"] = {k: str(v)[:80] for k, v in dict(data.get("scripts", {})).items()}
    for key in ("dependencies", "devDependencies"):
        facts[key] = sorted(dict(data.get(key, {})))
    return json.dumps(facts, ensure_ascii=False)


def _pyproject(data: dict) -> str:
    project, tool = dict(data.get("project", {})), dict(data.get("tool", {}))
    facts = {
        "requires-python": project.get("requires-python"),
        "dependencies": project.get("dependencies"),
        "build-system": data.get("build-system"),
        "dependency-groups": data.get("dependency-groups"),
        "tool keys": sorted(tool),
        "poetry dependencies": dict(tool.get("poetry", {})).get("dependencies"),
    }
    return json.dumps({k: v for k, v in facts.items() if v}, ensure_ascii=False, default=str)


def _readme(text: str | None) -> str | None:
    """The start, plus the sections whose heading is about installing or running."""
    if text is None:
        return None
    lines = text.splitlines()
    parts, keep = [text[:_README_INTRO]], False
    for i, line in enumerate(lines):
        underline = i + 1 < len(lines) and re.fullmatch(r"[=\-~^]{3,}", lines[i + 1].strip())
        if line.startswith("#") or (underline and line.strip()):
            keep = bool(_README_TOPICS.search(line))
        if keep:
            parts.append(line)
    return "\n".join(parts)[:_README_CHARS]


def _lines(text: str | None, wanted: re.Pattern[str], max_chars: int) -> str | None:
    if text is None:
        return None
    return "\n".join(line.strip() for line in text.splitlines() if wanted.search(line))[:max_chars]


def _compose(text: str | None) -> str | None:
    if text is None:
        return None
    service = re.compile(r"^  [A-Za-z0-9_.-]+:\s*$|^\s+image:")
    return "\n".join(line.rstrip() for line in text.splitlines() if service.match(line))[
        :_COMPOSE_CHARS
    ]


def _env_names(text: str | None) -> str | None:
    """Names only: an example file can still hold a real key someone pasted in."""
    if text is None:
        return None
    names = re.findall(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", text, re.MULTILINE)
    return ", ".join(names)[:_ENV_CHARS]
