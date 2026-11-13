"""The Browse button: the server opens this computer's own folder window, in a child process.

A web page can never learn a folder's full path. This app serves only this computer, so the
server and the browser share one screen. A child process keeps a stuck window out of the server,
and macOS draws windows only from a process's main thread, which a request thread is not.
"""

import importlib.util
import logging
import ntpath
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from setuppilot.errors import SetupPilotError
from setuppilot.guardrails.runner import HOST_TARGET, child_env, find_program, launch_args
from setuppilot.redact import redact

log = logging.getLogger(__name__)

# PROVISIONAL: long enough to look through a few folders. A forgotten window is closed after it,
# so Browse works again. Watching people use it would confirm the number.
WINDOW_SECONDS = 600
_TITLE = "Pick the folder to put the repo in"
# SetupPilot's own window scripts exit with this when no folder was picked, so a crash (exit 1)
# is never taken for a cancel.
_CANCEL_EXIT = 3
_ERROR_CHARS = 300
# A window must reach the screen; every command also gets child_env. None of these is a secret.
_SCREEN_ENV = (
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
)  # fmt: skip
_OSASCRIPT = "/usr/bin/osascript"
# The folder comes in as the script's argument, never as part of its text.
_OSASCRIPT_LINES = (
    "on run argv",
    f'return POSIX path of (choose folder with prompt "{_TITLE}" default location '
    "(POSIX file (item 1 of argv)))",
    "end run",
)
_TK_SCRIPT = f"""
import sys, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.askdirectory(
    parent=root, initialdir=sys.argv[1], mustexist=True, title="{_TITLE}"
)
root.destroy()
if not path:
    sys.exit({_CANCEL_EXIT})
sys.stdout.buffer.write(path.encode("utf-8"))
"""
# The folder comes in through SETUPPILOT_START. Raw UTF-8 bytes, because a pipe gets the console's
# code page otherwise. Not -File (the execution policy blocks it) and not -EncodedCommand
# (antivirus programs flag it).
_POWERSHELL_SCRIPT = "; ".join(
    (
        "$ErrorActionPreference = 'Stop'",
        "Add-Type -AssemblyName System.Windows.Forms",
        "$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}",
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog",
        f"$dialog.Description = '{_TITLE}'",
        "$dialog.SelectedPath = $env:SETUPPILOT_START",
        "if ($dialog.ShowDialog($owner) -ne [System.Windows.Forms.DialogResult]::OK) "
        f"{{ exit {_CANCEL_EXIT} }}",
        "$bytes = [System.Text.Encoding]::UTF8.GetBytes($dialog.SelectedPath)",
        "[Console]::OpenStandardOutput().Write($bytes, 0, $bytes.Length)",
    )
)

PickerName = Literal["osascript", "tkinter", "powershell", "zenity", "kdialog"]


@dataclass(frozen=True)
class FolderPicker:
    name: PickerName
    # The full path of the program that opens the window.
    program: str


@dataclass(frozen=True)
class NoFolderWindow:
    """This computer cannot open a folder window, so the page asks you to type the path."""

    problem: str


@dataclass(frozen=True)
class Picked:
    path: Path


@dataclass(frozen=True)
class Cancelled:
    # None when you closed the window yourself.
    reason: str | None


class FolderWindowFailed(SetupPilotError):
    """The folder window could not open, or ended with an error."""


def find_picker(
    system: str,
    env: Mapping[str, str],
    which: Callable[[str], str | None],
    has_tk: bool,
    python: str,
) -> FolderPicker | NoFolderWindow:
    """The folder window this computer can open. system is a sys.platform value; which gives a
    program's full path, or None when it is not there."""
    if env.get("SSH_CONNECTION"):
        return NoFolderWindow("SetupPilot runs over SSH, so a window would open on the other end")
    tk = [FolderPicker("tkinter", python)] if has_tk else []
    if system == "win32":
        root = env.get("SYSTEMROOT")
        shell = root and which(
            ntpath.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        )
        # An absolute path, so a powershell.exe in the current folder or on PATH never runs.
        found = tk + ([FolderPicker("powershell", shell)] if shell else [])
        missing = "neither Python's tkinter nor Windows PowerShell was found"
    elif system == "darwin":
        script = which(_OSASCRIPT)
        found = ([FolderPicker("osascript", script)] if script else []) + tk
        missing = f"neither {_OSASCRIPT} nor Python's tkinter was found"
    else:
        if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
            return NoFolderWindow("no screen was found: DISPLAY and WAYLAND_DISPLAY are not set")
        names: tuple[PickerName, ...] = ("zenity", "kdialog")
        found = [FolderPicker(name, path) for name in names if (path := which(name))]
        # Tk draws through X11 only, so on Wayland it needs XWayland's DISPLAY.
        found += tk if env.get("DISPLAY") else []
        missing = "zenity, kdialog and Python's tkinter are not installed"
    return found[0] if found else NoFolderWindow(missing)


def this_computers_picker() -> FolderPicker | NoFolderWindow:
    env = _window_env(os.environ)

    def which(program: str) -> str | None:
        found = find_program(program, env)
        return found if found is not None and os.path.isfile(found) else None

    has_tk = importlib.util.find_spec("_tkinter") is not None
    return find_picker(sys.platform, os.environ, which, has_tk, sys.executable)


def pick_folder(picker: FolderPicker, start: Path, timeout_seconds: float) -> Picked | Cancelled:
    """Open the window at start and wait for it. start must be a full path to a folder."""
    env = _window_env(os.environ)
    if picker.name == "powershell":
        env["SETUPPILOT_START"] = str(start)
    args, executable = launch_args(picker.program, _arguments(picker.name, start), env, HOST_TARGET)
    try:
        done = subprocess.run(  # noqa: S603 - fixed program and script; the folder is only data
            args,
            executable=executable,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        minutes = round(timeout_seconds / 60)
        return Cancelled(
            f"The folder window was open for {minutes} minutes, so SetupPilot closed it."
        )
    except OSError as error:
        raise _failed(picker, f"it could not start ({error.strerror})") from error
    # A newline ends the path; a byte order mark may start it on Windows.
    output = done.stdout.decode("utf-8", errors="replace").rstrip("\r\n").lstrip("\ufeff")
    errors = done.stderr.decode("utf-8", errors="replace")
    if _is_cancel(picker.name, done.returncode, errors):
        return Cancelled(None)
    if done.returncode != 0 or not output:
        raise _failed(picker, f"exit code {done.returncode}: {errors.strip()[-_ERROR_CHARS:]}")
    return Picked(Path(output))


def _window_env(environ: Mapping[str, str]) -> dict[str, str]:
    return child_env(environ, HOST_TARGET) | {k: environ[k] for k in _SCREEN_ENV if k in environ}


def _arguments(name: PickerName, start: Path) -> list[str]:
    if name == "osascript":
        return [*(part for line in _OSASCRIPT_LINES for part in ("-e", line)), str(start)]
    if name == "tkinter":
        return ["-I", "-c", _TK_SCRIPT, str(start)]
    if name == "powershell":
        return ["-NoProfile", "-NonInteractive", "-STA", "-Command", _POWERSHELL_SCRIPT]
    if name == "zenity":
        # The ending slash opens the window inside the folder, not at its parent.
        return ["--file-selection", "--directory", f"--title={_TITLE}", f"--filename={start}/"]
    return ["--title", _TITLE, "--getexistingdirectory", str(start)]


def _is_cancel(name: PickerName, exit_code: int, errors: str) -> bool:
    if name in ("tkinter", "powershell"):
        return exit_code == _CANCEL_EXIT
    if name == "osascript":
        # AppleScript error -128 is "User canceled".
        return exit_code == 1 and "(-128)" in errors
    return exit_code == 1


def _failed(picker: FolderPicker, what: str) -> FolderWindowFailed:
    problem = redact(what)
    log.warning("folder window failed", extra={"picker": picker.name, "problem": problem})
    return FolderWindowFailed(
        "Could not open a folder window.",
        details=f"{picker.name}: {problem}. Type the folder's path instead.",
    )
