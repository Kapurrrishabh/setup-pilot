import subprocess
import sys

import pytest

from setuppilot.errors import PathBlocked
from setuppilot.guardrails.paths import PathGuard

ON_WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="junctions exist only on Windows")


@pytest.fixture
def guard(tmp_path):
    (tmp_path / "work" / "src").mkdir(parents=True)
    return PathGuard(tmp_path / "work")


def junction(link, target):
    """A Windows junction: a folder link that needs no admin rights."""
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True)


def test_relative_path_inside_is_allowed(guard):
    assert guard.check("src/app.py") == guard.workspace / "src" / "app.py"


def test_absolute_path_inside_is_allowed(guard):
    assert guard.check(guard.workspace / "src") == guard.workspace / "src"


@pytest.mark.parametrize("path", ["../secret", "src/../../x", "/etc/passwd", "~/.ssh/id_rsa", "~"])
def test_paths_that_leave_the_workspace_are_blocked(guard, path):
    with pytest.raises(PathBlocked):
        guard.check(path)


def test_dot_dot_is_blocked_even_when_it_stays_inside(guard):
    with pytest.raises(PathBlocked) as error:
        guard.check("src/../src")
    assert "'..'" in error.value.details


@pytest.mark.parametrize(
    "path",
    [r"..\secret", r"src\..\..\x", r"\\attacker\share\x", "//attacker/share/x", r"\\.\pipe\x",
     r"\\?\C:\x", "D:secret.txt", "nul", "src/NUL.txt", "com1", "lpt9.log", "CONOUT$"],
)  # fmt: skip
def test_windows_paths_that_leave_the_workspace_or_open_a_device_are_blocked(guard, path):
    with pytest.raises(PathBlocked):
        guard.check(path)


@pytest.mark.parametrize(
    "path",
    [
        r"\\attacker\share\pkg",
        "//attacker/share/pkg",
        r"/\attacker\share\pkg",
        r"\/attacker\share\pkg",
    ],
)
def test_network_path_is_refused_before_it_is_opened(guard, path):
    with pytest.raises(PathBlocked) as error:
        guard.check(path)
    assert "Network and device paths" in error.value.details


def test_folder_whose_name_only_starts_like_a_device_is_allowed(guard):
    assert guard.check("console/app.py") == guard.workspace / "console" / "app.py"


def test_symlink_that_escapes_is_blocked(guard, tmp_path, can_symlink):
    (tmp_path / "outside.txt").write_text("secret")
    (guard.workspace / "link.txt").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(PathBlocked) as error:
        guard.check("link.txt")
    assert "outside the workspace" in error.value.details


def test_symlinked_folder_that_escapes_is_blocked(guard, tmp_path, can_symlink):
    (tmp_path / "other").mkdir()
    (guard.workspace / "linkdir").symlink_to(tmp_path / "other", target_is_directory=True)
    with pytest.raises(PathBlocked):
        guard.check("linkdir/file.txt")


@ON_WINDOWS
def test_junction_that_escapes_is_blocked(guard, tmp_path):
    (tmp_path / "other").mkdir()
    junction(guard.workspace / "linkdir", tmp_path / "other")
    with pytest.raises(PathBlocked):
        guard.check("linkdir/file.txt")


def test_symlink_that_stays_inside_is_allowed(guard, can_symlink):
    (guard.workspace / "alias").symlink_to(guard.workspace / "src", target_is_directory=True)
    assert guard.check("alias") == guard.workspace / "src"


def test_sibling_folder_with_the_same_prefix_is_blocked(guard, tmp_path):
    (tmp_path / "work-other").mkdir()
    with pytest.raises(PathBlocked):
        guard.check(tmp_path / "work-other")


def test_path_with_a_nul_byte_is_blocked(guard):
    with pytest.raises(PathBlocked):
        guard.check("a\x00b")


def test_repo_links_that_escape_are_listed(guard, tmp_path, can_symlink):
    (guard.workspace / "inside").symlink_to(guard.workspace / "src", target_is_directory=True)
    (guard.workspace / ".venv").symlink_to(tmp_path, target_is_directory=True)
    assert [e.split(" ->")[0] for e in guard.links_that_escape(("inside", ".venv"))] == [".venv"]


@ON_WINDOWS
def test_repo_junctions_that_escape_are_listed(guard, tmp_path):
    junction(guard.workspace / ".venv", tmp_path)
    assert [e.split(" ->")[0] for e in guard.links_that_escape((".venv",))] == [".venv"]
