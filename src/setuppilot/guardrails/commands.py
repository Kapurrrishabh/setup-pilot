"""Check a proposed command before it can run. Only allowlisted tools and sub-commands pass."""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from setuppilot.errors import CommandBlocked, PathBlocked
from setuppilot.guardrails.paths import PathGuard

INSTALL_SCRIPT_WARNING = (
    "Installing packages can run code from the repo and its dependencies (install scripts, "
    "build scripts). That code runs as you and can outlive the time limit. Only approve if you "
    "trust this repo, or use Docker mode."
)

Rule = tuple[tuple[str, ...], ...]
_MAVEN: Rule = (("dependency:resolve",), ("dependency:go-offline",), ("compile",))
_GRADLE: Rule = (("dependencies",), ("assemble",))
# Each tool on PATH, and the sub-commands it may start with. Anything else is blocked.
# Python always goes through uv into the project's .venv, never into the system Python.
ALLOWED: dict[str, Rule] = {
    "uv": (("venv",), ("pip", "install"), ("pip", "check"), ("pip", "list"), ("sync",)),
    "poetry": (("install",), ("check",)),
    "pipenv": (("install",), ("sync",)),
    "pdm": (("install",), ("sync",)),
    "conda": (("env", "create"), ("env", "update")),
    "npm": (("install",), ("ci",), ("ls",)),
    "yarn": (("install",),),
    "pnpm": (("install",),),
    "bun": (("install",),),
    "go": (("mod", "download"), ("mod", "verify"), ("build",)),
    "cargo": (("fetch",), ("build",)),
    "bundle": (("install",), ("check",)),
    "mvn": _MAVEN,
    "gradle": _GRADLE,
    "docker": (
        ("compose", "up", "-d"),
        ("compose", "ps"),
        ("compose", "build"),
        ("compose", "pull"),
        ("compose", "down"),
    ),
    "git": (("submodule", "update", "--init"),),
}
# Programs inside the workspace that may run, with their own rules.
WORKSPACE_PROGRAMS: dict[str, Rule] = {
    "mvnw": _MAVEN,
    "gradlew": _GRADLE,
    ".venv/bin/python": (("-c",),),
}
# Build tools run every goal they are given, so every word must be allowed, not only the first.
_EVERY_WORD_CHECKED = {"mvn", "mvnw", "gradle", "gradlew"}
_NEVER = {"sudo", "su", "doas", "rm", "curl", "wget", "sh", "bash", "zsh", "eval", "chmod", "dd"}
# Flags that install outside the project, or run another program of the caller's choice.
_BLOCKED_FLAGS = {"--system", "--break-system-packages", "--user", "-g", "--global", "--location"}
# Matched without dashes, because go reads -toolexec and --toolexec the same. -ldflags is refused
# whole: -extld inside it picks the program that links.
_TOOL_BLOCKED_FLAGS = {
    "go": {"toolexec", "exec", "ldflags", "gccgoflags"},
    "cargo": {"config", "Z"},
}
NODE_MANAGERS = {"npm", "yarn", "pnpm", "bun"}
_SHELL_PARTS = (";", "|", "&", "`", "$(", "\n", "\r", "\x00")
# `python -c` may only import a module: that is the verify step's import test.
_IMPORT_ONLY = re.compile(r"import [A-Za-z_]\w*(\.[A-Za-z_]\w*)*", re.ASCII)
_MAX_COMMAND_CHARS = 1000


@dataclass(frozen=True)
class SafeCommand:
    """A command that passed the checks. Build it only with validate_command."""

    argv: tuple[str, ...]
    cwd: Path
    text: str


def validate_command(text: str, guard: PathGuard) -> SafeCommand:
    if len(text) > _MAX_COMMAND_CHARS:
        raise _blocked(text, f"It is longer than {_MAX_COMMAND_CHARS} characters.")
    if any(part in text for part in _SHELL_PARTS):
        raise _blocked(text, "It chains commands or has hidden characters. Run one command.")
    try:
        argv = shlex.split(text)
    except ValueError as error:
        raise _blocked(text, f"It could not be read as a command: {error}.") from error
    if not argv:
        raise _blocked(text, "It is empty.")
    program, rule = _program(argv[0], guard, text)
    name = argv[0].removeprefix("./").rsplit("/", 1)[-1]
    for arg in argv[1:]:
        _check_flag(arg, name, text)
    words = _words_to_match(argv)
    if not any(words[: len(prefix)] == prefix for prefix in rule):
        options = ", ".join(" ".join((argv[0], *prefix)) for prefix in rule)
        raise _blocked(text, f"Allowed forms: {options}.")
    if name in _EVERY_WORD_CHECKED:
        allowed = {prefix[0] for prefix in rule}
        extra = [w for w in words if not w.startswith("-") and w not in allowed]
        if extra:
            raise _blocked(text, f"Only these goals are allowed: {', '.join(sorted(allowed))}.")
    if words[:1] == ("-c",) and (len(argv) != 3 or not _IMPORT_ONLY.fullmatch(argv[2])):
        raise _blocked(text, "python -c may only run one 'import module' line.")
    for arg in argv[1:]:
        _check_arg(arg, guard, text)
    return SafeCommand((program, *argv[1:]), guard.workspace, text)


def _words_to_match(argv: list[str]) -> tuple[str, ...]:
    """Words matched against the allowlist. `docker compose -f FILE up` counts as `compose up`."""
    words = argv[1:]
    while argv[0] == "docker" and words[:1] == ["compose"] and words[1:2] in (["-f"], ["--file"]):
        words = [words[0], *words[3:]]
    return tuple(words)


def _program(first: str, guard: PathGuard, text: str) -> tuple[str, Rule]:
    name = first.removeprefix("./")
    if name in WORKSPACE_PROGRAMS:
        # Check the folder, not the file: a venv's python is a link to the system Python.
        folder = _inside(str(Path(name).parent), guard, text)
        return str(folder / Path(name).name), WORKSPACE_PROGRAMS[name]
    if first.rsplit("/", 1)[-1] in _NEVER:
        raise _blocked(text, f"{first} is never allowed.")
    if "/" in first or name not in ALLOWED:
        raise _blocked(text, f"{first} is not on the allowlist.")
    return name, ALLOWED[name]


def _check_flag(arg: str, program: str, text: str) -> None:
    flag = arg.split("=", 1)[0]
    if flag in _BLOCKED_FLAGS or (
        program in NODE_MANAGERS and re.fullmatch(r"-[A-Za-z]*g[A-Za-z]*", arg)
    ):
        raise _blocked(text, f"{arg!r} installs outside this project.")
    if arg.startswith("-") and flag.lstrip("-") in _TOOL_BLOCKED_FLAGS.get(program, set()):
        raise _blocked(text, f"{arg!r} could make {program} run another program.")


def _check_arg(arg: str, guard: PathGuard, text: str) -> None:
    if arg.startswith((">", "<", "2>")):
        raise _blocked(text, f"{arg!r} redirects output. Run the command without it.")
    is_glued_short_flag = arg.startswith("-") and not arg.startswith("--") and len(arg) > 2
    if is_glued_short_flag and "=" not in arg and any(part in arg for part in ("/", "..", "~")):
        raise _blocked(text, f"{arg!r}: put a space between a short flag and a path.")
    values = [arg]
    if "=" in arg:
        values.append(arg.split("=", 1)[1])
    if "file:" in arg:
        values.append(arg.split("file:", 1)[1])
    for value in values:
        if "://" in value:
            if not value.removeprefix("git+").startswith("https://"):
                raise _blocked(text, f"{arg!r}: only https:// links are allowed.")
        elif "/" in value or value.startswith((".", "~")):
            _inside(value, guard, text)


def _inside(path: str, guard: PathGuard, text: str) -> Path:
    try:
        return guard.check(path)
    except PathBlocked as error:
        raise _blocked(text, error.details) from error


def _blocked(text: str, reason: str) -> CommandBlocked:
    return CommandBlocked(
        "This command is not allowed, so it was not run.",
        details=f"Command: {text[:200]!r}. {reason}",
    )
