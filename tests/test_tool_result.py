import functools

from setuppilot.errors import BadURL
from setuppilot.tools.result import run_tool


def add(a, b):
    return a + b


def refuse():
    raise BadURL("Please give a GitHub URL.", details="got ftp://x")


def crash():
    raise KeyError("bug")


def test_success_carries_the_data():
    result = run_tool(add, 2, b=3)
    assert (result.ok, result.data, result.error_type) == (True, 5, None)


def test_known_error_becomes_a_structured_failure():
    result = run_tool(refuse)
    assert (result.ok, result.error_type, result.message, result.details) == (
        False,
        "BadURL",
        "Please give a GitHub URL.",
        "got ftp://x",
    )


def test_unexpected_error_is_reported_with_its_traceback():
    result = run_tool(crash)
    assert (result.ok, result.error_type) == (False, "UnexpectedError")
    assert "KeyError" in result.details


def test_partial_tools_work_and_crashes_in_them_are_reported():
    assert run_tool(functools.partial(add, 1), 2).data == 3
    assert run_tool(functools.partial(crash)).error_type == "UnexpectedError"
