import io
import subprocess
import urllib.error

import pytest

from setuppilot.config import RepoConfig
from setuppilot.errors import CloneFailed, PrereqMissing
from setuppilot.tools import clone
from setuppilot.tools.clone import (
    CloneRequest,
    check_github_repo,
    clone_repo,
    git_clone,
    git_options,
)
from setuppilot.tools.github_url import RepoRef
from tests.fakes import fake_executable, make_repo

PUBLIC = CloneRequest("public", None)
REF = RepoRef("someone", "demo")
CONFIG = RepoConfig(max_size_mb=1, clone_timeout_seconds=60, max_tree_chars=2000)
# A fake git that prints the arguments it was given and fails.
ECHO_ARGS_AND_FAIL = "import sys; sys.stderr.write(' '.join(sys.argv[1:])); sys.exit(1)"


def no_check(ref):
    return None


def commit_all(repo):
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "change"], check=True)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A local git repo that stands in for GitHub. Our git settings allow only https, so tests
    turn on file:// for this one local remote."""
    source = make_repo(tmp_path / "remote", {"README.md": "# demo\n", "requirements.txt": "x\n"})
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    commit_all(source)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    real = git_clone
    monkeypatch.setattr(clone, "git_clone", lambda url, target, c: real(source.as_uri(), target, c))
    return source


def test_clone_makes_a_folder_named_after_the_repo(tmp_path, remote):
    target = clone_repo(REF, tmp_path, CONFIG, repo_check=no_check, request=PUBLIC)
    assert target == tmp_path / "demo"
    assert (target / "README.md").read_text() == "# demo\n"


def test_repo_bigger_than_the_limit_is_refused_before_its_files_are_checked_out(tmp_path, remote):
    (remote / "big.bin").write_bytes(b"0" * 2_000_000)
    commit_all(remote)
    with pytest.raises(CloneFailed, match="files were not checked out") as error:
        clone_repo(REF, tmp_path, CONFIG, repo_check=no_check, request=PUBLIC)
    assert "1.9 MB" in error.value.details
    assert not (tmp_path / "demo" / "big.bin").exists()


def test_existing_folder_is_not_overwritten(tmp_path):
    (tmp_path / "demo").mkdir()
    with pytest.raises(CloneFailed, match="already there"):
        clone_repo(REF, tmp_path, CONFIG, repo_check=no_check, request=PUBLIC)


def test_missing_destination_folder_is_refused(tmp_path):
    with pytest.raises(CloneFailed, match="does not exist"):
        clone_repo(REF, tmp_path / "nope", CONFIG, repo_check=no_check, request=PUBLIC)


def test_repo_github_does_not_know_is_refused_before_cloning(tmp_path):
    def missing(ref):
        raise CloneFailed("This repo was not found.", details="404")

    with pytest.raises(CloneFailed, match="not found"):
        clone_repo(REF, tmp_path, CONFIG, repo_check=missing, request=PUBLIC)
    assert not (tmp_path / "demo").exists()


def test_clone_is_shallow_https_only_and_without_saved_passwords(tmp_path, monkeypatch):
    fake_executable(tmp_path / "bin", "git", ECHO_ARGS_AND_FAIL)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(CloneFailed) as error:
        git_clone("https://github.com/someone/demo.git", tmp_path / "out", CONFIG)
    sent = error.value.details
    for part in ("--depth 1", "--no-checkout", "protocol.allow=never", "credential.helper="):
        assert part in sent
    assert "-- https://github.com/someone/demo.git" in sent


def test_clone_keeps_line_endings_and_allows_long_paths(tmp_path, monkeypatch):
    fake_executable(tmp_path / "bin", "git", ECHO_ARGS_AND_FAIL)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(CloneFailed) as error:
        git_clone("https://github.com/someone/demo.git", tmp_path / "out", CONFIG)
    assert "core.autocrlf=false" in error.value.details
    assert "core.longpaths=true" in error.value.details


def test_only_windows_checks_out_links_as_plain_files():
    turned_off = ("-c", "core.symlinks=false")
    windows, posix = git_options("windows"), git_options("posix")
    assert turned_off == windows[-2:] and "core.symlinks=false" not in posix


def test_failed_git_clone_reports_git_output(tmp_path, monkeypatch):
    code = "import sys; sys.stderr.write('fatal: repository not found\\n'); sys.exit(128)"
    fake_executable(tmp_path / "bin", "git", code)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(CloneFailed) as error:
        git_clone("https://github.com/someone/demo.git", tmp_path / "out", CONFIG)
    assert "exited with 128: fatal: repository not found" in error.value.details


def test_failed_checkout_does_not_blame_the_url(tmp_path, monkeypatch):
    code = "import sys; sys.exit(1 if 'checkout -q' in ' '.join(sys.argv) else 0)"
    fake_executable(tmp_path / "bin", "git", code)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(CloneFailed, match="^git checkout failed. Check that the disk"):
        git_clone("https://github.com/someone/demo.git", tmp_path / "out", CONFIG)


def test_empty_repo_is_called_empty_not_a_bad_url(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    subprocess.run(["git", "init", "-q", str(empty)], check=True)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    with pytest.raises(CloneFailed, match="may be empty"):
        git_clone(empty.as_uri(), tmp_path / "out", CONFIG)


def test_hanging_clone_is_stopped(tmp_path, monkeypatch):
    fake_executable(tmp_path / "bin", "git", "import time; time.sleep(5)")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    config = RepoConfig(max_size_mb=1, clone_timeout_seconds=0.5, max_tree_chars=2000)
    with pytest.raises(CloneFailed, match="longer than"):
        git_clone("https://github.com/someone/demo.git", tmp_path / "out", config)


def test_missing_git_is_a_prerequisite_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(PrereqMissing, match="git"):
        git_clone("https://github.com/someone/demo.git", tmp_path / "out", CONFIG)


def test_clone_does_not_ask_for_a_password(tmp_path, monkeypatch):
    code = "import os, sys; print('prompt=' + os.environ['GIT_TERMINAL_PROMPT'], file=sys.stderr)"
    code += "; sys.exit(1)"
    fake_executable(tmp_path / "bin", "git", code)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(CloneFailed) as error:
        git_clone("https://github.com/someone/demo.git", tmp_path / "out", CONFIG)
    assert "prompt=0" in error.value.details


def test_github_404_means_missing_or_private(monkeypatch):
    def not_found(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO())

    monkeypatch.setattr(clone.urllib.request, "urlopen", not_found)
    with pytest.raises(CloneFailed, match="not found"):
        check_github_repo(REF)


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError("u", 403, "rate limited", {}, io.BytesIO()),
        urllib.error.URLError("offline"),
        TimeoutError("slow"),
    ],
)
def test_github_check_that_cannot_run_lets_the_clone_decide(monkeypatch, failure):
    def fail(request, timeout):
        raise failure

    monkeypatch.setattr(clone.urllib.request, "urlopen", fail)
    assert check_github_repo(REF) is None
