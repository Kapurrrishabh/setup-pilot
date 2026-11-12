import logging
from datetime import UTC, datetime

import httpcore
import httpx
import pytest
from pydantic import SecretStr

from setuppilot.errors import PathBlocked
from setuppilot.github.api import (
    FixedToken,
    GitHubAuthEnded,
    GitHubClient,
    GitHubError,
    GitHubNotFound,
    GitHubRateLimited,
)
from setuppilot.redact import redact
from tests.fakes import FakeClock
from tests.github_fakes import (
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
    targz,
    tree_json,
)

INSTALLS_URL = "https://api.github.com/user/installations?per_page=100"
REPOS_URL = "https://api.github.com/user/installations/7/repositories?per_page=100"
PAGE_2 = "https://api.github.com/user/installations/7/repositories?per_page=100&page=2"


def repo_page(*names: str, next_url: str | None = None) -> httpx.Response:
    repos = [repo_json() | {"full_name": f"o/{name}"} for name in names]
    links = {"link": f'<{next_url}>; rel="next"'} if next_url else {}
    return ok({"total_count": 3, "repositories": repos}, **links)


def test_every_call_names_the_api_version():
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, ok(repo_json()))
    client_for(fake).get_repo(REF)
    headers = fake.requests[0].headers
    assert (headers["x-github-api-version"], headers["accept"], headers["user-agent"]) == (
        "2022-11-28",
        "application/vnd.github+json",
        "setuppilot",
    )


def test_public_reads_send_no_token():
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, ok(repo_json()))
    client_for(fake).get_repo(REF)
    assert "authorization" not in fake.requests[0].headers


def test_signed_in_reads_send_the_token():
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, ok(repo_json()))
    client_for(fake, FakeTokens("ghu_abc")).get_repo(REF)
    assert fake.requests[0].headers["authorization"] == "Bearer ghu_abc"


def test_repo_answer_says_if_it_is_private():
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, ok(repo_json(is_private=True)))
    repo = client_for(fake).get_repo(REF)
    assert (repo.is_private, repo.default_branch) == (True, "main")


def test_an_answer_of_the_wrong_shape_is_a_clear_error():
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, ok({"full_name": "o/r"}))
    with pytest.raises(GitHubError, match="not what SetupPilot expected") as raised:
        client_for(fake).get_repo(REF)
    assert "default_branch" in raised.value.details


def test_a_404_is_not_found():
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(GitHubNotFound):
        client_for(fake).get_repo(REF)


def test_no_connection_is_a_clear_error():
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, httpx.ConnectError("offline"))
    with pytest.raises(GitHubError, match="Could not reach GitHub"):
        client_for(fake).get_repo(REF)


def test_tree_lists_files_and_links_but_not_folders_or_submodules():
    fake = FakeGitHub()
    items = [
        {"path": "src", "mode": "040000", "type": "tree", "sha": "a"},
        {"path": "src/app.py", "mode": "100644", "type": "blob", "sha": "b", "size": 12},
        {"path": "latest", "mode": "120000", "type": "blob", "sha": "c", "size": 3},
        {"path": "vendor/lib", "mode": "160000", "type": "commit", "sha": "d"},
    ]
    fake.on("GET", TREE_URL, ok({"sha": "t", "truncated": False, "tree": items}))
    tree = client_for(fake).get_tree(REF, "main")
    assert [(e.path, e.size, e.is_link) for e in tree.entries] == [
        ("src/app.py", 12, False),
        ("latest", 3, True),
    ]


@pytest.mark.parametrize("path", ["../evil", "a/../../evil", "/etc/passwd", "a\\b", "C:x", "a//b"])
def test_tree_path_that_could_leave_the_repo_folder_is_refused(path):
    fake = FakeGitHub()
    fake.on("GET", TREE_URL, ok(tree_json({path: b"x"})))
    with pytest.raises(PathBlocked):
        client_for(fake).get_tree(REF, "main")


def test_pages_are_followed_through_the_link_header():
    fake = FakeGitHub()
    fake.on("GET", REPOS_URL, repo_page("a", "b", next_url=PAGE_2))
    fake.on("GET", PAGE_2, repo_page("c"))
    listing = client_for(fake, FakeTokens()).installation_repos(7, max_pages=10)
    assert ([r.full_name for r in listing.items], listing.is_complete) == (
        ["o/a", "o/b", "o/c"],
        True,
    )


def test_paging_stops_at_the_page_limit_and_says_the_list_is_cut_short():
    fake = FakeGitHub()
    fake.on("GET", REPOS_URL, repo_page("a", "b", next_url=PAGE_2))
    listing = client_for(fake, FakeTokens()).installation_repos(7, max_pages=1)
    assert (len(listing.items), listing.is_complete, len(fake.requests)) == (2, False, 1)


def test_a_next_page_on_another_site_is_not_followed():
    fake = FakeGitHub()
    fake.on("GET", REPOS_URL, repo_page("a", next_url="https://evil.example/steal"))
    with pytest.raises(GitHubError, match="another site"):
        client_for(fake, FakeTokens()).installation_repos(7, max_pages=10)


def test_installations_are_read():
    fake = FakeGitHub()
    install = {"id": 7, "account": {"login": "octocat"}, "repository_selection": "selected"}
    fake.on("GET", INSTALLS_URL, ok({"total_count": 1, "installations": [install]}))
    listing = client_for(fake, FakeTokens()).installations(max_pages=10)
    assert [(i.id, i.account.login) for i in listing.items] == [(7, "octocat")]


def test_used_up_limit_raises_with_the_reset_time_and_does_not_retry():
    fake = FakeGitHub()
    used_up = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800003600"}
    limited = httpx.Response(403, json={"message": "API rate limit"}, headers=used_up)
    fake.on("GET", REPO_URL, limited)
    with pytest.raises(GitHubRateLimited) as raised:
        client_for(fake).get_repo(REF)
    assert (raised.value.reset_at, len(fake.requests)) == (
        datetime.fromtimestamp(1800003600, UTC),
        1,
    )


def test_retry_after_is_waited_out_before_trying_again():
    fake = FakeGitHub()
    busy = httpx.Response(429, headers={"retry-after": "7"})
    fake.on("GET", REPO_URL, busy, ok(repo_json()))
    clock = FakeClock()
    client_for(fake, clock=clock).get_repo(REF)
    assert clock.sleeps == [7]


def test_a_long_retry_after_is_reported_at_once_instead_of_waited_out():
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, httpx.Response(403, headers={"retry-after": "3600"}))
    clock = FakeClock()
    with pytest.raises(GitHubRateLimited):
        client_for(fake, clock=clock).get_repo(REF)
    assert (clock.sleeps, len(fake.requests)) == ([], 1)


def test_secondary_limit_backs_off_then_gives_up():
    fake = FakeGitHub()
    limited = {"message": "You have exceeded a secondary rate limit."}
    fake.on("GET", REPO_URL, httpx.Response(403, json=limited))
    clock = FakeClock()
    with pytest.raises(GitHubRateLimited):
        client_for(fake, clock=clock).get_repo(REF)
    assert clock.sleeps == [60, 120]


def test_a_plain_403_is_refused_without_waiting():
    fake = FakeGitHub()
    no_access = {"message": "Resource not accessible by integration"}
    fake.on("GET", REPO_URL, httpx.Response(403, json=no_access))
    clock = FakeClock()
    with pytest.raises(GitHubError, match="HTTP 403") as raised:
        client_for(fake, clock=clock).get_repo(REF)
    assert (type(raised.value), clock.sleeps) == (GitHubError, [])


def test_an_unchanged_answer_comes_from_the_cache():
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, ok(repo_json(), etag='W/"v1"'), httpx.Response(304))
    client = client_for(fake, FakeTokens())
    first, second = client.get_repo(REF), client.get_repo(REF)
    assert (second, fake.requests[1].headers["if-none-match"]) == (first, 'W/"v1"')


def test_the_cache_is_not_shared_between_sign_ins():
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, ok(repo_json(), etag='W/"v1"'))
    tokens = FakeTokens("ghu_first")
    client = client_for(fake, tokens)
    client.get_repo(REF)
    tokens.token = "ghu_second"  # noqa: S105 - a fake token
    client.get_repo(REF)
    assert "if-none-match" not in fake.requests[1].headers


def test_a_refused_token_ends_the_sign_in():
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, httpx.Response(401, json={"message": "Bad credentials"}))
    tokens = FakeTokens("ghu_revoked")
    with pytest.raises(GitHubAuthEnded):
        client_for(fake, tokens).get_repo(REF)
    assert tokens.ended == ["ghu_revoked"]


def test_who_signed_in_is_asked_with_the_new_token():
    fake = FakeGitHub()
    fake.on("GET", "https://api.github.com/user", ok({"login": "octocat", "id": 1, "name": "M"}))
    user = client_for(fake, FixedToken(SecretStr("ghu_new"))).user()
    assert (user.login, fake.requests[0].headers["authorization"]) == ("octocat", "Bearer ghu_new")


def serve_download(fake: FakeGitHub, location: str = DOWNLOAD_URL) -> None:
    fake.on("GET", TARBALL_URL, httpx.Response(302, headers={"location": location}))
    fake.on("GET", DOWNLOAD_URL, httpx.Response(200, content=targz({"README.md": b"hi"})))


def test_the_download_is_fetched_without_the_token():
    fake = FakeGitHub()
    serve_download(fake)
    with client_for(fake, FakeTokens()).open_tarball(REF, "main") as stream:
        stream.read()
    assert [("authorization" in r.headers) for r in fake.requests] == [True, False]


def raw_reply(status: str, headers: dict[str, str], body: bytes = b"") -> bytes:
    head = [f"HTTP/1.1 {status}", f"Content-Length: {len(body)}"]
    head += [f"{name}: {value}" for name, value in headers.items()]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


class HostBackend(httpcore.NetworkBackend):
    """httpcore's network layer with canned bytes for each host, so no real network is used."""

    def __init__(self, replies: dict[str, bytes]) -> None:
        self.replies = replies

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return httpcore.MockStream([self.replies[host]])


def test_the_download_link_is_never_logged(caplog):
    replies = {
        "api.github.com": raw_reply("302 Found", {"Location": DOWNLOAD_URL}),
        "codeload.github.com": raw_reply("200 OK", {}, targz({"README.md": b"hi"})),
    }
    transport = httpx.HTTPTransport()
    # MockTransport would skip httpcore, whose DEBUG logs print whole replies; httpx has no
    # public way to give its transport another network layer.
    transport._pool = httpcore.ConnectionPool(network_backend=HostBackend(replies))
    clock = FakeClock()
    client = GitHubClient(httpx.Client(transport=transport), FakeTokens(), clock, clock.sleep)
    caplog.set_level(logging.DEBUG)
    with client.open_tarball(REF, "main") as stream:
        stream.read()
    assert "codeload" not in caplog.text


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/o/r.tar.gz",
        "http://codeload.github.com/o/r.tar.gz",
        "https://user:pw@codeload.github.com/o/r.tar.gz",
    ],
)
def test_a_download_link_somewhere_else_is_not_followed(location):
    fake = FakeGitHub()
    serve_download(fake, location)
    with (
        pytest.raises(GitHubError, match="download link"),
        client_for(fake).open_tarball(REF, "main"),
    ):
        pass
    assert len(fake.requests) == 1


def test_a_download_key_is_hidden_if_it_ever_reaches_a_log():
    assert "AABBCCDDEEFFGG112233" not in redact(f"failed: {DOWNLOAD_URL}")
