"""Read a repo straight from GitHub, without a clone: the list of its files, and the text of the
files asked for. Nothing is written to disk."""

import io
import re
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from setuppilot.config import GitHubConfig
from setuppilot.errors import PathBlocked, SetupPilotError
from setuppilot.github.api import GitHubClient, GitHubError, TreeEntry, check_repo_path
from setuppilot.tools.github_url import RepoRef, parse_github_url
from setuppilot.tools.scan import SKIP_DIRS, decode_text

_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


class SnapshotTooBig(SetupPilotError):
    """The repo's files add up to more than github.max_tarball_mb, so clone it instead."""

    def __init__(self, limit_mb: int, total_bytes: int | None) -> None:
        size = "more than that" if total_bytes is None else f"{total_bytes / 2**20:.1f} MB"
        super().__init__(
            f"This repo is bigger than the {limit_mb} MB that is read from GitHub without a clone.",
            details=f"Its files add up to {size}. Clone it instead, or raise "
            "github.max_tarball_mb in config.yaml.",
        )
        # None when GitHub's file tree was cut short, so the full size is not known.
        self.total_bytes = total_bytes


@dataclass(frozen=True)
class RepoSnapshot:
    ref: RepoRef
    commit_sha: str
    is_private: bool
    default_branch: str
    entries: tuple[TreeEntry, ...]
    # The text of each file that was asked for, is small enough and is not binary.
    files: Mapping[str, str]

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries if not entry.is_link)


def read_snapshot(
    ref: RepoRef,
    client: GitHubClient,
    config: GitHubConfig,
    wanted: Callable[[str], bool],
    max_file_bytes: int,
) -> RepoSnapshot:
    """Three GitHub calls: the repo, its file tree (to refuse a big repo before any download),
    and one download of all its files, read while it arrives. Files in SKIP_DIRS are never read."""
    repo = client.get_repo(ref)
    # GitHub's own name, so a renamed repo needs no redirect (the download never follows one).
    named = parse_github_url(f"https://github.com/{repo.full_name}")
    tree = client.get_tree(named, repo.default_branch)
    # A cut-short tree's sum is only part of the total, but over the limit is still too big.
    if tree.total_bytes > config.max_tarball_mb * 2**20:
        total = None if tree.is_truncated else tree.total_bytes
        raise SnapshotTooBig(config.max_tarball_mb, total)
    with client.open_tarball(named, repo.default_branch) as stream:
        commit_sha, entries, files = _read_archive(
            stream, wanted, max_file_bytes, config.max_tarball_mb
        )
    return RepoSnapshot(named, commit_sha, repo.is_private, repo.default_branch, entries, files)


def _read_archive(
    stream: io.RawIOBase, wanted: Callable[[str], bool], max_file_bytes: int, limit_mb: int
) -> tuple[str, tuple[TreeEntry, ...], dict[str, str]]:
    entries: list[TreeEntry] = []
    files: dict[str, str] = {}
    total, top = 0, None
    try:
        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
            for member in archive:
                # GitHub puts every file under one folder named after the repo and commit.
                folder, _, path = member.name.partition("/")
                top = top or folder
                # An empty first part is a path from the root, and must not leave top unset.
                if not folder or folder != top:
                    raise PathBlocked(
                        "GitHub's download holds a file outside the repo's folder, so it was not "
                        "read.",
                        details=f"Refused path: {member.name[:200]!r}.",
                    )
                if member.isdir() or not path:
                    continue
                check_repo_path(path)
                if member.issym() or member.islnk():
                    entries.append(TreeEntry(path, 0, is_link=True))
                    continue
                if not member.isreg():
                    continue
                total += member.size
                # Checked before the file is read, so a huge file is never unpacked.
                if total > limit_mb * 2**20:
                    raise SnapshotTooBig(limit_mb, None)
                entries.append(TreeEntry(path, member.size, is_link=False))
                if member.size > max_file_bytes or not wanted(path):
                    continue
                if not SKIP_DIRS.isdisjoint(path.split("/")[:-1]):
                    continue
                data = archive.extractfile(member)
                if data is not None and (text := decode_text(data.read())) is not None:
                    files[path] = text
            commit_sha = archive.pax_headers.get("comment", "")
    except (tarfile.TarError, EOFError) as error:
        raise GitHubError(
            "GitHub's download could not be read as an archive.",
            details=f"{type(error).__name__}: {error}. Try again in a few minutes.",
        ) from error
    if not _COMMIT_SHA.fullmatch(commit_sha):
        raise GitHubError(
            "GitHub's download did not say which commit it holds, so it was not used.",
            details="Try again in a few minutes. If it keeps happening, please report it.",
        )
    return commit_sha, tuple(entries), files
