"""Shallow clone of a GitHub repo, refused before its files are checked out if it is too big."""

import base64
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

import httpx

from setuppilot.config import GitHubApp, RepoConfig
from setuppilot.errors import CloneFailed, PathBlocked, PrereqMissing
from setuppilot.github.api import GitHubClient, GitHubError, GitHubNotFound, Tree
from setuppilot.github.vault import TokenVault
from setuppilot.guardrails.commands import ExecTarget
from setuppilot.guardrails.runner import HOST_TARGET
from setuppilot.tools.github_url import RepoRef, parse_github_url

log = logging.getLogger(__name__)

RepoAccess = Literal["public", "github"]
_STDERR_TAIL = 2000
# Only https, and no saved passwords: a private repo must fail, not clone quietly.
_GIT_SAFETY = (
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "credential.helper=",
    "-c",
    "core.askPass=",
    # Windows: paths past 260 characters. Everywhere: scripts keep LF line endings, so a
    # Windows clone still runs in Docker's Linux (.gitattributes eol=crlf is still followed).
    "-c",
    "core.longpaths=true",
    "-c",
    "core.autocrlf=false",
)
# Inherited settings that could print the token (traces) or add git config of their own.
_INHERITED_GIT = re.compile(
    r"GIT_ASKPASS|SSH_ASKPASS|GIT_TRACE.*|GIT_REDACT.*|GIT_CURL_VERBOSE"
    r"|GIT_CONFIG_(PARAMETERS|COUNT|KEY_\d+|VALUE_\d+)"
)
# Plain folder names only, so nothing in one can mean something special to git's patterns.
_SUBDIR = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")
_SUBDIR_CHARS = 200
SIGN_IN_OFF = (
    "Sign in with GitHub is off.",
    "To turn it on, register your own GitHub App, put its Client ID and slug in config.yaml "
    "under github.app, and restart SetupPilot. docs/SECURITY.md has the steps.",
)


def git_options(host: ExecTarget) -> tuple[str, ...]:
    """Windows checks out links as plain files on every machine, not only where the user
    turned links on (making them needs admin rights there)."""
    return _GIT_SAFETY + (("-c", "core.symlinks=false") if host == "windows" else ())


@dataclass(frozen=True)
class SignedIn:
    """The GitHub App sign-in: the client that reads with the user's token, the vault that holds
    it, and what signing in and out needs (the app, http and a clock)."""

    app: GitHubApp
    http: httpx.Client
    clock: Callable[[], float]
    client: GitHubClient
    vault: TokenVault


@dataclass(frozen=True)
class GitHubAccess:
    """How SetupPilot reads GitHub. Public reads never send a token."""

    public: GitHubClient
    # None when sign-in is off: there is no github.app in config.yaml.
    signed_in: SignedIn | None


def github_http() -> httpx.Client:
    # The same 10 seconds the repo check had before the GitHub client.
    return httpx.Client(timeout=10)


def public_github() -> GitHubAccess:
    """For the terminal, which has no sign-in."""
    return GitHubAccess(GitHubClient(github_http(), None, time.time, time.sleep), signed_in=None)


def reader_for(github: GitHubAccess, access: RepoAccess) -> tuple[GitHubClient, SignedIn | None]:
    """The client to read with, and the sign-in it reads through (None for a public read)."""
    if access == "public":
        return github.public, None
    if github.signed_in is None:
        raise CloneFailed(*SIGN_IN_OFF)
    return github.signed_in.client, github.signed_in


def not_found(ref: RepoRef, access: RepoAccess) -> CloneFailed:
    if access == "public":
        return CloneFailed(
            "This repo is private or missing. If it is yours, sign in with GitHub and pick it "
            "from your repos.",
            details=f"GitHub found no public repo at {ref.url}. Check the name.",
        )
    return CloneFailed(
        "GitHub found no repo there that your sign-in may read.",
        details=f"{ref.url} may be missing, or not among the repos your SetupPilot GitHub App is "
        "installed on. Add it to the app's repos on GitHub, then try again.",
    )


@dataclass(frozen=True)
class CloneRequest:
    """How a run reaches its repo: "public" needs no sign-in; "github" uses the signed-in user.
    subdir, when set, is the one project folder of a monorepo to check out."""

    access: RepoAccess
    subdir: str | None

    def __post_init__(self) -> None:
        if self.access not in get_args(RepoAccess):
            raise CloneFailed(
                "SetupPilot can read a repo only as public, or through your GitHub sign-in.",
                details=f"access must be 'public' or 'github', not {self.access!r}.",
            )
        subdir = self.subdir
        if subdir is not None and (
            len(subdir) > _SUBDIR_CHARS
            or not _SUBDIR.fullmatch(subdir)
            or {".", ".."} & set(subdir.split("/"))
        ):
            raise PathBlocked(
                "The sub-folder must be a folder inside the repo, like packages/web.",
                details=f"Not accepted: {subdir[:_SUBDIR_CHARS]!r}. Use letters, digits, '.', "
                "'_' and '-', with '/' between folder names, and no '..'.",
            )


@dataclass(frozen=True)
class _Auth:
    """A signed-in clone: git options, and the environment that carries the token."""

    options: tuple[str, ...]
    env: dict[str, str] = field(repr=False)


def _auth(url: str, token: str) -> _Auth:
    """The token goes as a header for this one repo URL, set through git's command-scope config:
    it lives only in git's environment, never in argv, the URL, logs or .git/config. No redirects,
    so the header cannot follow one to another site."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{url}.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }
    return _Auth(("-c", "http.followRedirects=false"), env)


def clone_repo(
    ref: RepoRef, dest_dir: Path, config: RepoConfig, github: GitHubAccess, request: CloneRequest
) -> Path:
    """Clone into dest_dir/<repo name>. Returns the new folder. GitHub's file list gives the size
    check before anything is downloaded; a sub-folder clone downloads only that folder's files."""
    if not dest_dir.is_dir():
        raise CloneFailed(
            "The destination folder does not exist.", details=f"Create {dest_dir} first."
        )
    target = repo_folder(ref, dest_dir)
    if target.exists():
        raise CloneFailed(
            "A folder with this repo's name is already there.",
            details=f"{target} exists. Pick another destination folder, or remove it yourself.",
        )
    client, signed_in = reader_for(github, request.access)
    try:
        found = _look_up(ref, client, request)
        named, tree = found if found else (ref, None)
        url = named.clone_url
        auth = None if signed_in is None else _auth(url, signed_in.vault.access_token())
        size = None if tree is None else _api_size(tree, request.subdir)
        if size is not None:
            _check_size(size, config, None)
        git_clone(url, target, config, auth, request.subdir)
        if tree is not None:
            size = _api_size(_cloned_tree(named, tree, client, target, config), request.subdir)
    except GitHubError as error:
        raise CloneFailed(error.message, details=error.details) from error
    # Only a full clone can have no size from GitHub, and it already holds every file's data.
    _check_size(_listed_size(target, config) if size is None else size, config, target)
    if request.subdir is not None:
        _git(
            ["-C", str(target), "sparse-checkout", "set", "--cone", "--", request.subdir],
            config,
            "git sparse-checkout",
            "The details show what git said.",
            auth,
        )
    _git(
        ["-C", str(target), "checkout", "-q"],
        config,
        "git checkout",
        "Check that the disk has free space; the details show what git said.",
        auth,
    )
    return target


def _look_up(
    ref: RepoRef, client: GitHubClient, request: CloneRequest
) -> tuple[RepoRef, Tree] | None:
    """GitHub's own name for the repo (so a renamed repo needs no redirect), and its file list.
    None when GitHub cannot be asked about a public repo: git is not held to the API's read
    limit, so a full clone still goes ahead."""
    try:
        repo = client.get_repo(ref)
        named = parse_github_url(f"https://github.com/{repo.full_name}")
        return named, client.get_tree(named, repo.default_branch)
    except GitHubNotFound as error:
        raise not_found(ref, request.access) from error
    except GitHubError as error:
        if request.access == "github":
            raise
        if request.subdir is not None:
            raise CloneFailed(
                error.message,
                details=f"{error.details} A sub-folder's size is checked from GitHub's file "
                "list, so it cannot be cloned without it.",
            ) from error
        log.warning("github repo check skipped", extra={"reason": error.message, "repo": ref.url})
        return None


def _cloned_tree(
    named: RepoRef, tree: Tree, client: GitHubClient, target: Path, config: RepoConfig
) -> Tree:
    """The file list of the commit that was cloned. Someone may have pushed since GitHub listed
    the files; then that commit's list is fetched, so the size check fits what is checked out."""
    cloned = _git(
        ["-C", str(target), "rev-parse", "HEAD", "HEAD^{tree}"],
        config,
        "git rev-parse",
        "The details show what git said.",
        None,
    ).split()
    # Measured on pypa/sampleproject: GitHub names the list by its commit, not its tree. Either
    # one pins the exact files, so either may match.
    if tree.sha in cloned:
        return tree
    tree = client.get_tree(named, cloned[0])
    if tree.sha not in cloned:
        raise CloneFailed(
            "The cloned files do not match what GitHub lists for that commit, so they were not "
            "checked out.",
            details=f"GitHub listed {tree.sha} for commit {cloned[0]}. Delete {target} and try "
            "again.",
        )
    return tree


def _api_size(tree: Tree, subdir: str | None) -> int | None:
    """What the checkout writes, from GitHub's file list: every file, or for a sub-folder (git's
    cone mode) its files plus the files directly in the root and in each folder above it. None
    for a full clone when the list was cut short; the clone then adds up its own files."""
    if subdir is None:
        return None if tree.is_truncated else tree.total_bytes
    if tree.is_truncated:
        raise CloneFailed(
            "This repo has too many files for GitHub to list at once, so the size of one "
            "sub-folder cannot be checked.",
            details="Clone the whole repo instead: leave the sub-folder empty.",
        )
    if not any(entry.path.startswith(f"{subdir}/") for entry in tree.entries):
        raise CloneFailed(
            f"This repo has no folder {subdir}.",
            details="Check the folder name on GitHub. Names are case-sensitive.",
        )
    parts = subdir.split("/")
    above = {"/".join(parts[:n]) for n in range(len(parts))}
    return sum(
        e.size
        for e in tree.entries
        if not e.is_link and (e.path.startswith(f"{subdir}/") or e.path.rpartition("/")[0] in above)
    )


def _listed_size(target: Path, config: RepoConfig) -> int:
    listing = _git(
        ["-C", str(target), "ls-tree", "-r", "-l", "--full-tree", "HEAD"],
        config,
        "git ls-tree",
        "The repo may be empty (no commits yet).",
        None,
    )
    return sum(int(line.split()[3]) for line in listing.splitlines() if line.split()[3].isdigit())


def _check_size(size: int, config: RepoConfig, target: Path | None) -> None:
    """target is the folder git already wrote to, or None when nothing was downloaded yet."""
    if size <= config.max_size_mb * 2**20:
        return
    left = f"Only git data is left in {target}; delete it, or raise" if target else "Raise"
    raise CloneFailed(
        f"This repo is bigger than the {config.max_size_mb} MB limit, so its files were not "
        "checked out.",
        details=f"Its files add up to {size / 2**20:.1f} MB. {left} repo.max_size_mb in "
        "config.yaml.",
    )


def git_clone(
    url: str, target: Path, config: RepoConfig, auth: _Auth | None, subdir: str | None
) -> None:
    """Clone without files. A sub-folder clone also leaves out every file's data, so the sparse
    checkout later fetches only what it needs."""
    sparse = ["--filter=blob:none", "--sparse"] if subdir is not None else []
    _git(
        [
            "clone",
            "--no-checkout",
            "--depth",
            "1",
            "--single-branch",
            "--no-tags",
            *sparse,
            "--",
            url,
            str(target),
        ],
        config,
        f"git clone {url}",
        "Check the URL and your internet connection.",
        auth,
    )


def head_commit(folder: Path, config: RepoConfig) -> str:
    """The commit a clone has checked out."""
    advice = f"The clone in {folder} may be broken. Delete it yourself, then try again."
    return _git(
        ["-C", str(folder), "rev-parse", "HEAD"], config, "git rev-parse", advice, None
    ).strip()


def _git(args: list[str], config: RepoConfig, label: str, advice: str, auth: _Auth | None) -> str:
    git = shutil.which("git")
    if git is None:
        raise PrereqMissing(
            "git is not installed.", details="Install git from https://git-scm.com/downloads."
        )
    # No prompts or sign-in windows (private repos fail fast) and no Git LFS downloads (they
    # can be huge).
    env = {k: v for k, v in os.environ.items() if not _INHERITED_GIT.fullmatch(k)}
    env |= {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_LFS_SKIP_SMUDGE": "1"}
    options = git_options(HOST_TARGET)
    if auth is not None:
        env |= auth.env
        options += auth.options
    try:
        done = subprocess.run(  # noqa: S603 - fixed git command, no shell
            [git, *options, *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.clone_timeout_seconds,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CloneFailed(
            f"The clone took longer than {config.clone_timeout_seconds:.0f} seconds, so it was "
            "stopped.",
            details=f"{label}. A half-finished folder may be left behind.",
        ) from error
    if done.returncode != 0:
        raise CloneFailed(
            f"{label} failed. {advice}",
            details=f"{label} exited with {done.returncode}: {done.stderr[-_STDERR_TAIL:]}",
        )
    return done.stdout


def repo_folder(ref: RepoRef, dest_dir: Path) -> Path:
    return dest_dir / ref.name
