"""Check that the tools a stack needs are installed, and say how to install the missing ones."""

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from setuppilot.guardrails.commands import (
    JAVA_WRAPPERS,
    ExecTarget,
    RecipeKind,
    command_verb,
    split_command,
)
from setuppilot.guardrails.process_tree import start_tree
from setuppilot.guardrails.runner import HOST_TARGET, child_env, find_program, launch_args
from setuppilot.tools.detect import StackCandidate
from setuppilot.tools.host import HostOs
from setuppilot.tools.versions import fits, is_pin


@dataclass(frozen=True)
class InstallRecipe:
    """How to install a tool on one system. command is a fixed form (see recipe_command in
    guardrails.commands) and a package name; None when the user must do it by hand."""

    command: tuple[RecipeKind, str] | None
    manual: str
    needs_admin: bool


@dataclass(frozen=True)
class ToolSpec:
    version_command: tuple[str, ...]
    guidance: str
    # True: a version outside the repo's range stops the run. False: only a warning, because
    # uv fetches Python, Go fetches its own toolchain, and rustup reads rust-toolchain.
    version_blocks: bool
    install: Mapping[HostOs, InstallRecipe]


def _winget(package: str, guidance: str, needs_admin: bool) -> dict[HostOs, InstallRecipe]:
    return {"windows": InstallRecipe(("winget", package), guidance, needs_admin)}


def _brew(formula: str, guidance: str) -> dict[HostOs, InstallRecipe]:
    return {"macos": InstallRecipe(("brew", formula), guidance, False)}


def _by_hand(os_name: HostOs, text: str) -> dict[HostOs, InstallRecipe]:
    return {os_name: InstallRecipe(None, text, True)}


def _linux(debian: str, fedora: str) -> dict[HostOs, InstallRecipe]:
    """apt and dnf need sudo, so SetupPilot only shows these for you to run."""
    return _by_hand("debian", f"Run this yourself: sudo apt install -y {debian}") | _by_hand(
        "fedora", f"Run this yourself: sudo dnf install -y {fedora}"
    )


def _uv_tool(name: str, guidance: str) -> dict[HostOs, InstallRecipe]:
    recipe = InstallRecipe(("uv_tool", name), guidance, False)
    return dict.fromkeys(("windows", "macos", "debian", "fedora", "linux"), recipe)


def _tool(
    command: tuple[str, ...], guidance: str, *recipes: dict[HostOs, InstallRecipe]
) -> ToolSpec:
    """A tool whose version only gets a warning when it does not fit the repo."""
    install: dict[HostOs, InstallRecipe] = {}
    for recipe in recipes:
        install |= recipe
    return ToolSpec(command, guidance, False, install)


def _strict_tool(
    command: tuple[str, ...], guidance: str, *recipes: dict[HostOs, InstallRecipe]
) -> ToolSpec:
    """A tool whose version must fit the repo, or the run stops."""
    return replace(_tool(command, guidance, *recipes), version_blocks=True)


_NODE = "Install Node.js LTS from https://nodejs.org"
_NODE_INSTALL = (
    _winget("OpenJS.NodeJS.LTS", _NODE, needs_admin=True),
    _brew("node", _NODE),
    _linux("nodejs npm", "nodejs npm"),
)
_COREPACK = "npm install -g corepack, then corepack enable."
_DOCKER = "Install Docker Desktop: https://docs.docker.com/get-docker/"
# Each tool: its version command, how to install it, and per system a command or manual steps.
# Python projects get their venv from uv, so uv stands in for Python.
TOOLS: dict[str, ToolSpec] = {
    "uv": _tool(
        ("uv", "--version"),
        "Install uv: https://docs.astral.sh/uv/getting-started/installation/",
        _winget("astral-sh.uv", "https://docs.astral.sh/uv/", needs_admin=False),
        _brew("uv", "https://docs.astral.sh/uv/"),
    ),
    "poetry": _tool(
        ("poetry", "--version"),
        "Install Poetry: https://python-poetry.org/docs/#installation",
        _uv_tool("poetry", "https://python-poetry.org/docs/#installation"),
    ),
    "pipenv": _tool(
        ("pipenv", "--version"),
        "Install Pipenv: https://pipenv.pypa.io/en/latest/installation.html",
        _uv_tool("pipenv", "https://pipenv.pypa.io/en/latest/installation.html"),
    ),
    "pdm": _tool(
        ("pdm", "--version"),
        "Install PDM: https://pdm-project.org/latest/#installation",
        _uv_tool("pdm", "https://pdm-project.org/latest/#installation"),
    ),
    "conda": _tool(
        ("conda", "--version"), "Install Miniconda: https://docs.anaconda.com/miniconda/"
    ),
    "node": _strict_tool(("node", "--version"), f"{_NODE}.", *_NODE_INSTALL),
    "npm": _tool(
        ("npm", "--version"), "npm comes with Node.js: https://nodejs.org", *_NODE_INSTALL
    ),
    "yarn": _tool(("yarn", "--version"), f"Install Yarn: {_COREPACK}", _brew("yarn", _COREPACK)),
    "pnpm": _tool(("pnpm", "--version"), f"Install pnpm: {_COREPACK}", _brew("pnpm", _COREPACK)),
    "bun": _tool(("bun", "--version"), "Install Bun: https://bun.sh"),
    "go": _tool(
        ("go", "version"),
        "Install Go: https://go.dev/doc/install",
        _winget("GoLang.Go", "https://go.dev/doc/install", needs_admin=True),
        _brew("go", "https://go.dev/doc/install"),
        _linux("golang-go", "golang"),
    ),
    "cargo": _tool(
        ("cargo", "--version"),
        "Install Rust with rustup: https://rustup.rs",
        _winget(
            "Rustlang.Rustup",
            "Rust on Windows also needs the Visual Studio C++ Build Tools: https://rustup.rs",
            needs_admin=False,
        ),
        _brew("rust", "https://rustup.rs"),
    ),
    "ruby": _strict_tool(
        ("ruby", "--version"),
        "Install Ruby: https://www.ruby-lang.org/en/documentation/installation/",
        _linux("ruby-full", "ruby ruby-devel"),
    ),
    "bundle": _tool(("bundle", "--version"), "Install Bundler with: gem install bundler"),
    "java": _strict_tool(
        ("java", "-version"),
        "Install a JDK, for example Temurin: https://adoptium.net",
        _winget("EclipseAdoptium.Temurin.21.JDK", "https://adoptium.net", needs_admin=True),
        _by_hand(
            "macos", "Run this yourself (it asks for your password): brew install --cask temurin@21"
        ),
        _linux("openjdk-21-jdk", "java-21-openjdk-devel"),
    ),
    "mvn": _tool(
        ("mvn", "--version"),
        "Install Maven: https://maven.apache.org/install.html",
        _brew("maven", "https://maven.apache.org/install.html"),
        _linux("maven", "maven"),
    ),
    "gradle": _tool(
        ("gradle", "--version"),
        "Install Gradle: https://gradle.org/install/",
        _brew("gradle", "https://gradle.org/install/"),
    ),
    "docker": _tool(("docker", "--version"), _DOCKER),
    "docker compose": _tool(
        ("docker", "compose", "version"),
        "Docker Compose comes with Docker Desktop: https://docs.docker.com/compose/",
    ),
    "docker engine": _tool(
        ("docker", "info", "--format", "{{.OSType}}"),
        "Start Docker Desktop (or `colima start` on macOS), then try again.",
    ),
    "git": _tool(
        ("git", "--version"),
        "Install git: https://git-scm.com/downloads",
        _winget("Git.Git", "https://git-scm.com/downloads", needs_admin=True),
        _brew("git", "https://git-scm.com/downloads"),
        _linux("git", "git"),
    ),
    "dotnet": _strict_tool(
        ("dotnet", "--version"),
        "Install the .NET SDK: https://dotnet.microsoft.com/download",
        _winget(
            "Microsoft.DotNet.SDK.10", "https://dotnet.microsoft.com/download", needs_admin=True
        ),
        _by_hand(
            "macos", "Run this yourself (it asks for your password): brew install --cask dotnet-sdk"
        ),
    ),
    "php": _tool(
        ("php", "--version"),
        "Install PHP: https://www.php.net/downloads",
        _brew("php", "https://www.php.net/downloads"),
        _linux("php-cli", "php-cli"),
    ),
    "composer": _tool(
        ("composer", "--version"),
        "Install Composer: https://getcomposer.org/download/",
        _brew("composer", "https://getcomposer.org/download/"),
        _linux("composer", "composer"),
    ),
    "dart": _tool(("dart", "--version"), "Install Dart: https://dart.dev/get-dart"),
    "flutter": _tool(
        ("flutter", "--version"),
        "Install Flutter (a large download): https://docs.flutter.dev/get-started/install",
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
    "dotnet": ("dotnet",),
    "composer": ("php", "composer"),
    "dart": ("dart",),
    "flutter": ("flutter",),
}
# The tool a detected version hint is about. uv picks Python by itself. go.mod's go line is only
# a minimum, so any newer Go builds the module.
_HINTED_TOOL = {"node": "node", "rust": "cargo", "ruby": "ruby", "go": "go", "dotnet": "dotnet"}
_MINIMUM_HINTS = {"go"}
# The manager whose tools a program needs, where the two names differ.
_MANAGER_FOR_PROGRAM = {"bundle": "bundler", "mvn": "maven", "docker": "compose"}
# The program each kind of install command runs, so it is offered only when that is there.
_INSTALLERS: dict[RecipeKind, tuple[str, ...]] = {
    "winget": ("winget", "--version"),
    "brew": ("brew", "--version"),
    "uv_tool": TOOLS["uv"].version_command,
}
_VERSION_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class ToolNeed:
    tool: str
    is_required: bool
    # A version rule like ">=18" or "3.3.0", or None.
    constraint: str | None
    reason: str


@dataclass(frozen=True)
class ToolCheck:
    tool: str
    is_installed: bool
    version: str | None
    problem: str | None
    is_required: bool
    # None when there is no version rule, or the versions could not be compared.
    fits_version: bool | None
    # Commands that need this tool must not run.
    is_blocking: bool
    reason: str


@dataclass(frozen=True)
class Probe:
    is_found: bool
    output: str


class ToolProbe(Protocol):
    def run(self, command: tuple[str, ...]) -> Probe:
        """Run a version command. is_found is False when the program is not on PATH."""


class SystemToolProbe:
    def run(self, command: tuple[str, ...]) -> Probe:
        # The same environment and program lookup as the commands themselves get.
        env = child_env(os.environ, HOST_TARGET)
        program = find_program(command[0], env)
        if program is None:
            return Probe(False, "")
        args, executable = launch_args(program, command[1:], env, HOST_TARGET)
        # Run outside the repo: yarn, for one, would run a script named in the repo's config.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as neutral_dir:
            cwd = Path(neutral_dir)
            process, tree = start_tree(args, cwd, env, executable, stderr=subprocess.PIPE)
            with process:
                try:
                    stdout, stderr = process.communicate(timeout=_VERSION_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    return Probe(True, "")
                finally:
                    # On Windows the node or java that npm.cmd or gradle.bat started holds the
                    # output open, so a time limit that stops only cmd.exe would never end.
                    tree.close()
        output = (stdout or stderr).decode("utf-8", errors="replace").strip()
        return Probe(True, output.splitlines()[0] if process.returncode == 0 and output else "")


def needs_for(candidate: StackCandidate, target: ExecTarget) -> list[ToolNeed]:
    """The tools the stack's own plan runs. All required."""
    wrapper = JAVA_WRAPPERS[target].get(candidate.manager)
    skipped = {"mvn", "gradle"} if wrapper in candidate.evidence else set()
    needs = []
    for tool in _TOOLS_FOR_MANAGER.get(candidate.manager, ()):
        if tool in skipped:
            continue
        hint = candidate.version_hint if _HINTED_TOOL.get(candidate.stack) == tool else None
        if hint and candidate.stack in _MINIMUM_HINTS:
            hint = f">={hint}"
        needs.append(ToolNeed(tool, True, hint, f"{candidate.manager} needs it"))
    return needs


def needs_for_command(text: str, target: ExecTarget) -> list[ToolNeed]:
    """The tools an allowed command runs on, for a plan only the AI knows. A repo's Maven or
    Gradle wrapper needs only Java, and the venv's python needs nothing installed."""
    verb = command_verb(text, target)
    if verb is None:
        raise ValueError(f"{text!r} is not an allowed command; check it before asking its tools.")
    first = split_command(text)[0].replace("\\", "/").removeprefix("./").lower()
    program = verb[0]
    if first in {wrapper.lower() for wrapper in JAVA_WRAPPERS[target].values()}:
        tools: tuple[str, ...] = ("java",)
    else:
        own = (program,) if program in TOOLS else ()
        tools = _TOOLS_FOR_MANAGER.get(_MANAGER_FOR_PROGRAM.get(program, program), own)
    return [ToolNeed(tool, True, None, f"`{text[:80]}` runs on it") for tool in tools]


def merged_needs(files: Sequence[ToolNeed], ai: Sequence[ToolNeed]) -> list[ToolNeed]:
    """Required if either side says so. The files' version rule wins over the AI's."""
    merged = {need.tool: need for need in files}
    for need in ai:
        if need.tool not in TOOLS:
            raise ValueError(f"{need.tool!r} is not in TOOLS; map AI tool names first.")
        old = merged.get(need.tool)
        if old is None:
            merged[need.tool] = need
        else:
            constraint = old.constraint or need.constraint
            merged[need.tool] = ToolNeed(
                need.tool, old.is_required or need.is_required, constraint, old.reason
            )
    return list(merged.values())


def check_tools(needs: Sequence[ToolNeed], probe: ToolProbe) -> tuple[ToolCheck, ...]:
    return tuple(_check(need, probe) for need in needs)


def check_prerequisites(
    candidate: StackCandidate, probe: ToolProbe, target: ExecTarget
) -> tuple[ToolCheck, ...]:
    return check_tools(needs_for(candidate, target), probe)


def known_tool(ai_name: str) -> str | None:
    """SetupPilot's name for a tool the AI named, or None when SetupPilot cannot check it.
    uv stands in for Python: it fetches the Python version the project needs."""
    name = "uv" if ai_name == "python" else ai_name
    return name if name in TOOLS else None


def recipe_for(tool: str, os_name: HostOs) -> InstallRecipe:
    """The way to install a tool here. Without an entry, the user follows the guidance."""
    spec = TOOLS[tool]
    return spec.install.get(os_name, InstallRecipe(None, spec.guidance, False))


def usable_recipe(tool: str, os_name: HostOs, probe: ToolProbe) -> InstallRecipe:
    """recipe_for, without its command when the program that would install the tool is
    missing: then the install could only fail."""
    recipe = recipe_for(tool, os_name)
    if recipe.command is None or probe.run(_INSTALLERS[recipe.command[0]]).is_found:
        return recipe
    installer = _INSTALLERS[recipe.command[0]][0]
    manual = (
        f"SetupPilot would install it with {installer}, which is not installed. {recipe.manual}"
    )
    return InstallRecipe(None, manual, recipe.needs_admin)


def _check(need: ToolNeed, probe: ToolProbe) -> ToolCheck:
    spec = TOOLS[need.tool]
    result = probe.run(spec.version_command)

    def missing(problem: str) -> ToolCheck:
        return ToolCheck(
            need.tool, False, None, problem, need.is_required, None, need.is_required, need.reason
        )

    if not result.is_found:
        return missing(f"Not installed. {spec.guidance}")
    if not result.output:
        command = " ".join(spec.version_command)
        return missing(f"Installed, but `{command}` did not answer. {spec.guidance}")
    if need.tool == "docker engine" and result.output != "linux":
        # Docker mode and Compose run Linux images, which Windows containers mode cannot.
        return missing(
            f"Docker runs {result.output} containers. Switch Docker Desktop to Linux containers."
        )
    fit = fits(need.constraint, result.output) if need.constraint else None
    # An exact pin (like .nvmrc 20.11) is often stricter than the repo needs, so it only warns.
    blocks = fit is False and spec.version_blocks and not is_pin(need.constraint or "")
    problem = None
    if fit is False:
        problem = (
            f"The repo asks for {need.tool} {need.constraint}, but {result.output} is installed."
        )
        if blocks:
            problem += f" {spec.guidance}"
    is_blocking = need.is_required and blocks
    return ToolCheck(
        need.tool, True, result.output, problem, need.is_required, fit, is_blocking, need.reason
    )
