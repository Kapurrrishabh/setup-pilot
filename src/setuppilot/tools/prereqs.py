"""Check that the tools a stack needs are installed, and say how to install the missing ones."""

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Protocol

from setuppilot.tools.detect import JAVA_WRAPPERS, StackCandidate

# Each tool: the command that prints its version, and how to install it.
# Python projects get their venv from uv, so uv stands in for Python.
TOOLS: dict[str, tuple[tuple[str, ...], str]] = {
    "uv": (
        ("uv", "--version"),
        "Install uv: https://docs.astral.sh/uv/getting-started/installation/",
    ),
    "poetry": (
        ("poetry", "--version"),
        "Install Poetry: https://python-poetry.org/docs/#installation",
    ),
    "pipenv": (
        ("pipenv", "--version"),
        "Install Pipenv: https://pipenv.pypa.io/en/latest/installation.html",
    ),
    "pdm": (("pdm", "--version"), "Install PDM: https://pdm-project.org/latest/#installation"),
    "conda": (("conda", "--version"), "Install Miniconda: https://docs.anaconda.com/miniconda/"),
    "node": (
        ("node", "--version"),
        "Install Node.js LTS from https://nodejs.org (macOS: brew install node).",
    ),
    "npm": (("npm", "--version"), "npm comes with Node.js: https://nodejs.org"),
    "yarn": (("yarn", "--version"), "Install Yarn: npm install -g corepack, then corepack enable."),
    "pnpm": (("pnpm", "--version"), "Install pnpm: npm install -g corepack, then corepack enable."),
    "bun": (("bun", "--version"), "Install Bun: https://bun.sh"),
    "go": (("go", "version"), "Install Go: https://go.dev/doc/install (macOS: brew install go)."),
    "cargo": (("cargo", "--version"), "Install Rust with rustup: https://rustup.rs"),
    "ruby": (
        ("ruby", "--version"),
        "Install Ruby: https://www.ruby-lang.org/en/documentation/installation/",
    ),
    "bundle": (("bundle", "--version"), "Install Bundler with: gem install bundler"),
    "java": (("java", "-version"), "Install a JDK, for example Temurin: https://adoptium.net"),
    "mvn": (
        ("mvn", "--version"),
        "Install Maven: https://maven.apache.org/install.html (macOS: brew install maven).",
    ),
    "gradle": (("gradle", "--version"), "Install Gradle: https://gradle.org/install/"),
    "docker": (
        ("docker", "--version"),
        "Install Docker Desktop: https://docs.docker.com/get-docker/",
    ),
    "docker compose": (
        ("docker", "compose", "version"),
        "Docker Compose comes with Docker Desktop: https://docs.docker.com/compose/",
    ),
    "docker engine": (
        ("docker", "info", "--format", "{{.ServerVersion}}"),
        "Start Docker Desktop, then try again.",
    ),
}
_TOOLS_FOR_MANAGER: dict[str, tuple[str, ...]] = {
    "pip": ("uv",),
    "uv": ("uv",),
    "poetry": ("poetry",),
    "pipenv": ("pipenv",),
    "pdm": ("pdm",),
    "conda": ("conda",),
    "npm": ("node", "npm"),
    "yarn": ("node", "yarn"),
    "pnpm": ("node", "pnpm"),
    "bun": ("bun",),
    "go": ("go",),
    "cargo": ("cargo",),
    "bundler": ("ruby", "bundle"),
    "maven": ("java", "mvn"),
    "gradle": ("java", "gradle"),
    "compose": ("docker", "docker compose", "docker engine"),
    "dockerfile": ("docker", "docker engine"),
}
# The tool whose version should match a pinned version hint (uv picks Python by itself).
_PINNED_TOOL = {"node": "node", "go": "go", "rust": "cargo", "ruby": "ruby"}
_VERSION_TIMEOUT_SECONDS = 10
_EXACT_PIN = re.compile(r"v?(\d+)\.(\d+)(\.\d+)?")


@dataclass(frozen=True)
class ToolCheck:
    tool: str
    is_installed: bool
    version: str | None
    problem: str | None


@dataclass(frozen=True)
class Probe:
    is_found: bool
    output: str


class ToolProbe(Protocol):
    def run(self, command: tuple[str, ...]) -> Probe:
        """Run a version command. is_found is False when the program is not on PATH."""


class SystemToolProbe:
    def run(self, command: tuple[str, ...]) -> Probe:
        executable = shutil.which(command[0])
        if executable is None:
            return Probe(False, "")
        # Run outside the repo: yarn, for one, would run a script named in the repo's config.
        with tempfile.TemporaryDirectory() as neutral_dir:
            try:
                done = subprocess.run(  # noqa: S603 - fixed version command, no shell
                    [executable, *command[1:]],
                    capture_output=True,
                    text=True,
                    timeout=_VERSION_TIMEOUT_SECONDS,
                    cwd=neutral_dir,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return Probe(True, "")
        output = (done.stdout or done.stderr).strip()
        return Probe(True, output.splitlines()[0] if done.returncode == 0 and output else "")


def check_prerequisites(candidate: StackCandidate, probe: ToolProbe) -> tuple[ToolCheck, ...]:
    wrapper = JAVA_WRAPPERS.get(candidate.manager)
    skipped = {"mvn", "gradle"} if wrapper in candidate.evidence else set()
    checks = []
    for tool in _TOOLS_FOR_MANAGER[candidate.manager]:
        if tool in skipped:
            continue
        command, guidance = TOOLS[tool]
        result = probe.run(command)
        if not result.is_found:
            checks.append(ToolCheck(tool, False, None, f"Not installed. {guidance}"))
        elif not result.output:
            problem = f"Installed, but `{' '.join(command)}` did not answer. {guidance}"
            checks.append(ToolCheck(tool, False, None, problem))
        else:
            mismatch = _pin_mismatch(candidate, tool, result.output)
            checks.append(ToolCheck(tool, True, result.output, mismatch))
    return tuple(checks)


def _pin_mismatch(candidate: StackCandidate, tool: str, version_output: str) -> str | None:
    """Warn when the repo pins an exact major.minor that the installed tool does not have."""
    pin = _EXACT_PIN.fullmatch(candidate.version_hint or "")
    if _PINNED_TOOL.get(candidate.stack) != tool or pin is None:
        return None
    wanted = f"{pin.group(1)}.{pin.group(2)}"
    installed = re.search(r"(\d+)\.(\d+)", version_output)
    if installed and f"{installed.group(1)}.{installed.group(2)}" != wanted:
        return (
            f"The repo asks for {tool} {candidate.version_hint}, but {version_output} is installed."
        )
    return None
