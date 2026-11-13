"""The repo card: facts from the manifests and CI files (license, versions, test matrix), sent with
every question. Search alone misses them: a long changelog outranks one requires-python line."""

import json
import logging
import tomllib
from collections.abc import Callable
from typing import Any

import yaml

from setuppilot.chat.chunks import Chunk
from setuppilot.chat.collect import Document

log = logging.getLogger(__name__)

# About 450 tokens per question, measured on the chat eval repos.
CARD_CHARS = 1500
_VALUE_CHARS = 200
_LIST_ITEMS = 10


def build_repo_card(documents: list[Document]) -> list[Chunk]:
    """One card chunk per file that gave facts, so a cited fact points at its file."""
    texts = {d.path: d.text for d in documents}
    readers: list[tuple[str, Callable[[str], list[str]]]] = [
        ("pyproject.toml", _pyproject_facts),
        ("package.json", _package_json_facts),
        ("Cargo.toml", _cargo_facts),
    ]
    workflows = sorted(path for path in texts if path.startswith(".github/workflows/"))
    readers += [(path, _workflow_facts) for path in workflows]
    chunks, used = [], 0
    for path, read in readers:
        if path not in texts:
            continue
        try:
            facts = read(texts[path])
        except (ValueError, yaml.YAMLError, RecursionError) as error:
            # tomllib and json errors are ValueErrors. A broken file is still indexed as text.
            # Not str(error): a YAML error quotes the broken line, which may be private.
            mark = error.problem_mark if isinstance(error, yaml.MarkedYAMLError) else None
            line = mark.line + 1 if mark else None
            log.info(
                "repo card skipped a file",
                extra={"path": path, "reason": type(error).__name__, "line": line},
            )
            continue
        kept = []
        for fact in facts:
            if used + len(fact) + 1 > CARD_CHARS:
                break
            kept.append(fact)
            used += len(fact) + 1
        if kept:
            chunks.append(Chunk(path, "repo card", "\n".join(kept), 0, 0, "card"))
    return chunks


def _pyproject_facts(text: str) -> list[str]:
    project = _table(tomllib.loads(text).get("project"))
    license_field = project.get("license")
    if isinstance(license_field, dict):
        license_field = license_field.get("text")
    classifiers = _strings(project.get("classifiers"))
    named = [c.split("::")[-1].strip() for c in classifiers if c.startswith("License")]
    return _facts(
        ("name", project.get("name")),
        ("description", project.get("description")),
        ("license", license_field or ", ".join(named)),
        ("requires-python", project.get("requires-python")),
        ("dependencies", _strings(project.get("dependencies"))),
        ("scripts", list(_table(project.get("scripts")))),
    )


def _package_json_facts(text: str) -> list[str]:
    package = _table(json.loads(text))
    return _facts(
        ("name", package.get("name")),
        ("description", package.get("description")),
        ("license", package.get("license")),
        ("node engines", _table(package.get("engines")).get("node")),
        ("dependencies", list(_table(package.get("dependencies")))),
        ("test script", _table(package.get("scripts")).get("test")),
    )


def _cargo_facts(text: str) -> list[str]:
    package = _table(tomllib.loads(text).get("package"))
    names = ("name", "version", "description", "license", "rust-version", "edition")
    return _facts(*((name, package.get(name)) for name in names))


def _workflow_facts(text: str) -> list[str]:
    facts = []
    for name, value in _table(_table(yaml.safe_load(text)).get("jobs")).items():
        job = _table(value)
        matrix = _table(_table(job.get("strategy")).get("matrix"))
        parts = [
            f"{key}: {_join(values)}"
            for key, values in matrix.items()
            if isinstance(values, list) and all(isinstance(v, str | int | float) for v in values)
        ]
        runs_on = job.get("runs-on")
        if isinstance(runs_on, str) and "${{" not in runs_on:
            parts.append(f"runs-on: {runs_on}")
        if parts:
            facts.append(_short(f"CI job {name}: " + "; ".join(parts)))
    return facts


def _facts(*pairs: tuple[str, Any]) -> list[str]:
    """ "label: value" lines for the values that are text or lists of text."""
    lines = []
    for label, value in pairs:
        shown = _join(value) if isinstance(value, list) else value
        if isinstance(shown, str) and shown.strip():
            lines.append(_short(f"{label}: {shown}"))
    return lines


def _join(values: list[Any]) -> str:
    # Capped before joining: YAML aliases can repeat one long value thousands of times.
    return ", ".join(_short(str(v)) for v in values[:_LIST_ITEMS])


def _short(text: str) -> str:
    """One line, NUL-free (Postgres refuses it), at most _VALUE_CHARS long."""
    return " ".join(text.replace("\x00", "").split())[:_VALUE_CHARS]


def _table(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
