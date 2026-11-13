"""The Browse button's folder window: which one each system opens, and how its answer is read."""

import sys
from pathlib import Path

import pytest

from setuppilot.web import folder_picker
from setuppilot.web.folder_picker import (
    Cancelled,
    FolderPicker,
    FolderWindowFailed,
    NoFolderWindow,
    Picked,
    find_picker,
    pick_folder,
)

PYTHON = "/usr/bin/python3"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
# Quotes and a space: a folder name like this must never become part of a script.
START = Path('/home/zoë/it\'s "my" work')


def which_of(*present):
    return lambda program: next((p for p in present if p.endswith(program)), None)


def choose(system, env, present=(), has_tk=False):
    return find_picker(system, env, which_of(*present), has_tk, PYTHON)


WINDOWS = {"SYSTEMROOT": r"C:\Windows"}


def test_windows_uses_tkinter_when_python_has_it():
    assert choose("win32", WINDOWS, [POWERSHELL], has_tk=True) == FolderPicker("tkinter", PYTHON)


def test_windows_without_tkinter_uses_powershell_by_its_full_path():
    picker = choose("win32", WINDOWS, [POWERSHELL])
    assert picker == FolderPicker("powershell", POWERSHELL)


def test_windows_never_looks_up_powershell_by_name():
    asked = []
    find_picker("win32", WINDOWS, lambda p: asked.append(p) or p, False, PYTHON)
    assert asked == [POWERSHELL]


def test_windows_with_neither_says_what_is_missing():
    choice = choose("win32", WINDOWS)
    assert isinstance(choice, NoFolderWindow) and "PowerShell" in choice.problem


def test_macos_uses_osascript():
    picker = choose("darwin", {}, ["/usr/bin/osascript"], has_tk=True)
    assert picker == FolderPicker("osascript", "/usr/bin/osascript")


def test_macos_without_osascript_falls_to_tkinter():
    assert choose("darwin", {}, has_tk=True) == FolderPicker("tkinter", PYTHON)


@pytest.mark.parametrize("system", ["darwin", "linux", "win32"])
def test_over_ssh_there_is_no_window_because_it_would_open_on_the_other_end(system):
    env = {"SSH_CONNECTION": "10.0.0.2 5000 10.0.0.1 22", "DISPLAY": ":0"} | WINDOWS
    choice = choose(system, env, ["/usr/bin/osascript", "/usr/bin/zenity", POWERSHELL], True)
    assert isinstance(choice, NoFolderWindow) and "SSH" in choice.problem


def test_linux_without_a_screen_has_no_window_and_says_why():
    choice = choose("linux", {}, ["/usr/bin/zenity"], has_tk=True)
    assert isinstance(choice, NoFolderWindow) and "DISPLAY" in choice.problem


@pytest.mark.parametrize(
    ("present", "has_tk", "name"),
    [(["/usr/bin/zenity", "/usr/bin/kdialog"], True, "zenity"),
     (["/usr/bin/kdialog"], True, "kdialog"), ([], True, "tkinter")],
)  # fmt: skip
def test_linux_with_a_screen_picks_zenity_then_kdialog_then_tkinter(present, has_tk, name):
    choice = choose("linux", {"DISPLAY": ":0"}, present, has_tk)
    assert isinstance(choice, FolderPicker) and choice.name == name


def test_linux_on_wayland_alone_cannot_use_tkinter():
    choice = choose("linux", {"WAYLAND_DISPLAY": "wayland-0"}, has_tk=True)
    assert isinstance(choice, NoFolderWindow) and "tkinter" in choice.problem


def test_wsl_with_wslg_opens_the_linux_window():
    env = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "WSL_DISTRO_NAME": "Ubuntu"}
    assert choose("linux", env, has_tk=True) == FolderPicker("tkinter", PYTHON)


class Finished:
    def __init__(self, code=0, out=b"", err=b""):
        self.returncode, self.stdout, self.stderr = code, out, err


@pytest.fixture
def captured(monkeypatch):
    """What the window's process would be started with; it answers /picked."""
    calls = []

    def run(args, **options):
        calls.append((args, options["env"]))
        return Finished(0, out=b"/picked\n")

    monkeypatch.setattr(folder_picker.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("name", ["osascript", "tkinter", "zenity", "kdialog"])
def test_the_start_folder_is_only_ever_its_own_argument(captured, name):
    pick_folder(FolderPicker(name, "/usr/bin/tool.exe"), START, 5)
    ((args, _),) = captured
    holding = [arg for arg in args if str(START).rstrip("/") in arg]
    assert len(holding) == 1 and holding[0].removeprefix("--filename=").rstrip("/") == str(START)


def test_powershell_gets_the_start_folder_through_its_environment(captured, monkeypatch):
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    pick_folder(FolderPicker("powershell", "/usr/bin/tool.exe"), START, 5)
    ((args, env),) = captured
    assert env["SETUPPILOT_START"] == str(START) and not any(str(START) in a for a in args)


def test_the_window_gets_no_secrets(captured, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")
    monkeypatch.setenv("DISPLAY", ":1")
    pick_folder(FolderPicker("zenity", "/usr/bin/zenity.exe"), START, 5)
    ((_, env),) = captured
    assert "GROQ_API_KEY" not in env and env["DISPLAY"] == ":1"


def fake_tk(monkeypatch, script):
    """Run the tkinter picker's real process with a stand-in for the script that opens Tk."""
    monkeypatch.setattr(folder_picker, "_TK_SCRIPT", script)
    return FolderPicker("tkinter", sys.executable)


def test_a_picked_folder_comes_back_as_its_path_even_with_letters_outside_ascii(monkeypatch):
    picker = fake_tk(monkeypatch, "import sys; sys.stdout.buffer.write(sys.argv[1].encode())")
    assert pick_folder(picker, START, 30) == Picked(START)


def test_closing_the_window_is_a_cancel_not_an_error(monkeypatch):
    picker = fake_tk(monkeypatch, "import sys; sys.exit(3)")
    assert pick_folder(picker, START, 30) == Cancelled(None)


def test_a_window_that_crashes_is_an_error_with_the_end_of_what_it_said(monkeypatch):
    script = "import sys; sys.stderr.write('_tkinter.TclError: no display name'); sys.exit(1)"
    with pytest.raises(FolderWindowFailed) as raised:
        pick_folder(fake_tk(monkeypatch, script), START, 30)
    assert "no display name" in raised.value.details and "Type the" in raised.value.details


def test_a_window_left_open_too_long_is_closed_and_says_so(monkeypatch):
    picker = fake_tk(monkeypatch, "import time; time.sleep(30)")
    result = pick_folder(picker, START, 0.5)
    assert isinstance(result, Cancelled) and "closed it" in result.reason


def fake_program(tmp_path, body):
    program = tmp_path / "fake-tool"
    program.write_text(f"#!{sys.executable}\nimport sys\n{body}\n", encoding="utf-8")
    program.chmod(0o755)
    return str(program)


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Windows runs only .exe tools")


@posix_only
def test_osascript_cancel_is_its_error_minus_128(tmp_path):
    body = "sys.stderr.write('execution error: User canceled. (-128)'); sys.exit(1)"
    picker = FolderPicker("osascript", fake_program(tmp_path, body))
    assert pick_folder(picker, START, 30) == Cancelled(None)


@posix_only
def test_osascript_with_no_way_to_show_a_window_is_an_error(tmp_path):
    body = "sys.stderr.write('execution error: No user interaction allowed. (-1713)'); sys.exit(1)"
    with pytest.raises(FolderWindowFailed, match="folder window"):
        pick_folder(FolderPicker("osascript", fake_program(tmp_path, body)), START, 30)


@posix_only
def test_zenity_exit_1_is_a_cancel_and_its_answer_is_read_without_the_newline(tmp_path):
    cancel = FolderPicker("zenity", fake_program(tmp_path, "sys.exit(1)"))
    assert pick_folder(cancel, START, 30) == Cancelled(None)
    body = "print(sys.argv[-1].removeprefix('--filename=').rstrip('/'))"
    (tmp_path / "again").mkdir()
    picked = FolderPicker("zenity", fake_program(tmp_path / "again", body))
    assert pick_folder(picked, START, 30) == Picked(START)


def test_a_program_that_cannot_start_is_a_clear_error(tmp_path):
    name = "no-such-python.exe" if sys.platform == "win32" else "no-such-python"
    missing = FolderPicker("tkinter", str(tmp_path / name))
    with pytest.raises(FolderWindowFailed, match="Could not open"):
        pick_folder(missing, START, 30)


def test_this_computers_choice_is_made_without_opening_anything():
    assert isinstance(folder_picker.this_computers_picker(), FolderPicker | NoFolderWindow)
