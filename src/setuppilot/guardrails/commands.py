"""Check a proposed command before it can run. Only allowlisted tools and sub-commands pass."""

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from setuppilot.errors import CommandBlocked, PathBlocked
from setuppilot.guardrails.paths import PathGuard

INSTALL_SCRIPT_WARNING = (
    "Installing packages can run code from the repo and its dependencies (install scripts, "
    "build scripts). That code runs as you and can outlive the time limit. Only approve if you "
    "trust this repo, or use Docker mode."
)

# Where a command runs: on Windows itself, or on macOS, Linux and inside Docker's Linux images.
ExecTarget = Literal["posix", "windows"]
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
    # MSBuild and Composer run the project's own build targets and scripts, as npm does.
    "dotnet": (("restore",), ("build",)),
    "composer": (
        ("install",),
        ("validate", "--no-check-publish"),
        ("check-platform-reqs", "--no-plugins"),
    ),
    "dart": (("pub", "get"),),
    "flutter": (("pub", "get"),),
}
VENV_PYTHON: dict[ExecTarget, str] = {
    "posix": ".venv/bin/python",
    "windows": ".venv/Scripts/python.exe",
}
# A repo's own build script for Java; it replaces an installed Maven or Gradle.
JAVA_WRAPPERS: dict[ExecTarget, dict[str, str]] = {
    "posix": {"maven": "mvnw", "gradle": "gradlew"},
    "windows": {"maven": "mvnw.cmd", "gradle": "gradlew.bat"},
}
# Programs inside the workspace that may run, with their own rules.
WORKSPACE_PROGRAMS: dict[ExecTarget, dict[str, Rule]] = {
    target: {
        wrappers["maven"]: _MAVEN,
        wrappers["gradle"]: _GRADLE,
        VENV_PYTHON[target]: (("-c",),),
    }
    for target, wrappers in JAVA_WRAPPERS.items()
}
# Build tools run every goal they are given, so every word must be allowed, not only the first.
_EVERY_WORD_CHECKED = {"mvn", "gradle"} | {w for ws in JAVA_WRAPPERS.values() for w in ws.values()}
_NEVER = {"sudo", "su", "doas", "rm", "curl", "wget", "sh", "bash", "zsh", "eval", "chmod", "dd"}
# Flags that install outside the project, or run another program of the caller's choice.
_BLOCKED_FLAGS = {"--system", "--break-system-packages", "--user", "-g", "--global", "--location"}
# Matched without dashes, because go reads -toolexec and --toolexec the same. -ldflags is refused
# whole: -extld inside it picks the program that links.
_TOOL_BLOCKED_FLAGS = {
    "go": {"toolexec", "exec", "ldflags", "gccgoflags"},
    "cargo": {"config", "Z"},
    # -t:Run starts the built program, and a logger loads any code. MSBuild ignores case.
    "dotnet": {"t", "target", "logger", "l", "distributedlogger", "dl"},
}
# MSBuild's -p can set any property, and some run a program (PreBuildEvent) or load a file
# (CustomBeforeMicrosoftCommonTargets), so only these may be set.
_DOTNET_PROPERTY = re.compile(r"(?:--?|/)(?:p|property|rp|restoreproperty)(?::(.*))?", re.I | re.S)
_DOTNET_PROPERTIES = ("Configuration", "TargetFramework")
# Never taken from the AI: they pick where packages come from or which program runs the scripts,
# and a README or a build's output can ask for them. You can still type them in an edit.
_NOT_FROM_THE_AI = {
    "--index-url", "--extra-index-url", "--default-index", "--index", "--find-links",
    "--registry", "--script-shell", "--source", "--configfile", "--userconfig",
}  # fmt: skip
_NOT_FROM_THE_AI_SHORT = {"uv": ("-i", "-f"), "dotnet": ("-s",)}
# uv must be told the repo's .venv, so a parent folder's venv or the repo's own uv settings
# cannot send packages anywhere else. Checked on edits too, not only on SetupPilot's plans.
_REQUIRED_ARGS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("uv", "pip", "install"): ("--python", ".venv"),
    ("uv", "pip", "check"): ("--python", ".venv"),
}
NODE_MANAGERS = {"npm", "yarn", "pnpm", "bun"}
_SHELL_PARTS = (";", "|", "&", "`", "$(", "\n", "\r", "\x00")
# `python -c` may only import a module: that is the verify step's import test.
_IMPORT_ONLY = re.compile(r"import [A-Za-z_]\w*(\.[A-Za-z_]\w*)*", re.ASCII)
MAX_COMMAND_CHARS = 1000
# A Windows drive, as in C:\Users or the drive-relative D:req.txt.
WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")


@dataclass(frozen=True)
class SafeCommand:
    """A command that passed the checks. Build it only with validate_command or recipe_command."""

    argv: tuple[str, ...]
    cwd: Path
    text: str
    # Docker mode names its container, so a stop reaches the container and not only `docker`.
    container_name: str | None


def split_command(text: str) -> list[str]:
    """Split like a POSIX shell, but keep backslashes: on Windows they separate folders."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)


def validate_command(text: str, guard: PathGuard, target: ExecTarget) -> SafeCommand:
    if len(text) > MAX_COMMAND_CHARS:
        raise _blocked(text, f"It is longer than {MAX_COMMAND_CHARS} characters.")
    if any(part in text for part in _SHELL_PARTS):
        raise _blocked(text, "It chains commands or has hidden characters. Run one command.")
    try:
        argv = split_command(text)
    except ValueError as error:
        raise _blocked(text, f"It could not be read as a command: {error}.") from error
    if not argv:
        raise _blocked(text, "It is empty.")
    program, rule = _program(argv[0], guard, text, target)
    name = _program_name(argv[0], target).rsplit("/", 1)[-1]
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
    _check_required(name, argv, text)
    for arg in argv[1:]:
        _check_arg(arg, guard, text)
    return SafeCommand((program, *argv[1:]), guard.workspace, text, None)


def validate_ai_command(text: str, guard: PathGuard, target: ExecTarget) -> SafeCommand:
    """validate_command, for a command the AI wrote: some flags are only taken from you."""
    command = validate_command(text, guard, target)
    argv = split_command(text)
    short = _NOT_FROM_THE_AI_SHORT.get(_program_name(argv[0], target).rsplit("/", 1)[-1], ())
    for arg in argv[1:]:
        flag = arg.split("=", 1)[0]
        if flag in _NOT_FROM_THE_AI or flag.endswith(":registry") or arg.startswith(short):
            raise _blocked(
                text,
                f"{arg!r} can change where packages come from or what runs the scripts, so "
                "SetupPilot does not take it from the AI. Run it yourself if you trust it.",
            )
    return command


def _check_required(name: str, argv: list[str], text: str) -> None:
    for (program, *sub), (flag, value) in _REQUIRED_ARGS.items():
        if name != program or argv[1 : 1 + len(sub)] != sub:
            continue
        args = argv[1 + len(sub) :]
        given = [args[i + 1] if i + 1 < len(args) else "" for i, a in enumerate(args) if a == flag]
        given += [a.split("=", 1)[1] for a in args if a.startswith(f"{flag}=")]
        # uv also reads -p, alone or glued to other short flags; only the long form is allowed.
        short = [a for a in args if re.fullmatch(r"-[A-Za-z]*p.*", a)]
        if given != [value] or short:
            raise _blocked(
                text, f"Add {flag} {value} once (not -p), so packages go only into {value}."
            )


def _words_to_match(argv: list[str]) -> tuple[str, ...]:
    """Words matched against the allowlist. `docker compose -f FILE up` counts as `compose up`."""
    words = argv[1:]
    while argv[0] == "docker" and words[:1] == ["compose"] and words[1:2] in (["-f"], ["--file"]):
        words = [words[0], *words[3:]]
    return tuple(words)


def _program_name(first: str, target: ExecTarget) -> str:
    """.\\mvnw.cmd is ./mvnw.cmd, and Windows ignores case in program names."""
    name = first.replace("\\", "/").removeprefix("./")
    return name.lower() if target == "windows" else name


def _program(first: str, guard: PathGuard, text: str, target: ExecTarget) -> tuple[str, Rule]:
    name = _program_name(first, target)
    programs = {_program_name(key, target): key for key in WORKSPACE_PROGRAMS[target]}
    if name in programs:
        key = programs[name]
        # Check the folder, not the file: a venv's python is a link to the system Python.
        folder = _inside(str(Path(key).parent), guard, text)
        return str(folder / Path(key).name), WORKSPACE_PROGRAMS[target][key]
    if name.rsplit("/", 1)[-1] in _NEVER:
        raise _blocked(text, f"{first} is never allowed.")
    if "/" in first.replace("\\", "/") or name not in ALLOWED:
        raise _blocked(text, f"{first} is not on the allowlist.")
    return name, ALLOWED[name]


def _check_flag(arg: str, program: str, text: str) -> None:
    flag = arg.split("=", 1)[0]
    if flag in _BLOCKED_FLAGS or (
        program in NODE_MANAGERS and re.fullmatch(r"-[A-Za-z]*g[A-Za-z]*", arg)
    ):
        raise _blocked(text, f"{arg!r} installs outside this project.")
    # MSBuild writes switches as -name:value or /name:value, and reads more from an @file.
    is_dotnet = program == "dotnet"
    name = flag.lstrip("-/" if is_dotnet else "-").split(":", 1)[0]
    name = name.lower() if is_dotnet else name
    is_switch = arg.startswith("-") or (is_dotnet and arg.startswith("/"))
    is_glued_cargo_z = program == "cargo" and arg.startswith("-Z")  # -Zbuild-std and friends
    is_response_file = is_dotnet and arg.startswith("@")
    if (
        is_glued_cargo_z
        or is_response_file
        or (is_switch and name in _TOOL_BLOCKED_FLAGS.get(program, set()))
    ):
        raise _blocked(text, f"{arg!r} could make {program} run another program.")
    if is_dotnet and (switch := _DOTNET_PROPERTY.fullmatch(arg)):
        # -p:A=1,B=2 sets two properties, so each one is checked.
        pairs = re.split(r"[,;]", switch[1] or "")
        allowed = {known.lower() for known in _DOTNET_PROPERTIES}
        if not all("=" in p and p.split("=", 1)[0].lower() in allowed for p in pairs):
            names = " or ".join(_DOTNET_PROPERTIES)
            raise _blocked(text, f"{arg!r}: only -p:Name=Value is allowed, with Name {names}.")


def _check_arg(arg: str, guard: PathGuard, text: str) -> None:
    if arg.startswith((">", "<", "2>")):
        raise _blocked(text, f"{arg!r} redirects output. Run the command without it.")
    is_glued_short_flag = arg.startswith("-") and not arg.startswith("--") and len(arg) > 2
    has_path = any(part in arg for part in ("/", "\\", "..", "~")) or WINDOWS_DRIVE.search(arg)
    if is_glued_short_flag and "=" not in arg and has_path:
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
        elif _looks_like_path(value):
            _inside(value, guard, text)


def _looks_like_path(value: str) -> bool:
    has_separator = "/" in value or "\\" in value
    return has_separator or value.startswith((".", "~")) or bool(WINDOWS_DRIVE.match(value))


def _inside(path: str, guard: PathGuard, text: str) -> Path:
    try:
        return guard.check(path)
    except PathBlocked as error:
        raise _blocked(text, error.details) from error


def command_verb(text: str, target: ExecTarget) -> tuple[str, ...] | None:
    """The program and the allowed form it starts with, like ("npm", "ci"), or None when it is
    not allowed. A repo's Maven or Gradle wrapper counts as mvn or gradle."""
    try:
        argv = split_command(text)
    except ValueError:
        return None
    if not argv:
        return None
    name = _program_name(argv[0], target)
    workspace = {_program_name(key, target): key for key in WORKSPACE_PROGRAMS[target]}
    if name in workspace:
        key = workspace[name]
        rule = WORKSPACE_PROGRAMS[target][key]
        tools = {wrapper: tool for tool, wrapper in JAVA_WRAPPERS[target].items()}
        program = {"maven": "mvn", "gradle": "gradle"}.get(tools.get(key, ""), "python")
    elif name in ALLOWED:
        program, rule = name, ALLOWED[name]
    else:
        return None
    words = _words_to_match(argv)
    prefix = next((p for p in rule if words[: len(p)] == p), None)
    return None if prefix is None else (program, *prefix)


def allowlist_digest(target: ExecTarget) -> str:
    """The allowlist in a few lines, for the LLM. Made from the rules, so it cannot drift."""

    # Whole command starts, with required arguments written in: tried on real Groq, a rule at
    # the end was missed, and "mvn: compile" got its colon copied into commands.
    def forms(program: str, rule: Rule) -> str:
        def form(prefix: tuple[str, ...]) -> str:
            if prefix == ("-c",):
                return f'{program} -c "import <module>"'
            return " ".join((program, *prefix, *_REQUIRED_ARGS.get((program, *prefix), ())))

        return " | ".join(form(p) for p in rule)

    lines = [forms(program, rule) for program, rule in ALLOWED.items()]
    lines += [
        forms(key if key.startswith(".") else "./" + key, rule)
        for key, rule in WORKSPACE_PROGRAMS[target].items()
    ]
    # Real Groq copied the bare form `uv pip install --python .venv`, which installs nothing.
    pip = " ".join(("uv", "pip", "install", *_REQUIRED_ARGS[("uv", "pip", "install")]))
    lines.append(
        "Rules: a form is only how a command starts, so add what it works on, as in "
        f"`{pip} -e .` or `{pip} -r requirements.txt`, never `{pip}` alone. "
        "One command, no shell characters (; | & > < ` $( ), never "
        + ", ".join(sorted(_NEVER))
        + ". No .., ~ or absolute paths: stay in the project folder. No "
        + ", ".join(sorted(_BLOCKED_FLAGS))
        + ". mvn and gradle may only run the goals listed. Use / in paths."
    )
    return "\n".join(lines)


# How SetupPilot may install a tool. The package name comes from a fixed table, never from the AI
# or the user; these forms keep even that table from building any other command.
RecipeKind = Literal["winget", "brew", "uv_tool"]
_RECIPE_ARGV: dict[RecipeKind, Callable[[str], tuple[str, ...]]] = {
    # Flags from https://learn.microsoft.com/en-us/windows/package-manager/winget/install
    "winget": lambda name: (
        "winget",
        "install",
        "--id",
        name,
        "-e",
        "--source",
        "winget",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--disable-interactivity",
    ),  # fmt: skip
    "brew": lambda name: ("brew", "install", name),
    # Installs for you only (no admin rights), into uv's own tool folder.
    "uv_tool": lambda name: ("uv", "tool", "install", name),
}
_RECIPE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,60}")


def recipe_command(kind: RecipeKind, name: str, cwd: Path) -> SafeCommand:
    """cwd should be an empty folder, so no repo file can change what the installer does."""
    if not _RECIPE_NAME.fullmatch(name):
        raise CommandBlocked(
            "This install command is not one SetupPilot knows, so it was not run.",
            details=f"The {kind} package name {name!r} has characters no package name has.",
        )
    argv = _RECIPE_ARGV[kind](name)
    return SafeCommand(argv, cwd, " ".join(argv), None)


def _blocked(text: str, reason: str) -> CommandBlocked:
    return CommandBlocked(
        "This command is not allowed, so it was not run.",
        details=f"Command: {text[:200]!r}. {reason}",
    )
