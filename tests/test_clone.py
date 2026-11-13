import base64
import json
import subprocess

import httpx
import pytest

from setuppilot.config import RepoConfig
from setuppilot.errors import CloneFailed, PathBlocked, PrereqMissing
from setuppilot.tools.clone import (
    SIGN_IN_OFF,
    CloneRequest,
    GitHubAccess,
    clone_repo,
    git_clone,
    git_options,
    head_commit,
)
from setuppilot.tools.github_url import RepoRef
from tests.fakes import FakeClock, fake_executable
from tests.github_fakes import (
    REPO_URL,
    TREE_URL,
    USER,
    FakeGitHub,
    GitServer,
    MemoryKeyring,
    client_for,
    commit_all,
    git_repo,
    github_access,
    ok,
    repo_json,
    tree_json,
    tree_of,
)

PUBLIC = CloneRequest("public", None, None)
SIGNED_IN = CloneRequest("github", None, USER.id)
REF = RepoRef("o", "r")
CONFIG = RepoConfig(max_size_mb=1, clone_timeout_seconds=60, max_tree_chars=2000)
TOKEN = "ghu_cloneSecret0123456789"  # noqa: S105 - a fake token
BASIC = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
# A fake git that prints the arguments it was given and fails.
ECHO_ARGS_AND_FAIL = "import sys; sys.stderr.write(' '.join(sys.argv[1:])); sys.exit(1)"
# A fake git that writes down its arguments and git settings, fails when FAIL_ON is one of its
# arguments, and answers rev-parse with TREE_SHA.
RECORDING_GIT = (
    "import json, os, sys\n"
    "env = {k: v for k, v in os.environ.items() if k.startswith(('GIT_', 'SSH_'))}\n"
    "with open(os.environ['RECORD'], 'a') as f: f.write(json.dumps([sys.argv[1:], env]) + '\\n')\n"
    "if os.environ.get('FAIL_ON') in sys.argv: sys.exit(1)\n"
    "heads = [a for a in sys.argv if a.startswith('HEAD')]\n"
    "if 'rev-parse' in sys.argv: print(*[os.environ['TREE_SHA'] for _ in heads])"
)


def public(fake):
    return GitHubAccess(client_for(fake), signed_in=None)


def signed_in(fake, token=TOKEN):
    return github_access(fake, FakeClock(), MemoryKeyring(), token)


def serve_api(fake, tree, full_name="o/r", is_private=False):
    fake.on("GET", REPO_URL, ok(repo_json(is_private) | {"full_name": full_name}))
    fake.on("GET", TREE_URL, ok(tree))


@pytest.fixture
def local_git(tmp_path, monkeypatch):
    """Our git settings allow only https. Tests also allow file:// and http:// (for 127.0.0.1) in
    a global git config of their own, which also keeps the user's own settings out."""
    config = tmp_path / "gitconfig"
    config.write_text(
        '[protocol "file"]\n\tallow = always\n[protocol "http"]\n\tallow = always\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))


def clone_from(monkeypatch, url):
    """git clones from url instead of github.com; GitHub's API is still the fake."""
    monkeypatch.setattr(RepoRef, "clone_url", property(lambda self: url))


@pytest.fixture
def remote(tmp_path, monkeypatch, local_git):
    """A local git repo that stands in for GitHub, and a fake API that lists its files."""
    source = git_repo(tmp_path / "remote", {"README.md": "# demo\n", "requirements.txt": "x\n"})
    clone_from(monkeypatch, source.as_uri())
    fake = FakeGitHub()
    serve_api(fake, tree_of(source))
    return source, fake


@pytest.fixture
def recording_git(tmp_path, monkeypatch):
    """A fake git on PATH whose clone fails (set FAIL_ON to change that). Returns a function that
    reads back each call's (argv, git settings)."""
    record = tmp_path / "git-calls.jsonl"
    record.touch()
    fake_executable(tmp_path / "bin", "git", RECORDING_GIT)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("RECORD", str(record))
    monkeypatch.setenv("FAIL_ON", "clone")
    return lambda: [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]


def test_clone_makes_a_folder_named_after_the_repo(tmp_path, remote):
    target = clone_repo(REF, tmp_path, CONFIG, public(remote[1]), PUBLIC)
    assert target == tmp_path / "r"
    assert (target / "README.md").read_text(encoding="utf-8") == "# demo\n"


def test_repo_github_lists_as_too_big_is_refused_before_anything_is_downloaded(
    tmp_path, recording_git
):
    fake = FakeGitHub()
    serve_api(fake, tree_json({"big.bin": b"0" * 2_000_000}))
    with pytest.raises(CloneFailed, match="files were not checked out") as error:
        clone_repo(REF, tmp_path, CONFIG, public(fake), PUBLIC)
    assert "1.9 MB" in error.value.details and "git data" not in error.value.details
    assert recording_git() == [] and not (tmp_path / "r").exists()


def test_without_githubs_list_a_too_big_repo_is_refused_before_its_files_are_checked_out(
    tmp_path, remote
):
    source, _ = remote
    (source / "big.bin").write_bytes(b"0" * 2_000_000)
    commit_all(source)
    limited = FakeGitHub()
    used_up = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"}
    limited.on("GET", REPO_URL, httpx.Response(403, headers=used_up))
    with pytest.raises(CloneFailed, match="files were not checked out") as error:
        clone_repo(REF, tmp_path, CONFIG, public(limited), PUBLIC)
    assert "1.9 MB" in error.value.details and "git data is left" in error.value.details
    assert not (tmp_path / "r" / "big.bin").exists()


def test_existing_folder_is_not_overwritten(tmp_path):
    (tmp_path / "r").mkdir()
    with pytest.raises(CloneFailed, match="already there"):
        clone_repo(REF, tmp_path, CONFIG, public(FakeGitHub()), PUBLIC)


def test_missing_destination_folder_is_refused(tmp_path):
    with pytest.raises(CloneFailed, match="does not exist"):
        clone_repo(REF, tmp_path / "nope", CONFIG, public(FakeGitHub()), PUBLIC)


def test_a_private_or_missing_repo_without_sign_in_says_to_sign_in(tmp_path, recording_git):
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(CloneFailed, match="private or missing.*sign in"):
        clone_repo(REF, tmp_path, CONFIG, public(fake), PUBLIC)
    assert recording_git() == [] and not (tmp_path / "r").exists()


def test_a_repo_the_sign_in_cannot_read_says_the_app_may_not_be_installed_on_it(
    tmp_path, recording_git
):
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(CloneFailed) as error:
        clone_repo(REF, tmp_path, CONFIG, signed_in(fake), SIGNED_IN)
    assert "installed on" in error.value.details and recording_git() == []


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9"}),
        httpx.ConnectError("offline"),
    ],
    ids=["read limit used up", "offline"],
)
def test_github_that_cannot_answer_lets_a_public_clone_go_ahead(tmp_path, remote, failure):
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, failure)
    assert (clone_repo(REF, tmp_path, CONFIG, public(fake), PUBLIC) / "README.md").exists()


def test_github_that_cannot_answer_stops_a_signed_in_clone(tmp_path, recording_git):
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, httpx.ConnectError("offline"))
    with pytest.raises(CloneFailed, match="Could not reach GitHub"):
        clone_repo(REF, tmp_path, CONFIG, signed_in(fake), SIGNED_IN)
    assert recording_git() == []


def test_a_renamed_repo_is_cloned_under_githubs_own_name(tmp_path, recording_git):
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, ok(repo_json() | {"full_name": "o/new-name"}))
    fake.on("GET", f"{REPO_URL[:-2]}/new-name/git/trees/main?recursive=1", ok(tree_json({})))
    with pytest.raises(CloneFailed):
        clone_repo(REF, tmp_path, CONFIG, public(fake), PUBLIC)
    (argv, _), *_ = recording_git()
    assert argv[-2:] == ["https://github.com/o/new-name.git", str(tmp_path / "r")]


def test_a_signed_in_clone_sends_the_token_only_as_a_header_in_gits_settings(
    tmp_path, recording_git
):
    fake = FakeGitHub()
    serve_api(fake, tree_json({"README.md": b"x"}))
    with pytest.raises(CloneFailed) as error:
        clone_repo(REF, tmp_path, CONFIG, signed_in(fake), SIGNED_IN)
    (argv, env), *_ = recording_git()
    assert TOKEN not in " ".join(argv) and "http.followRedirects=false" in argv
    assert (env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == (
        "1",
        "http.https://github.com/o/r.git.extraHeader",
        f"Authorization: Basic {BASIC}",
    )
    shown = f"{error.value.message} {error.value.details}"
    assert TOKEN not in shown and BASIC not in shown


def test_a_public_clone_sends_no_token_and_follows_githubs_usual_redirects(tmp_path, recording_git):
    fake = FakeGitHub()
    serve_api(fake, tree_json({"README.md": b"x"}))
    with pytest.raises(CloneFailed):
        clone_repo(REF, tmp_path, CONFIG, public(fake), PUBLIC)
    (argv, env), *_ = recording_git()
    assert "GIT_CONFIG_COUNT" not in env and "http.followRedirects=false" not in argv


def test_inherited_git_traces_and_settings_never_reach_git(tmp_path, recording_git, monkeypatch):
    inherited = {
        "GIT_TRACE": "1", "GIT_TRACE_CURL": "1", "GIT_CURL_VERBOSE": "1", "GIT_TRACE_REDACT": "0",
        "GIT_REDACT_COOKIES": "x", "GIT_CONFIG_PARAMETERS": "'http.extraheader'='X-Leak: 1'",
        "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_1": "http.proxy", "GIT_CONFIG_VALUE_1": "evil",
        "GIT_ASKPASS": "/bin/echo",
    }  # fmt: skip
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    fake = FakeGitHub()
    serve_api(fake, tree_json({"README.md": b"x"}))
    with pytest.raises(CloneFailed):
        clone_repo(REF, tmp_path, CONFIG, signed_in(fake), SIGNED_IN)
    (_, env), *_ = recording_git()
    assert set(inherited) & set(env) == {"GIT_CONFIG_COUNT"} and env["GIT_CONFIG_COUNT"] == "1"


def test_a_signed_in_clone_works_with_the_header_and_leaves_no_token_in_the_clone(
    tmp_path, monkeypatch, local_git
):
    source = git_repo(tmp_path / "remote", {"README.md": "# private\n"})
    server = GitServer(tmp_path / "served", required_auth=f"Basic {BASIC}")
    try:
        server.add_repo(source, "o/r")
        clone_from(monkeypatch, f"{server.url}/o/r.git")
        fake = FakeGitHub()
        serve_api(fake, tree_of(source), is_private=True)
        target = clone_repo(REF, tmp_path, CONFIG, signed_in(fake), SIGNED_IN)
    finally:
        server.close()
    assert (target / "README.md").read_text(encoding="utf-8") == "# private\n"
    assert server.seen_auth and set(server.seen_auth) == {f"Basic {BASIC}"}
    saved = b"".join(p.read_bytes() for p in (target / ".git").rglob("*") if p.is_file())
    assert TOKEN.encode() not in saved and BASIC.encode() not in saved
    assert b"extraheader" not in (target / ".git" / "config").read_bytes().lower()


def test_a_sub_folder_clone_downloads_only_that_folders_files(tmp_path, monkeypatch, local_git):
    files = {"README.md": "root\n", "sub/app/main.py": "print(1)\n", "other/o.txt": "other\n"}
    source = git_repo(tmp_path / "remote", files)
    tree = tree_of(source)
    server = GitServer(tmp_path / "served", required_auth=f"Basic {BASIC}")
    try:
        server.add_repo(source, "o/r")
        clone_from(monkeypatch, f"{server.url}/o/r.git")
        fake = FakeGitHub()
        serve_api(fake, tree)
        request = CloneRequest("github", "sub/app", USER.id)
        target = clone_repo(REF, tmp_path, CONFIG, signed_in(fake), request)
    finally:
        server.close()
    assert (target / "sub/app/main.py").exists() and (target / "README.md").exists()
    assert not (target / "other").exists()
    listing = subprocess.run(
        ["git", "-C", str(target), "rev-list", "--objects", "--missing=print", "HEAD"],
        capture_output=True, encoding="utf-8", check=True,
    ).stdout  # fmt: skip
    missing = {line[1:] for line in listing.splitlines() if line.startswith("?")}
    blob = {item["path"]: item["sha"] for item in tree["tree"]}
    assert blob["other/o.txt"] in missing and blob["sub/app/main.py"] not in missing
    # Every request carried the header, including the one that fetched the folder's files.
    assert set(server.seen_auth) == {f"Basic {BASIC}"}


SPLIT_REPO = {
    "README.md": b"x" * 600_000,
    "packages/NOTES.md": b"x" * 50_000,
    "packages/web/a.js": b"x" * 300_000,
    "packages/api/big.bin": b"x" * 5_000_000,
}


def test_a_sub_folder_is_sized_by_what_cone_mode_checks_out_and_never_by_git(
    tmp_path, recording_git, monkeypatch
):
    monkeypatch.setenv("TREE_SHA", "1" * 40)
    monkeypatch.setenv("FAIL_ON", "nothing")
    fake = FakeGitHub()
    serve_api(fake, tree_json(SPLIT_REPO))
    clone_repo(REF, tmp_path, CONFIG, public(fake), CloneRequest("public", "packages/web", None))
    calls = [argv for argv, _ in recording_git()]
    assert not any("ls-tree" in argv for argv in calls)
    assert {"--filter=blob:none", "--sparse"} <= set(calls[0])
    assert calls[-2][-5:] == ["sparse-checkout", "set", "--cone", "--", "packages/web"]


def test_a_sub_folder_too_big_for_the_limit_is_refused_before_anything_is_downloaded(
    tmp_path, recording_git
):
    fake = FakeGitHub()
    serve_api(fake, tree_json(SPLIT_REPO | {"setup.cfg": b"x" * 200_000}))
    with pytest.raises(CloneFailed, match="files were not checked out"):
        clone_repo(
            REF, tmp_path, CONFIG, public(fake), CloneRequest("public", "packages/web", None)
        )
    assert recording_git() == []


@pytest.mark.parametrize(
    ("tree", "says"),
    [(tree_json(SPLIT_REPO), "has no folder"), (tree_json(SPLIT_REPO, True), "too many files")],
    ids=["missing folder", "cut-short list"],
)
def test_a_sub_folder_that_cannot_be_checked_is_refused_before_cloning(
    tmp_path, recording_git, tree, says
):
    fake = FakeGitHub()
    serve_api(fake, tree)
    with pytest.raises(CloneFailed, match=says):
        clone_repo(
            REF, tmp_path, CONFIG, public(fake), CloneRequest("public", "apps/missing", None)
        )
    assert recording_git() == []


def test_a_sub_folder_needs_githubs_list_so_github_being_down_stops_it(tmp_path, recording_git):
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, httpx.ConnectError("offline"))
    with pytest.raises(CloneFailed, match="Could not reach GitHub"):
        clone_repo(
            REF, tmp_path, CONFIG, public(fake), CloneRequest("public", "packages/web", None)
        )
    assert recording_git() == []


@pytest.mark.parametrize(
    "subdir", ["../x", "/abs", "a//b", "a/./b", "a\\b", "C:/x", "a b", "x" * 201, "a/"]
)
def test_a_sub_folder_that_is_not_a_plain_folder_path_is_refused(subdir):
    with pytest.raises(PathBlocked, match="sub-folder"):
        CloneRequest("public", subdir, None)


@pytest.mark.parametrize("subdir", ["packages/web", "a.b_c-d", "x" * 200])
def test_plain_folder_paths_are_accepted(subdir):
    assert CloneRequest("github", subdir, USER.id).subdir == subdir


def test_a_push_between_githubs_list_and_the_clone_sizes_the_commit_that_was_cloned(
    tmp_path, remote
):
    source, fake = remote
    head = head_commit(source, CONFIG)
    fake.on("GET", TREE_URL, ok(tree_json({"README.md": b"older"})))
    fake.on("GET", f"{REPO_URL}/git/trees/{head}?recursive=1", ok(tree_of(source)))
    assert (clone_repo(REF, tmp_path, CONFIG, public(fake), PUBLIC) / "README.md").exists()
    assert f"{REPO_URL}/git/trees/{head}?recursive=1" in fake.urls()


def test_a_clone_that_does_not_match_githubs_list_for_its_commit_is_not_checked_out(
    tmp_path, remote
):
    source, fake = remote
    head = head_commit(source, CONFIG)
    fake.on("GET", TREE_URL, ok(tree_json({})))
    fake.on("GET", f"{REPO_URL}/git/trees/{head}?recursive=1", ok(tree_json({})))
    with pytest.raises(CloneFailed, match="do not match"):
        clone_repo(REF, tmp_path, CONFIG, public(fake), PUBLIC)
    assert not (tmp_path / "r" / "README.md").exists()


def test_a_signed_in_clone_with_sign_in_off_says_how_to_turn_it_on(tmp_path):
    with pytest.raises(CloneFailed, match=SIGN_IN_OFF[0]):
        clone_repo(REF, tmp_path, CONFIG, public(FakeGitHub()), SIGNED_IN)


def test_a_signed_in_clone_after_the_sign_in_ended_says_to_sign_in(tmp_path, recording_git):
    fake = FakeGitHub()
    serve_api(fake, tree_json({"README.md": b"x"}))
    vault_without_sign_in = github_access(fake, FakeClock(), MemoryKeyring(), token=None)
    with pytest.raises(CloneFailed, match="not signed in"):
        clone_repo(REF, tmp_path, CONFIG, vault_without_sign_in, SIGNED_IN)
    assert recording_git() == []


def test_clone_is_shallow_https_only_and_without_saved_passwords(tmp_path, monkeypatch):
    fake_executable(tmp_path / "bin", "git", ECHO_ARGS_AND_FAIL)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(CloneFailed) as error:
        git_clone("https://github.com/o/r.git", tmp_path / "out", CONFIG, None, None)
    sent = error.value.details
    for part in ("--depth 1", "--no-checkout", "protocol.allow=never", "credential.helper="):
        assert part in sent
    assert "--filter" not in sent and "-- https://github.com/o/r.git" in sent


def test_clone_keeps_line_endings_and_allows_long_paths(tmp_path, monkeypatch):
    fake_executable(tmp_path / "bin", "git", ECHO_ARGS_AND_FAIL)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(CloneFailed) as error:
        git_clone("https://github.com/o/r.git", tmp_path / "out", CONFIG, None, None)
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
        git_clone("https://github.com/o/r.git", tmp_path / "out", CONFIG, None, None)
    assert "exited with 128: fatal: repository not found" in error.value.details


def test_failed_checkout_does_not_blame_the_url(tmp_path, recording_git, monkeypatch):
    monkeypatch.setenv("TREE_SHA", "1" * 40)
    monkeypatch.setenv("FAIL_ON", "checkout")
    fake = FakeGitHub()
    serve_api(fake, tree_json({"README.md": b"x"}))
    with pytest.raises(CloneFailed, match="^git checkout failed. Check that the disk"):
        clone_repo(REF, tmp_path, CONFIG, public(fake), PUBLIC)


def test_empty_repo_is_called_empty_not_a_bad_url(tmp_path, monkeypatch, local_git):
    empty = tmp_path / "empty"
    subprocess.run(["git", "init", "-q", str(empty)], check=True)
    clone_from(monkeypatch, empty.as_uri())
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, ok(repo_json()))
    fake.on("GET", TREE_URL, httpx.Response(409, json={"message": "Git Repository is empty."}))
    with pytest.raises(CloneFailed, match="may be empty"):
        clone_repo(REF, tmp_path, CONFIG, public(fake), PUBLIC)


def test_hanging_clone_is_stopped(tmp_path, monkeypatch):
    fake_executable(tmp_path / "bin", "git", "import time; time.sleep(5)")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    config = RepoConfig(max_size_mb=1, clone_timeout_seconds=0.5, max_tree_chars=2000)
    with pytest.raises(CloneFailed, match="longer than"):
        git_clone("https://github.com/o/r.git", tmp_path / "out", config, None, None)


def test_missing_git_is_a_prerequisite_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(PrereqMissing, match="git"):
        git_clone("https://github.com/o/r.git", tmp_path / "out", CONFIG, None, None)


def test_clone_does_not_ask_for_a_password(tmp_path, monkeypatch):
    code = "import os, sys; print('prompt=' + os.environ['GIT_TERMINAL_PROMPT'], file=sys.stderr)"
    code += "; sys.exit(1)"
    fake_executable(tmp_path / "bin", "git", code)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(CloneFailed) as error:
        git_clone("https://github.com/o/r.git", tmp_path / "out", CONFIG, None, None)
    assert "prompt=0" in error.value.details


@pytest.mark.parametrize(
    ("access", "owner"), [("github", None), ("public", USER.id)]
)  # fmt: skip
def test_only_a_signed_in_request_names_its_github_user(access, owner):
    with pytest.raises(CloneFailed, match="must name its GitHub user"):
        CloneRequest(access, None, owner)


def test_a_run_started_by_another_github_user_never_gets_this_users_token(tmp_path, recording_git):
    fake = FakeGitHub()
    serve_api(fake, tree_json({"README.md": b"x"}), is_private=True)
    someone_else = CloneRequest("github", None, USER.id + 1)
    with pytest.raises(CloneFailed, match="sign-in changed"):
        clone_repo(REF, tmp_path, CONFIG, signed_in(fake), someone_else)
    assert recording_git() == []
