"""Fakes for GitHub tests: a scripted GitHub for httpx.MockTransport, archives and a keyring.
No test reaches the real GitHub."""

import io
import tarfile

import httpx
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from setuppilot.github.api import GitHubClient, TokenSource
from setuppilot.tools.github_url import RepoRef
from tests.fakes import FakeClock

REF = RepoRef("o", "r")
REPO_URL = "https://api.github.com/repos/o/r"
TREE_URL = f"{REPO_URL}/git/trees/main?recursive=1"
TARBALL_URL = f"{REPO_URL}/tarball/main"
DOWNLOAD_URL = (
    "https://codeload.github.com/o/r/legacy.tar.gz/refs/heads/main?token=AABBCCDDEEFFGG112233"
)
COMMIT = "621e4974ca25ce531773def586ba3ed8e736b3fc"


class FakeGitHub:
    """Answers each request by its method and full URL, and records every request. Replies for one
    URL are used in order, and the last one repeats; an exception reply is raised."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[httpx.Response | Exception]] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method: str, url: str, *replies: httpx.Response | Exception) -> None:
        self.routes[(method, url)] = list(replies)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        replies = self.routes.get((request.method, str(request.url)))
        if not replies:
            raise AssertionError(f"No fake reply for {request.method} {request.url}")
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def http(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]


class FakeTokens:
    """A sign-in whose token a test can change, and which records refused tokens."""

    def __init__(self, token: str = "ghu_testtoken") -> None:  # noqa: S107 - a fake token
        self.token = token
        self.ended: list[str] = []

    def access_token(self) -> str:
        return self.token

    def sign_in_ended(self, access_token: str) -> None:
        self.ended.append(access_token)


def client_for(
    fake: FakeGitHub, tokens: TokenSource | None = None, clock: FakeClock | None = None
) -> GitHubClient:
    clock = clock or FakeClock()
    return GitHubClient(fake.http(), tokens, clock, clock.sleep)


def ok(body: object, **headers: str) -> httpx.Response:
    return httpx.Response(200, json=body, headers=headers)


def repo_json(is_private: bool = False) -> dict:
    return {
        "full_name": "o/r",
        "private": is_private,
        "visibility": "private" if is_private else "public",
        "default_branch": "main",
        "description": "A demo",
        "language": "Python",
        "archived": False,
        "fork": False,
        "pushed_at": "2024-11-06T22:36:40Z",
    }


def tree_json(files: dict[str, bytes], truncated: bool = False) -> dict:
    items = [
        {"path": path, "mode": "100644", "type": "blob", "sha": "0" * 40, "size": len(data)}
        for path, data in files.items()
    ]
    return {"sha": "1" * 40, "truncated": truncated, "tree": items}


def targz(
    files: dict[str, bytes],
    links: dict[str, str] | None = None,
    outside: dict[str, bytes] | None = None,
    commit: str = COMMIT,
) -> bytes:
    """A .tar.gz shaped like GitHub's: one top folder, and the commit in a pax header.
    outside holds members given by their full name, to try escaping the top folder."""
    top = "o-r-621e497"
    buffer = io.BytesIO()
    pax = {"comment": commit} if commit else {}
    with tarfile.open(
        fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers=pax
    ) as archive:
        # outside goes first, so it is read before the top folder is known.
        for name, data in (outside or {}).items():
            add_file(archive, name, data)
        folder = tarfile.TarInfo(top)
        folder.type = tarfile.DIRTYPE
        archive.addfile(folder)
        for name, data in files.items():
            add_file(archive, f"{top}/{name}", data)
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return buffer.getvalue()


def add_file(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def serve_repo(
    fake: FakeGitHub,
    archive: bytes,
    tree: dict,
    is_private: bool = False,
) -> None:
    fake.on("GET", REPO_URL, ok(repo_json(is_private)))
    fake.on("GET", TREE_URL, ok(tree))
    fake.on("GET", TARBALL_URL, httpx.Response(302, headers={"location": DOWNLOAD_URL}))
    fake.on("GET", DOWNLOAD_URL, httpx.Response(200, content=archive))


class MemoryKeyring(KeyringBackend):
    """A keyring in a dict. Its priority is below keyring's "none found", so keyring never picks
    it on its own; tests hand it to the vault directly."""

    priority = -1

    def __init__(self, get_error: Exception | None = None) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}
        self.get_error = get_error
        self.set_error: Exception | None = None
        self.delete_error: Exception | None = None

    def get_password(self, service: str, username: str) -> str | None:
        if self.get_error is not None:
            raise self.get_error
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.set_error is not None:
            raise self.set_error
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        if self.items.pop((service, username), None) is None:
            raise PasswordDeleteError("Password not found")
