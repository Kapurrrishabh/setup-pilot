"""Accept only https://github.com/<owner>/<repo>. Everything else is refused."""

import re
from dataclasses import dataclass

from setuppilot.errors import BadURL

# ASCII-only on purpose, so look-alike letters from other alphabets cannot pass.
_GITHUB_REPO = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))"
    r"/(?P<name>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?"
)
_MAX_URL_LENGTH = 200


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}"

    @property
    def clone_url(self) -> str:
        return f"{self.url}.git"


def parse_github_url(text: str) -> RepoRef:
    url = text.strip()
    match = _GITHUB_REPO.fullmatch(url) if len(url) <= _MAX_URL_LENGTH else None
    if match is None or match["name"] in {".", ".."}:
        raise BadURL(
            "Please give a public GitHub repo URL like https://github.com/owner/repo.",
            details=f"Not accepted: {url[:_MAX_URL_LENGTH]!r}. Only https://github.com/<owner>/<repo>"
            " works: no other sites, no http, no extra path like /tree/main, no ? or #.",
        )
    return RepoRef(match["owner"], match["name"])
