"""Shallow clone of a GitHub repo, refused before any file is written if it is too big."""

import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from setuppilot.config import RepoConfig
from setuppilot.errors import CloneFailed, PrereqMissing
from setuppilot.tools.github_url import RepoRef

log = logging.getLogger(__name__)

# Raises CloneFailed when GitHub says the repo does not exist (or is private).
RepoCheck = Callable[[RepoRef], None]
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
)


def check_github_repo(ref: RepoRef) -> None:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{ref.owner}/{ref.name}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "setuppilot"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10):  # noqa: S310 - fixed https URL
            return
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise CloneFailed(
                "This repo was not found. It may be private or the name may be wrong.",
                details=f"GitHub returned 404 for {ref.url}.",
            ) from error
        reason = f"HTTP {error.code}"
    except OSError as error:
        reason = str(error)
    # Not fatal: the clone itself fails clearly if the repo is missing or private.
    log.warning("github repo check skipped", extra={"reason": reason, "repo": ref.url})


def git_clone(url: str, target: Path, config: RepoConfig) -> None:
    """Clone without files, add up the file sizes, and check out only if they fit."""
    _git(
        [
            "clone",
            "--no-checkout",
            "--depth",
            "1",
            "--single-branch",
            "--no-tags",
            "--",
            url,
            str(target),
        ],
        config,
        f"git clone {url}",
    )
    listing = _git(
        ["-C", str(target), "ls-tree", "-r", "-l", "--full-tree", "HEAD"], config, "git ls-tree"
    )
    size = sum(int(line.split()[3]) for line in listing.splitlines() if line.split()[3].isdigit())
    limit = config.max_size_mb * 1024 * 1024
    if size > limit:
        raise CloneFailed(
            f"This repo is bigger than the {config.max_size_mb} MB limit, so its files were not "
            "downloaded.",
            details=f"Its files add up to {size / 2**20:.1f} MB. Only git data is left in "
            f"{target}; delete it, or raise repo.max_size_mb in config.yaml.",
        )
    _git(["-C", str(target), "checkout", "-q"], config, "git checkout")


def _git(args: list[str], config: RepoConfig, label: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise PrereqMissing(
            "git is not installed.", details="Install git from https://git-scm.com/downloads."
        )
    # No prompts (private repos fail fast) and no Git LFS downloads (they can be huge).
    hidden = {"GIT_ASKPASS", "SSH_ASKPASS"}
    env = {k: v for k, v in os.environ.items() if k not in hidden}
    env |= {"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    try:
        done = subprocess.run(  # noqa: S603 - fixed git command, no shell
            [git, *_GIT_SAFETY, *args],
            capture_output=True,
            text=True,
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
            "git clone failed. Check the URL and your internet connection.",
            details=f"{label} exited with {done.returncode}: {done.stderr[-_STDERR_TAIL:]}",
        )
    return done.stdout


def clone_repo(ref: RepoRef, dest_dir: Path, config: RepoConfig, repo_check: RepoCheck) -> Path:
    """Clone into dest_dir/<repo name>. Returns the new folder."""
    if not dest_dir.is_dir():
        raise CloneFailed(
            "The destination folder does not exist.", details=f"Create {dest_dir} first."
        )
    target = dest_dir / ref.name
    if target.exists():
        raise CloneFailed(
            "A folder with this repo's name is already there.",
            details=f"{target} exists. Pick another destination folder, or remove it yourself.",
        )
    repo_check(ref)
    git_clone(ref.clone_url, target, config)
    return target
