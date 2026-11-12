"""Does an installed tool's version fit what a repo asks for? None when it cannot tell."""

import re

Version = tuple[int, int, int]

# One part of a rule: an optional operator, then a version that may end in x or * (18.x, 3.*).
_PART = re.compile(
    r"\s*(>=|<=|!=|==|~=|>|<|=|\^|~)?\s*v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?"
    r"(?:[-+][0-9A-Za-z.-]*)?\s*,?"
)
_PIN = re.compile(r"v?\d+(\.\d+){0,2}")


def version_of(text: str) -> Version | None:
    """The first version in a tool's output: 'go version go1.25.1 darwin/arm64' is 1.25.1."""
    match = re.search(r"\d+(?:\.\d+)+", text) or re.search(r"\d+", text)
    return _padded([int(n) for n in match.group().split(".")[:3]]) if match else None


def is_pin(constraint: str) -> bool:
    """A plain version (3.3.0, 20.11, 18), as in .nvmrc or .ruby-version, not a range."""
    return bool(_PIN.fullmatch(constraint.strip()))


def fits(constraint: str, installed: str) -> bool | None:
    """Handles >=, >, <=, <, ==, !=, ^, ~, ~=, x and * wildcards, bare prefixes, || (or) and
    commas or spaces (and)."""
    version = version_of(installed)
    if version is None:
        return None
    results = [_fits_all(alternative, version) for alternative in constraint.split("||")]
    if True in results:
        return True
    return None if None in results or not results else False


def _fits_all(rule: str, version: Version) -> bool | None:
    rule, position, answers = rule.strip(), 0, []
    while position < len(rule):
        match = _PART.match(rule, position)
        if match is None or match.end() == position:
            return None
        operator, *raw = match.groups()
        numbers = [int(n) for n in _before_wildcard([p for p in raw if p is not None])]
        answer = _compare(operator or "=", numbers, version)
        if answer is None:
            return None
        answers.append(answer)
        position = match.end()
    return all(answers) if answers else None


def _before_wildcard(parts: list[str]) -> list[str]:
    numbers = []
    for part in parts:
        if not part.isdigit():
            break
        numbers.append(part)
    return numbers


def _compare(operator: str, numbers: list[int], version: Version) -> bool | None:
    if not numbers:
        return True if operator in ("=", "==") else None  # "*" or "x" alone: any version
    low = _padded(numbers)
    match operator:
        case "=" | "==":
            return version[: len(numbers)] == tuple(numbers)
        case "!=":
            return version[: len(numbers)] != tuple(numbers)
        case ">=":
            return version >= low
        case ">":
            return version > low
        case "<=":
            return version[: len(numbers)] <= tuple(numbers)
        case "<":
            return version < low
        case "^":
            # The first part that is not 0 may not change: ^1.2 is below 2, ^0.2 is below 0.3.
            first = next((i for i, n in enumerate(numbers) if n), len(numbers) - 1)
            return low <= version < _bumped(numbers, first)
        case "~":
            return low <= version < _bumped(numbers, min(1, len(numbers) - 1))
        case "~=":
            # Python's compatible release: ~=3.8 means 3.8 or newer, but still 3.x.
            if len(numbers) < 2:
                return None
            return low <= version < _bumped(numbers, len(numbers) - 2)
    return None


def _bumped(numbers: list[int], index: int) -> Version:
    return _padded([*numbers[:index], numbers[index] + 1])


def _padded(numbers: list[int]) -> Version:
    a, b, c = (numbers + [0, 0, 0])[:3]
    return (a, b, c)
