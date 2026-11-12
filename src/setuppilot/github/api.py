"""The GitHub REST API: one request at a time, answers reused by ETag, and no secret ever logged."""

import hashlib
import io
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from setuppilot.errors import PathBlocked, SetupPilotError
from setuppilot.tools.github_url import RepoRef

if TYPE_CHECKING:
    from _typeshed import WriteableBuffer

log = logging.getLogger(__name__)

API_URL = "https://api.github.com"
USER_AGENT = "setuppilot"
# Naming the version means a GitHub change cannot quietly change the shape of the answers.
API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": USER_AGENT,
}
_DOWNLOAD_HOST = "codeload.github.com"
# PROVISIONAL, from GitHub's advice for its secondary limits: wait at least a minute, twice as
# long each time, and give up after 2 more tries.
_FIRST_WAIT_SECONDS = 60
_MAX_RETRIES = 2
# PROVISIONAL: never wait longer than the backoff's last step. A longer retry-after is reported
# with its time instead, so a page does not hang on GitHub for an hour.
_MAX_WAIT_SECONDS = _FIRST_WAIT_SECONDS * 2 ** (_MAX_RETRIES - 1)
# PROVISIONAL: the biggest answer is a 7 MB tree, so this keeps a few of those and many small ones.
_CACHE_MAX_BYTES = 32 * 2**20
_MESSAGE_CHARS = 300


def _is_not_a_download(record: logging.LogRecord) -> bool:
    args = record.args if isinstance(record.args, tuple) else ()
    return not any(isinstance(arg, httpx.URL) and arg.host == _DOWNLOAD_HOST for arg in args)


# httpx logs every URL it fetches, and a private repo's download link holds a short-lived key.
# httpcore's DEBUG logs print whole replies, the link too. redact() in the log file is a backstop.
logging.getLogger("httpx").addFilter(_is_not_a_download)
logging.getLogger("httpcore").setLevel(logging.INFO)


class GitHubError(SetupPilotError):
    """GitHub could not be reached, or did not give the answer asked for."""


class GitHubNotFound(GitHubError):
    """GitHub answered 404: missing, private, or not shared with the GitHub App."""


class GitHubAuthEnded(GitHubError):
    """The GitHub sign-in is gone (signed out, run out or cancelled on GitHub)."""


class GitHubRateLimited(GitHubError):
    """GitHub's read limit is used up until reset_at."""

    def __init__(self, reset_at: datetime, is_signed_in: bool) -> None:
        how = (
            "Your GitHub account allows 5,000 reads an hour, shared with your other apps."
            if is_signed_in
            else "Without sign-in GitHub allows 60 reads an hour from your network; sign in "
            "with GitHub for 5,000."
        )
        super().__init__(
            f"GitHub's read limit is used up until {reset_at.astimezone():%H:%M}.",
            details=f"{how} Try again after that time.",
        )
        self.reset_at = reset_at


class TokenSource(Protocol):
    def access_token(self) -> str: ...

    def sign_in_ended(self, access_token: str) -> None: ...


@dataclass(frozen=True)
class FixedToken:
    """The token of a sign-in that has just finished, used to ask GitHub who signed in."""

    token: SecretStr

    def access_token(self) -> str:
        return self.token.get_secret_value()

    def sign_in_ended(self, access_token: str) -> None:
        """Nothing is saved yet, so there is nothing to forget."""


class GitHubReply(BaseModel):
    # GitHub adds fields over time; only the ones read here are checked.
    model_config = ConfigDict(extra="ignore", frozen=True)


class GitHubUser(GitHubReply):
    login: str
    id: int


class RepoInfo(GitHubReply):
    full_name: str
    is_private: bool = Field(alias="private")
    visibility: Literal["public", "private", "internal"]
    default_branch: str
    description: str | None
    language: str | None
    is_archived: bool = Field(alias="archived")
    is_fork: bool = Field(alias="fork")
    pushed_at: datetime | None


class Account(GitHubReply):
    login: str


class Installation(GitHubReply):
    id: int
    account: Account
    repository_selection: Literal["all", "selected"]


class _InstallationPage(GitHubReply):
    installations: list[Installation]


class _RepoPage(GitHubReply):
    repositories: list[RepoInfo]


class _TreeItem(GitHubReply):
    path: str
    mode: str
    type: str
    size: int | None = None

    @model_validator(mode="after")
    def _files_have_a_size(self) -> "_TreeItem":
        if self.type == "blob" and self.size is None:
            raise ValueError(f"{self.path!r} has no size")
        return self


class _TreeReply(GitHubReply):
    sha: str
    tree: list[_TreeItem]
    truncated: bool


@dataclass(frozen=True)
class TreeEntry:
    """A file or a link in a repo. Folders are not listed, as in scan_repo."""

    path: str
    size: int
    is_link: bool


@dataclass(frozen=True)
class Tree:
    sha: str
    entries: tuple[TreeEntry, ...]
    # GitHub stops at 100,000 entries or 7 MB; then the list and its sizes are incomplete.
    is_truncated: bool

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries if not entry.is_link)


@dataclass(frozen=True)
class Listing[T]:
    items: tuple[T, ...]
    # False when the page limit stopped the paging before the end.
    is_complete: bool


def check_repo_path(path: str) -> str:
    """A file path from GitHub, refused if it could point outside the repo folder on any OS."""
    if (
        "\\" in path
        or "\x00" in path
        or PureWindowsPath(path).drive
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise PathBlocked(
            "This repo has a file path that could point outside its folder, so it was not read.",
            details=f"Refused path: {path[:200]!r}.",
        )
    return path


def unreachable(error: httpx.HTTPError) -> GitHubError:
    return GitHubError(
        "Could not reach GitHub.",
        details=f"Check your internet connection. {type(error).__name__}: {error}",
    )


class GitHubClient:
    """Calls go one at a time, as GitHub asks, and an answer is reused while its ETag holds.
    tokens is None for public reads, which never send a token."""

    def __init__(
        self,
        http: httpx.Client,
        tokens: TokenSource | None,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._http = http
        self._tokens = tokens
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._cache = _EtagCache(_CACHE_MAX_BYTES)

    def user(self) -> GitHubUser:
        return parse_reply(GitHubUser, self._get_json(f"{API_URL}/user")[0])

    def get_repo(self, ref: RepoRef) -> RepoInfo:
        return parse_reply(RepoInfo, self._get_json(_repo_url(ref))[0])

    def get_tree(self, ref: RepoRef, tree_ish: str) -> Tree:
        url = f"{_repo_url(ref)}/git/trees/{quote(tree_ish, safe='')}?recursive=1"
        reply = parse_reply(_TreeReply, self._get_json(url)[0])
        entries = tuple(
            TreeEntry(check_repo_path(item.path), item.size or 0, is_link=item.mode == "120000")
            for item in reply.tree
            if item.type == "blob"
        )
        return Tree(reply.sha, entries, reply.truncated)

    def installations(self, max_pages: int) -> Listing[Installation]:
        """Where the user installed this GitHub App. GitHub lists only this app's installations."""
        return self._pages(
            f"{API_URL}/user/installations?per_page=100",
            lambda data: parse_reply(_InstallationPage, data).installations,
            max_pages,
        )

    def installation_repos(self, installation_id: int, max_pages: int) -> Listing[RepoInfo]:
        return self._pages(
            f"{API_URL}/user/installations/{installation_id}/repositories?per_page=100",
            lambda data: parse_reply(_RepoPage, data).repositories,
            max_pages,
        )

    @contextmanager
    def open_tarball(self, ref: RepoRef, git_ref: str) -> Iterator[io.RawIOBase]:
        """The repo's .tar.gz at git_ref, read as it arrives. GitHub answers with a link to its
        download server; for a private repo the link holds a short-lived key, so it is never logged.
        """
        with self._lock:
            token = self._token()
            url = f"{_repo_url(ref)}/tarball/{quote(git_ref, safe='')}"
            response = self._send(url, token, {}, follow_redirects=False)
        self._check(response, token)
        location = httpx.URL(response.headers.get("location", ""))
        if (
            response.status_code != 302
            or location.scheme != "https"
            or location.host != _DOWNLOAD_HOST
            or location.userinfo
        ):
            raise GitHubError(
                "GitHub did not give a download link SetupPilot trusts, so nothing was downloaded.",
                details=f"Expected a redirect to https://{_DOWNLOAD_HOST}/, got HTTP "
                f"{response.status_code}.",
            )
        try:
            # No Authorization header: the link itself carries the permission.
            with self._http.stream("GET", location, headers={"User-Agent": USER_AGENT}) as download:
                log.debug("github download", extra={"status": download.status_code})
                if download.status_code != 200:
                    raise GitHubError(
                        f"GitHub's download failed (HTTP {download.status_code}).",
                        details="Try again in a few minutes.",
                    )
                yield _ChunkReader(download.iter_bytes())
        except httpx.HTTPError as error:
            # Only the error's type: its text could hold the download link.
            raise GitHubError(
                "The download from GitHub broke off.",
                details=f"Check your internet connection ({type(error).__name__}).",
            ) from error

    def _pages[T](self, url: str, read: Callable[[Any], list[T]], max_pages: int) -> Listing[T]:
        items: list[T] = []
        next_url: str | None = url
        for _ in range(max_pages):
            if next_url is None:
                break
            data, next_url = self._get_json(next_url)
            items += read(data)
        return Listing(tuple(items), is_complete=next_url is None)

    def _get_json(self, url: str) -> tuple[Any, str | None]:
        """The JSON at url and the next page's URL (GitHub's Link header)."""
        with self._lock:
            token = self._token()
            key = (url, _fingerprint(token))
            cached = self._cache.get(key)
            asked = {"If-None-Match": cached.etag} if cached else {}
            response = self._send(url, token, asked, follow_redirects=True)
            if cached and response.status_code == 304:
                return _json(cached.body), cached.next_url
            self._check(response, token)
            next_url = response.links.get("next", {}).get("url")
            if next_url is not None and not next_url.startswith(f"{API_URL}/"):
                raise GitHubError(
                    "GitHub's next page pointed to another site, so it was not followed.",
                    details=f"Expected a page on {API_URL}.",
                )
            if etag := response.headers.get("etag"):
                self._cache.put(key, _Cached(etag, response.content, next_url))
            return _json(response.content), next_url

    def _token(self) -> str | None:
        return None if self._tokens is None else self._tokens.access_token()

    def _send(
        self, url: str, token: str | None, extra: dict[str, str], follow_redirects: bool
    ) -> httpx.Response:
        auth = {"Authorization": f"Bearer {token}"} if token else {}
        attempt = 0
        while True:
            try:
                response = self._http.get(
                    url, headers=API_HEADERS | extra | auth, follow_redirects=follow_redirects
                )
            except httpx.HTTPError as error:
                raise unreachable(error) from error
            log.debug(
                "github request",
                extra={
                    "path": httpx.URL(url).path,
                    "status": response.status_code,
                    "reads_left": response.headers.get("x-ratelimit-remaining"),
                },
            )
            wait = _limit_wait(response, attempt, token is not None, self._clock())
            if wait is None:
                return response
            self._sleep(wait)
            attempt += 1

    def _check(self, response: httpx.Response, token: str | None) -> None:
        status = response.status_code
        if status < 400:
            return
        where = f"{response.request.url.path}: {_message(response)}"
        if status == 401 and token is not None and self._tokens is not None:
            self._tokens.sign_in_ended(token)
            raise GitHubAuthEnded(
                "Your GitHub sign-in ended; sign in again.",
                details=f"GitHub no longer accepts it. {where}",
            )
        if status == 404:
            raise GitHubNotFound("GitHub found nothing there, or it is private.", details=where)
        raise GitHubError(f"GitHub refused the request (HTTP {status}).", details=where)


def _limit_wait(
    response: httpx.Response, attempt: int, is_signed_in: bool, now: float
) -> float | None:
    """Seconds to wait before trying again, or None when the answer is not a rate limit."""
    if response.status_code not in (403, 429):
        return None
    headers = response.headers
    reset = headers.get("x-ratelimit-reset", "")
    if headers.get("x-ratelimit-remaining") == "0" and reset.isdigit():
        raise GitHubRateLimited(datetime.fromtimestamp(int(reset), UTC), is_signed_in)
    retry_after = headers.get("retry-after", "")
    if retry_after.isdigit():
        wait = float(retry_after)
    elif response.status_code == 429 or "rate limit" in response.text.lower():
        wait = float(_FIRST_WAIT_SECONDS * 2**attempt)
    else:
        return None
    if attempt >= _MAX_RETRIES or wait > _MAX_WAIT_SECONDS:
        raise GitHubRateLimited(datetime.fromtimestamp(now + wait, UTC), is_signed_in)
    return wait


def _repo_url(ref: RepoRef) -> str:
    return f"{API_URL}/repos/{ref.owner}/{ref.name}"


def _fingerprint(token: str | None) -> str:
    return hashlib.sha256(token.encode()).hexdigest() if token else ""


def _message(response: httpx.Response) -> str:
    try:
        message = str(response.json().get("message", ""))
    except (ValueError, AttributeError):
        message = response.text
    return message[:_MESSAGE_CHARS]


def _json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError as error:
        raise GitHubError(
            "GitHub's answer could not be read.", details=f"It was not JSON: {error}."
        ) from error


def parse_reply[M: BaseModel](model: type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        # Only where and what: the input could hold a token.
        problems = "; ".join(
            f"{'.'.join(map(str, problem['loc']))}: {problem['msg']}"
            for problem in error.errors(include_input=False)[:3]
        )
        raise GitHubError(
            "GitHub's answer was not what SetupPilot expected.",
            details=f"{model.__name__.lstrip('_')}: {problems}",
        ) from error


@dataclass(frozen=True)
class _Cached:
    etag: str
    body: bytes
    next_url: str | None


class _EtagCache:
    """Answers kept for If-None-Match; the oldest go first once they pass max_bytes."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._entries: OrderedDict[tuple[str, str], _Cached] = OrderedDict()
        self._bytes = 0

    def get(self, key: tuple[str, str]) -> _Cached | None:
        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
        return cached

    def put(self, key: tuple[str, str], cached: _Cached) -> None:
        if (old := self._entries.pop(key, None)) is not None:
            self._bytes -= len(old.body)
        self._entries[key] = cached
        self._bytes += len(cached.body)
        while self._bytes > self._max_bytes:
            _, dropped = self._entries.popitem(last=False)
            self._bytes -= len(dropped.body)


class _ChunkReader(io.RawIOBase):
    """A file-like view of a download, so tarfile can read it while it arrives."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._left = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: "WriteableBuffer") -> int:
        while not self._left:
            chunk = next(self._chunks, None)
            if chunk is None:
                return 0
            self._left = chunk
        view = memoryview(buffer).cast("B")
        size = min(len(view), len(self._left))
        view[:size] = self._left[:size]
        self._left = self._left[size:]
        return size
