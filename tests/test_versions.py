import pytest

from setuppilot.tools.versions import fits, is_pin, version_of


@pytest.mark.parametrize(
    ("constraint", "installed", "expected"),
    [
        (">=18", "v20.11.0", True),
        (">=22.12", "v20.11.0", False),
        (">18", "v18.0.0", False),
        ("<=1.2", "1.2.9", True),
        ("<2", "2.0.0", False),
        ("==3.11.*", "Python 3.11.4", True),
        ("!=3.0", "3.0.1", False),
        ("^1.2.3", "1.9.0", True),
        ("^1.2.3", "2.0.0", False),
        ("^0.2.3", "0.3.0", False),
        ("~1.2.3", "1.2.9", True),
        ("~1.2.3", "1.3.0", False),
        ("~=3.8", "3.12.1", True),
        ("~=3.8", "4.0", False),
        ("18.x", "v18.19.0", True),
        ("18.x", "v20.0.0", False),
        ("3.11", "Python 3.11.4", True),
        ("3.11", "Python 3.12.0", False),
        ("*", "1.0.0", True),
        (">=3.8,<4", "3.12.1", True),
        (">=1.2 <2", "1.5.0", True),
        (">=1.2 <2", "2.1.0", False),
        ("^16 || ^18", "v18.1.0", True),
        ("^16 || ^18", "v20.1.0", False),
        (">=1.22", "go version go1.25.1 darwin/arm64", True),
        ("17", 'openjdk version "17.0.9" 2023-10-17', True),
        ("3.3.0-preview1", "ruby 3.3.0", True),
    ],
)
def test_fits_compares_each_kind_of_version_rule(constraint, installed, expected):
    assert fits(constraint, installed) is expected


@pytest.mark.parametrize(
    ("constraint", "installed"),
    [("lts/*", "v20.0.0"), ("stable", "1.80.0"), (">=18", "no version here"), ("", "1.0"),
     ("~=3", "3.1"), ("1.2 - 2.3", "1.5"), ("^16 || stable", "v20.0.0")],
)  # fmt: skip
def test_fits_says_none_when_it_cannot_tell(constraint, installed):
    assert fits(constraint, installed) is None


def test_one_known_match_is_enough_even_when_another_part_cannot_be_read():
    assert fits("^20 || lts/*", "v20.1.0") is True


@pytest.mark.parametrize(
    ("output", "version"),
    [("v20.11.0", (20, 11, 0)), ("go version go1.25.1 darwin/arm64", (1, 25, 1)),
     ("cargo 1.80.0 (376290515 2024-07-16)", (1, 80, 0)), ("Bundler version 2", (2, 0, 0)),
     ("nothing", None)],
)  # fmt: skip
def test_version_of_reads_the_first_version_in_a_tools_output(output, version):
    assert version_of(output) == version


@pytest.mark.parametrize(
    ("constraint", "expected"), [("20.11", True), ("v18", True), (">=18", False), ("18.x", False)]
)
def test_only_a_plain_version_counts_as_a_pin(constraint, expected):
    assert is_pin(constraint) is expected
