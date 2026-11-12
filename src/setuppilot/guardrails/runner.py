"""Run one checked command: no shell, no secrets, a time limit, and only the end of the output."""

import contextlib
import ntpath
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from setuppilot.config import RunnerConfig
from setuppilot.errors import CommandBlocked, CommandTimeout, InstallFailed, PrereqMissing
from setuppilot.guardrails.commands import ExecTarget, SafeCommand
from setuppilot.guardrails.process_tree import STOP_GRACE_SECONDS, ProcessTree, start_tree
from setuppilot.redact import redact

HOST_TARGET: ExecTarget = "windows" if sys.platform == "win32" else "posix"
_READ_CHUNK = 4096
_READER_JOIN_SECONDS = 5
# Wait in short steps: on Windows one long wait does not notice Ctrl+C until it ends.
_WAIT_SLICE_SECONDS = 0.5
# PROVISIONAL: `docker stop` waits STOP_GRACE_SECONDS, then the engine must still answer.
_DOCKER_STOP_TIMEOUT_SECONDS = 15
# Where Go downloads modules from (for networks that block proxy.golang.org). GOFLAGS is left out
# on purpose: it could add flags the allowlist blocks.
GO_DOWNLOAD_ENV = ("GOPROXY", "GOSUMDB", "GOPRIVATE", "GONOPROXY", "GONOSUMDB")
# Only these reach a command, matched in any case (http_proxy too). Everything else (API keys,
# tokens, ssh-agent, VIRTUAL_ENV) is left out, because install scripts run code we do not trust.
_PASSED_ENV_SHARED = {
    "HOME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "JAVA_HOME", "GOROOT", "RUSTUP_HOME", "CARGO_HOME",
    # So docker commands reach the same engine as the pre-check. HOME already exposes ~/.docker.
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
    *GO_DOWNLOAD_ENV,
}  # fmt: skip
_PASSED_ENV: dict[ExecTarget, set[str]] = {
    "posix": _PASSED_ENV_SHARED | {"USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ"},
    # PROVISIONAL: what Windows programs need to find the system (Winsock needs SYSTEMROOT),
    # temp space and the user's folders. Confirm with the Windows CI environment tests.
    "windows": _PASSED_ENV_SHARED | {
        "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "OS",
        "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432", "NUMBER_OF_PROCESSORS",
        "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "USERNAME",
        "TEMP", "TMP", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
        "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
        "PROGRAMDATA", "ALLUSERSPROFILE",
    },
}  # fmt: skip
_FIXED_ENV: dict[ExecTarget, dict[str, str]] = {
    "posix": {},
    "windows": {
        # cmd.exe, which runs npm.cmd and gradlew.bat, would look in the repo before PATH.
        "NoDefaultCurrentDirectoryInExePath": "1",
        # Git Credential Manager must not open a sign-in window.
        "GCM_INTERACTIVE": "never",
    },
}
# cmd.exe reads a .bat or .cmd file's arguments by its own rules, and no quoting is safe for all
# of them (the BatBadBut problem). So, like Rust, refuse any other character instead of escaping.
_BATCH_SAFE_ARG = re.compile(r"[A-Za-z0-9_.,:=@+/\\-]+")
# Keep each project's environment inside the repo folder, not in your home folder.
IN_REPO_ENV = {
    # Settings in the repo's own poetry.toml cannot turn the venv off (env beats config).
    "POETRY_VIRTUALENVS_CREATE": "true",
    "POETRY_VIRTUALENVS_IN_PROJECT": "true",
    "PIPENV_VENV_IN_PROJECT": "1",
    "BUNDLE_PATH": "vendor/bundle",
}
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class CommandOutput:
    argv: tuple[str, ...]
    exit_code: int
    output_tail: str
    is_output_cut: bool
    duration_ms: int
    # Cancel or the time limit stopped it. On Windows a stopped command exits with 1, not -15.
    is_stopped: bool


def child_env(environ: Mapping[str, str], host: ExecTarget) -> dict[str, str]:
    """What a command running on `host` may see of `environ` (os.environ)."""
    env = {
        k: v
        for k, v in environ.items()
        if k.upper() in _PASSED_ENV[host] or (host == "posix" and k.startswith("LC_"))
    }
    env["PATH"] = _absolute_entries(environ.get("PATH", ""), host)
    fixed = IN_REPO_ENV | _FIXED_ENV[host] | {"GIT_TERMINAL_PROMPT": "0", "PIP_NO_INPUT": "1"}
    return env | fixed


def _absolute_entries(path: str, host: ExecTarget) -> str:
    """Relative PATH entries would let a file in the repo stand in for a real tool."""
    if host == "posix":
        return ":".join(p for p in path.split(":") if posixpath.isabs(p))
    # Python 3.12 calls \Windows absolute, but it means a folder on whatever drive is current.
    kept = [p for p in path.split(";") if ntpath.isabs(p) and not p.startswith(("\\", "/"))]
    return ";".join(kept)


def find_program(program: str, env: Mapping[str, str]) -> str | None:
    """The full path of a program on the command's own PATH. A path is returned as it is."""
    if "/" in program or "\\" in program:
        return program
    found = shutil.which(program, path=env["PATH"])
    if found is not None and not os.path.isabs(found):
        # Every PATH entry is absolute, so this came from the current folder (Windows looks
        # there first unless NoDefaultCurrentDirectoryInExePath is set).
        raise CommandBlocked(
            f"A program called {program} is in the current folder, so it was not run.",
            details=f"{found} would run instead of the {program} on PATH. Start SetupPilot from "
            "its own folder.",
        )
    return found


def launch_args(
    program: str, args: Sequence[str], env: Mapping[str, str], host: ExecTarget
) -> tuple[list[str] | str, str | None]:
    """Popen's arguments and executable. Windows starts .exe files itself, and .bat and .cmd
    files only through cmd.exe, so those get a command line built here, not by Windows."""
    if host == "posix" or program.lower().endswith(".exe"):
        return [program, *args], None
    if not program.lower().endswith((".bat", ".cmd")):
        raise PrereqMissing(
            f"Windows cannot start {program}: only .exe, .bat and .cmd files run there.",
            details="A repo's Maven or Gradle wrapper is mvnw.cmd or gradlew.bat on Windows.",
        )
    return _batch_command_line(env["SYSTEMROOT"], program, args)


def _batch_command_line(system_root: str, program: str, args: Sequence[str]) -> tuple[str, str]:
    # cmd.exe expands %NAME% (and !NAME! when set up to) even inside quotes.
    if "%" in program or "!" in program:
        raise CommandBlocked(
            "This program cannot be run safely on Windows, so it was not run.",
            details=f"Its folder has % or ! in its name, which cmd.exe changes: {program}. Move "
            "the repo to a folder without them.",
        )
    for arg in args:
        if not _BATCH_SAFE_ARG.fullmatch(arg):
            raise CommandBlocked(
                "This command is not allowed on Windows, so it was not run.",
                details=f"{ntpath.basename(program)} is a batch file, which cmd.exe reads, so "
                f"{arg!r} is refused there. Only letters, digits and _ . , : = @ + / \\ - are "
                "safe in its arguments.",
            )
    cmd_exe = ntpath.join(system_root, "System32", "cmd.exe")
    # /d skips the AutoRun commands in the registry; /s keeps the quotes below as written.
    return f'"{cmd_exe}" /d /s /c ""{program}" {" ".join(args)}"', cmd_exe


def run_command(
    command: SafeCommand, config: RunnerConfig, on_start: Callable[[ProcessTree], None]
) -> CommandOutput:
    """on_start gets the running command, so a Cancel button can stop it."""
    started = time.monotonic()
    env = child_env(os.environ, HOST_TARGET)
    program = find_program(command.argv[0], env)
    if program is None:
        raise PrereqMissing(
            f"{command.argv[0]} is not installed or not on PATH.",
            details=f"Could not start {redact(command.text)!r}. Install it, or add its folder to "
            "PATH, then try again.",
        )
    process, tree = _start(command, program, env)
    if command.container_name is not None:
        tree = _ContainerTree(tree, process, program, command.container_name, env)
    tail = _TailBuffer(config.max_output_chars)
    reader = threading.Thread(target=tail.read_all, args=(process.stdout,), daemon=True)
    try:
        on_start(tree)
        reader.start()
        exit_code = _wait(process, config.timeout_seconds)
    except subprocess.TimeoutExpired as error:
        tree.stop()
        reader.join(timeout=_READER_JOIN_SECONDS)
        raise CommandTimeout(
            f"The command ran longer than {config.timeout_seconds:.0f} seconds and was stopped.",
            details=f"{redact(command.text)!r}. Raise runner.timeout_seconds in config.yaml if it "
            f"needs longer. Last output:\n{tail.text()}",
        ) from error
    finally:
        # Also on Ctrl+C: children left running would go on with no time limit.
        tree.close()
    reader.join(timeout=_READER_JOIN_SECONDS)
    if reader.is_alive():
        raise InstallFailed(
            "A background process started by this command is still running.",
            details=f"{redact(command.text)!r} left a process that escaped the time limit and "
            "keeps its output open. Stop it yourself, or use Docker mode. Output so far:\n"
            f"{tail.text()}",
        )
    text = tail.text()
    return CommandOutput(
        command.argv,
        exit_code,
        text,
        tail.is_cut,
        int((time.monotonic() - started) * 1000),
        tree.was_stopped,
    )


def _wait(process: subprocess.Popen[bytes], timeout_seconds: float) -> int:
    deadline = time.monotonic() + timeout_seconds
    while (exit_code := process.poll()) is None:
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, timeout_seconds)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_WAIT_SLICE_SECONDS)
    return exit_code


def _start(
    command: SafeCommand, program: str, env: Mapping[str, str]
) -> tuple[subprocess.Popen[bytes], ProcessTree]:
    args, executable = launch_args(program, command.argv[1:], env, HOST_TARGET)
    try:
        return start_tree(args, command.cwd, env, executable, stderr=subprocess.STDOUT)
    except OSError as error:
        raise _not_started(command.argv[0], error, HOST_TARGET) from error


def _not_started(program: str, error: OSError, host: ExecTarget) -> PrereqMissing:
    """Why a program that was found could not start, in words that fit the system."""
    if host == "windows":
        return PrereqMissing(
            f"{program} could not be started: {error.strerror}.",
            details="If an antivirus program blocked it, allow it and try again, or use Docker "
            "mode.",
        )
    if isinstance(error, PermissionError):
        return PrereqMissing(
            f"{program} cannot be run: it is not marked as a program.",
            details=f"Run `chmod +x {program}` yourself if you trust it, then try again.",
        )
    return PrereqMissing(
        f"{program} could not be started: {error.strerror}.",
        details="If the file is there, check that its first line starts with #! and that it "
        "uses Unix (LF) line endings.",
    )


@dataclass
class _ContainerTree:
    """Docker mode. Stopping `docker run` does not always stop its container (on Windows it
    cannot pass a stop on), so the container is stopped by name first."""

    tree: ProcessTree
    process: subprocess.Popen[bytes]
    docker: str
    name: str
    env: Mapping[str, str]

    @property
    def was_stopped(self) -> bool:
        return self.tree.was_stopped

    def stop(self) -> None:
        try:
            self._stop_container()
        finally:
            self.tree.stop()

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self._stop_container()
        finally:
            self.tree.close()

    def _stop_container(self) -> None:
        stop = ("stop", "--time", str(STOP_GRACE_SECONDS), self.name)
        args, executable = launch_args(self.docker, stop, self.env, HOST_TARGET)
        # A container that has already gone makes docker stop fail, which is fine; and the
        # forced stop of the tree comes next whatever happens here.
        with contextlib.suppress(subprocess.TimeoutExpired):
            subprocess.run(  # noqa: S603 - fixed docker command, no shell
                args,
                executable=executable,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_DOCKER_STOP_TIMEOUT_SECONDS,
                check=False,
            )


class _TailBuffer:
    """Keeps only the last max_chars of output, so huge output cannot fill memory."""

    def __init__(self, max_chars: int) -> None:
        self._max_bytes = max_chars * 4
        self._max_chars = max_chars
        self._data = bytearray()
        self._dropped_bytes = False

    def read_all(self, stream) -> None:
        while chunk := stream.read1(_READ_CHUNK):
            self._data += chunk
            if len(self._data) > self._max_bytes:
                del self._data[: len(self._data) - self._max_bytes]
                self._dropped_bytes = True
        stream.close()

    @property
    def is_cut(self) -> bool:
        return self._dropped_bytes or len(self._clean()) > self._max_chars

    def text(self) -> str:
        text = self._clean()
        if self.is_cut:
            # Drop the first, partial line too: it may hold half of a secret.
            text = text[-self._max_chars :].split("\n", 1)[-1]
        return text

    def _clean(self) -> str:
        # Hide secrets before cutting, so a secret is never split across the cut.
        decoded = bytes(self._data).decode("utf-8", errors="replace")
        return _CONTROL_CHARS.sub("", redact(decoded))
