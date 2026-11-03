"""Run one checked command: no shell, no secrets, a time limit, and only the end of the output."""

import contextlib
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from setuppilot.config import RunnerConfig
from setuppilot.errors import CommandTimeout, InstallFailed, PrereqMissing
from setuppilot.guardrails.commands import SafeCommand
from setuppilot.redact import redact

_READ_CHUNK = 4096
_READER_JOIN_SECONDS = 5
# PROVISIONAL: time for `docker run` and `docker compose` to stop their containers after SIGTERM.
_STOP_GRACE_SECONDS = 3
# Where Go downloads modules from (for networks that block proxy.golang.org). GOFLAGS is left out
# on purpose: it could add flags the allowlist blocks.
GO_DOWNLOAD_ENV = ("GOPROXY", "GOSUMDB", "GOPRIVATE", "GONOPROXY", "GONOSUMDB")
# Only these reach a command. Everything else (API keys, tokens, ssh-agent, VIRTUAL_ENV) is left
# out, because install scripts run code we do not trust.
_PASSED_ENV = {
    "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "JAVA_HOME", "GOROOT", "RUSTUP_HOME", "CARGO_HOME",
    # So docker commands reach the same engine as the pre-check. HOME already exposes ~/.docker.
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
    *GO_DOWNLOAD_ENV,
}  # fmt: skip
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


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _PASSED_ENV or k.startswith("LC_")}
    # Relative PATH entries would let a file in the repo stand in for a real tool.
    path = [p for p in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(p)]
    env["PATH"] = os.pathsep.join(path)
    return env | IN_REPO_ENV | {"GIT_TERMINAL_PROMPT": "0", "PIP_NO_INPUT": "1"}


def run_command(
    command: SafeCommand, config: RunnerConfig, on_start: Callable[[int], None]
) -> CommandOutput:
    """on_start gets the process group id, so a Cancel button can stop the command."""
    started = time.monotonic()
    process = _start(command)
    on_start(process.pid)
    tail = _TailBuffer(config.max_output_chars)
    reader = threading.Thread(target=tail.read_all, args=(process.stdout,), daemon=True)
    reader.start()
    try:
        exit_code = process.wait(timeout=config.timeout_seconds)
    except subprocess.TimeoutExpired as error:
        _stop_group(process)
        reader.join(timeout=_READER_JOIN_SECONDS)
        raise CommandTimeout(
            f"The command ran longer than {config.timeout_seconds:.0f} seconds and was stopped.",
            details=f"{redact(command.text)!r}. Raise runner.timeout_seconds in config.yaml if it "
            f"needs longer. Last output:\n{tail.text()}",
        ) from error
    finally:
        # Also on Ctrl+C: children left running would go on with no time limit.
        _stop_group(process)
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
        command.argv, exit_code, text, tail.is_cut, int((time.monotonic() - started) * 1000)
    )


def _start(command: SafeCommand) -> subprocess.Popen:
    try:
        return subprocess.Popen(  # noqa: S603 - argv was checked by validate_command
            command.argv,
            cwd=command.cwd,
            env=child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except PermissionError as error:
        raise PrereqMissing(
            f"{command.argv[0]} cannot be run: it is not marked as a program.",
            details=f"Run `chmod +x {command.argv[0]}` yourself if you trust it, then try again.",
        ) from error
    except OSError as error:
        # A path means a file from the repo, so "not on PATH" would send you the wrong way.
        if not isinstance(error, FileNotFoundError) or "/" in command.argv[0]:
            raise PrereqMissing(
                f"{command.argv[0]} could not be started: {error.strerror}.",
                details="If the file is there, check that its first line starts with #! and that "
                "it uses Unix (LF) line endings.",
            ) from error
        raise PrereqMissing(
            f"{command.argv[0]} is not installed or not on PATH.",
            details=f"Could not start {redact(command.text)!r}: {error.strerror}.",
        ) from error


def cancel_group(group_id: int) -> None:
    """Stop a running command from another thread (the web app's Cancel button)."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group_id, signal.SIGTERM)
        time.sleep(_STOP_GRACE_SECONDS)
        os.killpg(group_id, signal.SIGKILL)


def _stop_group(process: subprocess.Popen) -> None:
    """Stop the command's whole process group: ask nicely, then force it."""
    # macOS answers EPERM, not ESRCH, for a group that holds only finished processes.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_STOP_GRACE_SECONDS)
        os.killpg(process.pid, signal.SIGKILL)


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
