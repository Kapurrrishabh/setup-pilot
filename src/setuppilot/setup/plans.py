"""Install and check steps for each stack. Built in plain code, then checked like any command."""

import json
import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from setuppilot.errors import StackNotFound
from setuppilot.guardrails.commands import JAVA_WRAPPERS, VENV_PYTHON, ExecTarget
from setuppilot.guardrails.commands import NODE_MANAGERS as _NODE_MANAGERS
from setuppilot.guardrails.runner import CommandOutput
from setuppilot.tools.compose import MAX_READ_BYTES, find_compose_files
from setuppilot.tools.detect import StackCandidate

_PYTHON_MANAGERS = {"pip", "uv", "poetry", "pipenv", "pdm"}
SUPPORTED_MANAGERS = _PYTHON_MANAGERS | _NODE_MANAGERS | {
    "conda", "go", "cargo", "bundler", "maven", "gradle", "compose",
}  # fmt: skip
# conda puts python.exe at the top of a Windows environment, not in bin/.
_CONDA_PYTHON: dict[ExecTarget, str] = {
    "posix": ".conda-env/bin/python",
    "windows": ".conda-env/python.exe",
}
# Keeps Maven's downloads inside the repo folder.
_MAVEN_REPO = "-Dmaven.repo.local=.m2/repository"
# uv is told the venv to use, so neither a parent folder's .venv nor the repo's own uv
# settings (like `system = true`) can send packages anywhere else.
_UV_TARGET = "--python .venv"
_NOT_PACKAGES = {"tests", "test", "docs", "doc", "examples", "scripts", "benchmarks"}


@dataclass(frozen=True)
class PlannedCommand:
    text: str
    reason: str


@dataclass(frozen=True)
class Check:
    name: str
    is_passed: bool
    detail: str


def install_plan(
    candidate: StackCandidate,
    top_files: frozenset[str],
    is_installable_project: bool,
    target: ExecTarget,
) -> list[PlannedCommand]:
    """Commands that install the project's dependencies inside the repo folder.
    is_installable_project: the repo itself can be pip-installed (see is_installable_project)."""
    match candidate.manager:
        case "pip":
            return [
                PlannedCommand("uv venv .venv", "Make an isolated Python environment."),
                PlannedCommand(
                    _pip_install(candidate, is_installable_project), "Install the packages."
                ),
            ]
        case "uv":
            return [PlannedCommand("uv sync", "Make .venv and install the locked packages.")]
        case "poetry" | "pipenv" | "pdm":
            return [PlannedCommand(f"{candidate.manager} install", "Install into .venv.")]
        case "conda":
            command = f"conda env create -f {candidate.evidence[0]} -p ./.conda-env"
            return [PlannedCommand(command, "Make a conda environment inside the repo folder.")]
        case "npm":
            if "package-lock.json" in top_files:
                return [PlannedCommand("npm ci", "Install the exact locked Node packages.")]
            return [PlannedCommand("npm install", "Install the Node packages.")]
        case "yarn" | "pnpm" | "bun":
            return [PlannedCommand(f"{candidate.manager} install", "Install the Node packages.")]
        case "go":
            return [PlannedCommand("go mod download", "Download the Go modules.")]
        case "cargo":
            return [PlannedCommand("cargo fetch", "Download the Rust crates.")]
        case "bundler":
            return [PlannedCommand("bundle install", "Install the Ruby gems into vendor/bundle.")]
        case "maven":
            command = f"{_java_program(candidate, target)} dependency:resolve {_MAVEN_REPO}"
            return [PlannedCommand(command, "Download the Java dependencies into the repo folder.")]
        case "gradle":
            command = f"{_java_program(candidate, target)} dependencies"
            return [PlannedCommand(command, "Download the Java dependencies.")]
        case "compose":
            command = f"docker compose -f {candidate.evidence[0]} up -d --build --wait"
            return [
                PlannedCommand(command, "Build and start the services, and wait until healthy.")
            ]
    raise StackNotFound(
        f"SetupPilot cannot set up a {candidate.stack} project with {candidate.manager} yet.",
        details="Set it up by hand for now, using the repo's README.",
    )


def is_installable_project(repo: Path) -> bool:
    """A setup.py, or a pyproject.toml with a [project] or [build-system] table. A pyproject
    that only holds tool settings (black, ruff, pytest) cannot be installed with `-e .`."""
    if (repo / "setup.py").is_file():
        return True
    path = repo / "pyproject.toml"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_READ_BYTES:
        return False
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, RecursionError):
        return False
    return "project" in data or "build-system" in data


def _pip_install(candidate: StackCandidate, is_installable: bool) -> str:
    """requirements.txt when there is one, else the other requirements files, plus the project
    itself when it can be installed."""
    files = [f for f in candidate.evidence if f.endswith(".txt")]
    if "requirements.txt" in files:
        files = ["requirements.txt"]
    elif any("/" not in f for f in files):
        files = [f for f in files if "/" not in f]
    elif "requirements/base.txt" in files:
        files = ["requirements/base.txt"]
    parts = [f"-r {f}" for f in files] + (["-e ."] if is_installable else [])
    if not parts:
        raise StackNotFound(
            "This Python repo has nothing to install: no requirements file and no installable "
            "project.",
            details="Its pyproject.toml or setup.cfg only holds tool settings.",
        )
    return f"uv pip install {_UV_TARGET} " + " ".join(parts)


def code_checks(
    candidate: StackCandidate, files: tuple[str, ...], target: ExecTarget
) -> list[PlannedCommand]:
    """Checks that run the project's own code, so they need approval like installs."""
    match candidate.manager:
        case manager if manager in _PYTHON_MANAGERS:
            package = _python_package(files)
            if package is None:
                return []
            return [
                PlannedCommand(
                    f"{VENV_PYTHON[target]} -c 'import {package}'",
                    f"Check: import {package} (this runs the package's own code).",
                )
            ]
        case "go":
            return [PlannedCommand("go build ./...", "Check: the Go code builds.")]
        case "cargo":
            return [PlannedCommand("cargo build", "Check: the Rust code builds (runs build.rs).")]
        case "bundler":
            # Bundler reads the Gemfile by running it as Ruby, so even this check needs approval.
            return [
                PlannedCommand("bundle check", "Check: all gems are installed (runs the Gemfile).")
            ]
        case "maven":
            command = f"{_java_program(candidate, target)} compile {_MAVEN_REPO}"
            return [PlannedCommand(command, "Check: the Java code compiles.")]
        case "gradle":
            command = f"{_java_program(candidate, target)} assemble"
            return [PlannedCommand(command, "Check: it builds.")]
    return []


def quiet_checks(
    candidate: StackCandidate, repo: Path, run: Callable[[str], CommandOutput], target: ExecTarget
) -> list[Check]:
    """Checks that only read state and run no repo code, so they run without asking.
    `run` checks and runs one command."""
    checks: list[Check] = []
    manager = candidate.manager
    if manager in _PYTHON_MANAGERS:
        checks.append(_exists(repo, VENV_PYTHON[target], "Python environment exists"))
        checks.append(
            _exit_zero(run(f"uv pip check {_UV_TARGET}"), "Installed packages fit together")
        )
    elif manager == "conda":
        checks.append(_exists(repo, _CONDA_PYTHON[target], "Conda environment exists"))
    elif manager in _NODE_MANAGERS:
        if not _has_node_dependencies(repo):
            checks.append(Check("Node packages", True, "package.json lists no dependencies."))
        elif manager == "yarn" and os.path.lexists(repo / ".pnp.cjs"):
            # Yarn 2+ installs in Plug'n'Play mode by default: .pnp.cjs and no node_modules.
            checks.append(_exists(repo, ".pnp.cjs", "Yarn Plug'n'Play files exist"))
        else:
            checks.append(_exists(repo, "node_modules", "node_modules exists"))
            if manager == "npm":
                checks.append(
                    _exit_zero(run("npm ls --depth=0"), "All listed Node packages are installed")
                )
    elif manager == "compose":
        compose_file = candidate.evidence[0]
        (found,) = find_compose_files(repo, (compose_file,))
        output = run(f"docker compose -f {compose_file} ps --all --format json")
        checks.append(_services_running(output, found.services))
    return checks


def _java_program(candidate: StackCandidate, target: ExecTarget) -> str:
    wrapper = JAVA_WRAPPERS[target][candidate.manager]
    if wrapper in candidate.evidence:
        return f"./{wrapper}"
    return {"maven": "mvn", "gradle": "gradle"}[candidate.manager]


def _python_package(files: tuple[str, ...]) -> str | None:
    """A top-level package (pkg/__init__.py or src/pkg/__init__.py) to import as a check."""
    for path in sorted(files):
        parts = path.split("/")
        if parts[0] == "src":
            parts = parts[1:]
        is_package = len(parts) == 2 and parts[1] == "__init__.py" and parts[0].isidentifier()
        if is_package and parts[0] not in _NOT_PACKAGES:
            return parts[0]
    return None


def _has_node_dependencies(repo: Path) -> bool:
    path = repo / "package.json"
    if not path.is_file() or path.stat().st_size > MAX_READ_BYTES:
        return True
    try:
        package = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, RecursionError):
        return True
    return isinstance(package, dict) and any(
        package.get(key) for key in ("dependencies", "devDependencies", "optionalDependencies")
    )


def _exists(repo: Path, relative: str, name: str) -> Check:
    # lexists: in Docker mode the venv's python is a link to a path inside the container.
    is_there = os.path.lexists(repo / relative)
    return Check(name, is_there, f"{relative} {'found' if is_there else 'is missing'}.")


def _exit_zero(output: CommandOutput, name: str) -> Check:
    detail = output.output_tail[-500:] or f"exit code {output.exit_code}"
    return Check(name, output.exit_code == 0, detail)


def _services_running(output: CommandOutput, expected: tuple[str, ...]) -> Check:
    name = "All Docker services are running"
    if output.exit_code != 0:
        return Check(name, False, output.output_tail[-500:])
    # Compose also prints warnings into the same output, so only JSON lines are read.
    lines = [x for x in output.output_tail.splitlines() if x.lstrip().startswith(("{", "["))]
    try:
        rows = [row for line in lines for row in _json_rows(line)]
    except ValueError:  # bad JSON, or a number too long for Python to read
        return Check(name, False, f"Could not read `docker compose ps`: {lines[:3]}")
    # A one-shot job (migrate, init) ends with exit 0. That is safe to accept: `up --wait`
    # already fails when a service that should keep running exits.
    healthy = {
        r.get("Service")
        for r in rows
        if (r.get("State") == "running" and r.get("Health") in ("", None, "healthy"))
        or (r.get("State") == "exited" and r.get("ExitCode") == 0)
    }
    missing = [s for s in expected if s not in healthy]
    if not expected or missing:
        return Check(
            name, False, f"Not running or not healthy: {', '.join(missing) or 'none listed'}"
        )
    return Check(name, True, f"{len(expected)} services running.")


def _json_rows(line: str) -> list[dict]:
    """Newer Compose prints one JSON object per line; older versions print one JSON list."""
    data = json.loads(line)
    return [r for r in (data if isinstance(data, list) else [data]) if isinstance(r, dict)]
