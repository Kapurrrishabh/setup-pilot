import pytest

from setuppilot.config import GitHubConfig
from setuppilot.errors import PathBlocked
from setuppilot.github.api import GitHubError
from setuppilot.github.reader import SnapshotTooBig, read_snapshot
from setuppilot.tools.github_url import RepoRef
from tests.github_fakes import (
    COMMIT,
    DOWNLOAD_URL,
    REF,
    REPO_URL,
    TARBALL_URL,
    TREE_URL,
    FakeGitHub,
    FakeTokens,
    client_for,
    ok,
    repo_json,
    serve_repo,
    targz,
    tree_json,
)

CONFIG = GitHubConfig(app=None, max_repo_pages=10, max_tarball_mb=1)
MAX_FILE_BYTES = 1000


def everything(path: str) -> bool:
    return True


def snapshot_of(files, links=None, wanted=everything, tree=None, is_private=False, tokens=None):
    fake = FakeGitHub()
    serve_repo(fake, targz(files, links), tree or tree_json(files), is_private)
    return read_snapshot(REF, client_for(fake, tokens), CONFIG, wanted, MAX_FILE_BYTES), fake


def test_every_file_is_listed_and_only_wanted_ones_are_read():
    files = {"README.md": b"# demo\n", "src/app.py": b"print(1)\n"}
    snapshot, _ = snapshot_of(files, wanted=lambda path: path.endswith(".md"))
    assert ([e.path for e in snapshot.entries], dict(snapshot.files)) == (
        ["README.md", "src/app.py"],
        {"README.md": "# demo\n"},
    )


def test_reading_takes_three_api_calls_and_one_download():
    _, fake = snapshot_of({"README.md": b"hi"})
    assert fake.urls() == [REPO_URL, TREE_URL, TARBALL_URL, DOWNLOAD_URL]


def test_the_commit_and_privacy_come_from_github():
    snapshot, _ = snapshot_of({"README.md": b"hi"}, is_private=True, tokens=FakeTokens())
    assert (snapshot.commit_sha, snapshot.is_private, snapshot.default_branch) == (
        COMMIT,
        True,
        "main",
    )


def test_installed_and_built_folders_are_listed_but_not_read():
    snapshot, _ = snapshot_of({"node_modules/x/index.js": b"x", "dist/app.js": b"y"})
    assert (len(snapshot.entries), dict(snapshot.files)) == (2, {})


def test_a_binary_file_is_not_read():
    snapshot, _ = snapshot_of({"logo.png": b"\x89PNG\x00\x01"})
    assert dict(snapshot.files) == {}


def test_a_file_over_the_size_limit_is_listed_but_not_read():
    snapshot, _ = snapshot_of({"big.md": b"x" * (MAX_FILE_BYTES + 1)})
    assert (snapshot.total_bytes, dict(snapshot.files)) == (MAX_FILE_BYTES + 1, {})


def test_links_are_listed_but_never_read():
    snapshot, _ = snapshot_of({"README.md": b"hi"}, links={"secret.md": "/etc/passwd"})
    assert ([(e.path, e.is_link) for e in snapshot.entries], list(snapshot.files)) == (
        [("README.md", False), ("secret.md", True)],
        ["README.md"],
    )


@pytest.mark.parametrize(("is_truncated", "total_bytes"), [(False, 2**20 + 1), (True, None)])
def test_a_repo_too_big_for_the_limit_is_refused_before_any_download(is_truncated, total_bytes):
    fake = FakeGitHub()
    files = {"data.bin": b"x" * (2**20 + 1)}
    serve_repo(fake, targz({}), tree_json(files, truncated=is_truncated))
    with pytest.raises(SnapshotTooBig) as raised:
        read_snapshot(REF, client_for(fake), CONFIG, everything, MAX_FILE_BYTES)
    assert (raised.value.total_bytes, DOWNLOAD_URL in fake.urls()) == (total_bytes, False)


def test_a_cut_short_tree_still_stops_a_download_that_is_too_big():
    fake = FakeGitHub()
    files = {"a.bin": b"x" * 2**19, "b.bin": b"y" * (2**19 + 1)}
    serve_repo(fake, targz(files), tree_json({}, truncated=True))
    with pytest.raises(SnapshotTooBig):
        read_snapshot(REF, client_for(fake), CONFIG, everything, MAX_FILE_BYTES)


@pytest.mark.parametrize(
    "outside",
    [{"o-r-621e497/../evil.md": b"x"}, {"other-folder/evil.md": b"x"}, {"/evil.md": b"x"}],
)
def test_a_download_with_a_file_outside_the_repo_folder_is_refused(outside):
    fake = FakeGitHub()
    serve_repo(fake, targz({"README.md": b"hi"}, outside=outside), tree_json({}))
    with pytest.raises(PathBlocked):
        read_snapshot(REF, client_for(fake), CONFIG, everything, MAX_FILE_BYTES)


def test_a_download_that_is_not_an_archive_is_a_clear_error():
    fake = FakeGitHub()
    serve_repo(fake, b"<html>busy</html>", tree_json({}))
    with pytest.raises(GitHubError, match="could not be read as an archive"):
        read_snapshot(REF, client_for(fake), CONFIG, everything, MAX_FILE_BYTES)


def test_a_download_that_does_not_name_its_commit_is_not_used():
    fake = FakeGitHub()
    serve_repo(fake, targz({"README.md": b"hi"}, commit=""), tree_json({}))
    with pytest.raises(GitHubError, match="which commit"):
        read_snapshot(REF, client_for(fake), CONFIG, everything, MAX_FILE_BYTES)


def test_a_renamed_repo_is_read_under_its_new_name():
    fake = FakeGitHub()
    files = {"README.md": b"hi"}
    serve_repo(fake, targz(files), tree_json(files))
    fake.on("GET", "https://api.github.com/repos/o/old-name", ok(repo_json()))
    old = RepoRef("o", "old-name")
    snapshot = read_snapshot(old, client_for(fake), CONFIG, everything, MAX_FILE_BYTES)
    assert snapshot.ref == REF
