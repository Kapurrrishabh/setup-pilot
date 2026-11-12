"""Fakes for GitHub tests: a scripted GitHub for httpx.MockTransport, archives, a keyring, a
sign-in, and a real git server on 127.0.0.1. No test reaches the real GitHub."""

import io
import os
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from setuppilot.config import GitHubApp
from setuppilot.github.api import GitHubClient, GitHubUser, TokenSource
from setuppilot.github.device_flow import TokenGrant, parse_grant
from setuppilot.github.vault import GitHubCredentials, TokenVault, binding_hash
from setuppilot.tools.clone import GitHubAccess, SignedIn
from setuppilot.tools.github_url import RepoRef
from tests.fakes import FakeClock, make_repo

REF = RepoRef("o", "r")
REPO_URL = "https://api.github.com/repos/o/r"
TREE_URL = f"{REPO_URL}/git/trees/main?recursive=1"
TARBALL_URL = f"{REPO_URL}/tarball/main"
DOWNLOAD_URL = (
    "https://codeload.github.com/o/r/legacy.tar.gz/refs/heads/main?token=AABBCCDDEEFFGG112233"
)
COMMIT = "621e4974ca25ce531773def586ba3ed8e736b3fc"
APP = GitHubApp(client_id="Iv23liTest", app_slug="setuppilot-test")
USER = GitHubUser(login="octocat", id=583231)
EIGHT_HOURS = 28800
SIX_MONTHS = 15897600


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


def grant(token: str, refresh: str, now: float, lives: int = EIGHT_HOURS) -> TokenGrant:
    reply = {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": lives,
        "refresh_token": refresh,
        "refresh_token_expires_in": SIX_MONTHS,
    }
    return parse_grant(reply, now)


def credentials(grant: TokenGrant, key: str = "browser-key") -> GitHubCredentials:
    return GitHubCredentials(grant=grant, user=USER, binding_hash=binding_hash(key))


def github_access(
    fake: FakeGitHub, clock: FakeClock, keyring: MemoryKeyring, token: str | None
) -> GitHubAccess:
    """Reads through the fake GitHub with sign-in on. With a token, octocat is signed in from the
    browser that holds "browser-key"; with None, nobody is signed in yet."""
    vault = TokenVault.load(keyring, lambda refresh, now: grant("ghu_new", "ghr_new", now), clock)
    if token is not None:
        vault.save(credentials(grant(token, "ghr_testtoken", clock())))
    http = fake.http()
    signed_in = SignedIn(APP, http, clock, GitHubClient(http, vault, clock, clock.sleep), vault)
    return GitHubAccess(GitHubClient(http, None, clock, clock.sleep), signed_in)


def commit_all(repo: Path) -> None:
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "files"], check=True)


def git_repo(folder: Path, files: dict[str, str]) -> Path:
    """A real git repo with one commit holding files."""
    make_repo(folder, files)
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    commit_all(folder)
    return folder


def tree_of(repo: Path) -> dict:
    """GitHub's recursive tree answer for the repo's HEAD, read from git itself. Like GitHub's,
    its sha is the commit's (measured), not the tree's."""

    def git(*args: str) -> str:
        command = ["git", "-C", str(repo), *args]
        return subprocess.run(command, capture_output=True, encoding="utf-8", check=True).stdout

    items = []
    for line in git("ls-tree", "-r", "-l", "HEAD").splitlines():
        mode, kind, sha, size_and_path = line.split(" ", 3)
        size, path = size_and_path.lstrip().split("\t", 1)
        items.append({"path": path, "mode": mode, "type": kind, "sha": sha, "size": int(size)})
    return {"sha": git("rev-parse", "HEAD").strip(), "truncated": False, "tree": items}


class GitServer:
    """Real git over smart HTTP on 127.0.0.1 (git http-backend), serving the bare repos under
    root. Each request's Authorization header is recorded; with required_auth set, a request
    without that exact header is refused."""

    def __init__(self, root: Path, required_auth: str | None) -> None:
        self.root = root
        self.seen_auth: list[str | None] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server.answer(self)

            def do_POST(self) -> None:
                server.answer(self)

            def log_message(self, *args: object) -> None:
                """Quiet: tests read seen_auth instead."""

        self._required = required_auth
        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._http.server_port}"
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    def add_repo(self, source: Path, key: str) -> None:
        """Serve a copy of source as key, with partial clones allowed."""
        bare = self.root / f"{key}.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(source), str(bare)], check=True)
        for setting in ("uploadpack.allowFilter", "uploadpack.allowAnySHA1InWant"):
            subprocess.run(["git", "-C", str(bare), "config", setting, "true"], check=True)

    def answer(self, request: BaseHTTPRequestHandler) -> None:
        auth = request.headers.get("Authorization")
        self.seen_auth.append(auth)
        if self._required is not None and auth != self._required:
            request.send_response(401)
            request.send_header("WWW-Authenticate", 'Basic realm="test"')
            request.end_headers()
            return
        body = _request_body(request)
        path, _, query = request.path.partition("?")
        env = os.environ | {
            "GIT_PROJECT_ROOT": str(self.root), "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path,
            "QUERY_STRING": query, "REQUEST_METHOD": request.command, "REMOTE_ADDR": "127.0.0.1",
            "CONTENT_TYPE": request.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "GIT_PROTOCOL": request.headers.get("Git-Protocol", ""),
            "HTTP_CONTENT_ENCODING": request.headers.get("Content-Encoding", ""),
        }  # fmt: skip
        done = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True,
                              check=False)  # fmt: skip
        head, _, content = done.stdout.replace(b"\r\n", b"\n").partition(b"\n\n")
        headers = [line.split(": ", 1) for line in head.decode().splitlines()]
        status = next((int(v.split()[0]) for k, v in headers if k.lower() == "status"), 200)
        request.send_response(status)
        for name, value in headers:
            if name.lower() != "status":
                request.send_header(name, value)
        request.send_header("Content-Length", str(len(content)))
        request.end_headers()
        request.wfile.write(content)

    def close(self) -> None:
        self._http.shutdown()
        self._http.server_close()


def _request_body(request: BaseHTTPRequestHandler) -> bytes:
    if request.headers.get("Transfer-Encoding") != "chunked":
        return request.rfile.read(int(request.headers.get("Content-Length") or 0))
    body = b""
    while size := int(request.rfile.readline().split(b";")[0], 16):
        body += request.rfile.read(size)
        request.rfile.readline()
    request.rfile.readline()
    return body
