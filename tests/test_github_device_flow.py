import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from setuppilot.config import GitHubApp
from setuppilot.github import device_flow
from setuppilot.github.api import GitHubAuthEnded, GitHubError
from setuppilot.github.device_flow import (
    DEVICE_CODE_URL,
    REVOKE_URL,
    TOKEN_URL,
    Failed,
    Granted,
    Pending,
    SlowDown,
    parse_grant,
    parse_poll_reply,
)
from tests.github_fakes import FakeGitHub, ok

APP = GitHubApp(client_id="Iv23liTest", app_slug="setuppilot-test")
NOW = 1_800_000_000.0
CODE_REPLY = {
    "device_code": "3584d83530557fdd1f46af8289938c8ef79f9dc5",
    "user_code": "WDJB-MJHT",
    "verification_uri": "https://github.com/login/device",
    "expires_in": 900,
    "interval": 5,
}
GRANT_REPLY = {
    "access_token": "ghu_new",
    "expires_in": 28800,
    "refresh_token": "ghr_new",
    "refresh_token_expires_in": 15897600,
    "scope": "",
    "token_type": "bearer",
}


def form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


@pytest.mark.parametrize(
    ("code", "says"),
    [
        ("expired_token", "ran out"),
        ("token_expired", "ran out"),
        ("access_denied", "Cancel"),
        ("incorrect_client_credentials", "Client ID"),
        ("incorrect_device_code", "did not accept the sign-in code"),
        ("bad_verification_code", "did not accept the sign-in code"),
        ("unsupported_grant_type", "SetupPilot's sign-in request"),
        ("device_flow_disabled", "turned off"),
        ("Not Found", "Client ID"),
        ("something_new", "something_new"),
    ],
)
def test_every_sign_in_error_says_what_happened(code, says):
    result = parse_poll_reply({"error": code}, 5, NOW, APP)
    assert isinstance(result, Failed)
    assert says in result.message + result.details


def test_device_flow_turned_off_says_where_to_turn_it_on():
    result = parse_poll_reply({"error": "device_flow_disabled"}, 5, NOW, APP)
    assert isinstance(result, Failed)
    assert "https://github.com/settings/apps/setuppilot-test" in result.details


def test_waiting_for_the_user_is_pending():
    assert parse_poll_reply({"error": "authorization_pending"}, 5, NOW, APP) == Pending()


def test_slow_down_uses_the_interval_github_sends():
    assert parse_poll_reply({"error": "slow_down", "interval": 10}, 5, NOW, APP) == SlowDown(10)


def test_slow_down_without_an_interval_waits_5_seconds_longer():
    assert parse_poll_reply({"error": "slow_down"}, 5, NOW, APP) == SlowDown(10)


def test_a_grant_knows_when_both_tokens_run_out():
    result = parse_poll_reply(GRANT_REPLY, 5, NOW, APP)
    assert isinstance(result, Granted) and result.grant.renewal is not None
    renewal = result.grant.renewal
    assert (renewal.access_expires_at, renewal.refresh_expires_at) == (
        datetime.fromtimestamp(NOW + 28800, UTC),
        datetime.fromtimestamp(NOW + 15897600, UTC),
    )


def test_a_grant_that_never_runs_out_has_no_renewal():
    grant = parse_grant({"access_token": "ghu_x", "token_type": "bearer", "scope": ""}, NOW)
    assert grant.renewal is None


def test_a_grant_with_only_part_of_its_expiry_is_refused():
    partial = {"access_token": "ghu_x", "token_type": "bearer", "expires_in": 28800}
    with pytest.raises(GitHubError, match="only part"):
        parse_grant(partial, NOW)


def test_tokens_are_hidden_when_a_grant_is_printed():
    grant = parse_grant(GRANT_REPLY, NOW)
    assert "ghu_new" not in repr(grant) and "ghr_new" not in str(grant)


def test_start_sends_only_the_client_id():
    fake = FakeGitHub()
    fake.on("POST", DEVICE_CODE_URL, ok(CODE_REPLY))
    device_flow.start(fake.http(), APP, NOW)
    assert form(fake.requests[0]) == {"client_id": ["Iv23liTest"]}


def test_start_gives_the_code_to_type_and_when_it_runs_out():
    fake = FakeGitHub()
    fake.on("POST", DEVICE_CODE_URL, ok(CODE_REPLY))
    pending = device_flow.start(fake.http(), APP, NOW)
    assert (pending.code.user_code, pending.expires_at, pending.next_poll_at) == (
        "WDJB-MJHT",
        NOW + 900,
        NOW + 5,
    )


def test_start_refuses_a_page_to_type_the_code_that_is_not_githubs():
    fake = FakeGitHub()
    fake.on("POST", DEVICE_CODE_URL, ok(CODE_REPLY | {"verification_uri": "https://evil.example"}))
    with pytest.raises(GitHubError, match="not what SetupPilot expected"):
        device_flow.start(fake.http(), APP, NOW)


def test_start_with_an_unknown_client_id_says_so():
    fake = FakeGitHub()
    fake.on("POST", DEVICE_CODE_URL, httpx.Response(404, json={"error": "Not Found"}))
    with pytest.raises(GitHubError, match="does not know the Client ID"):
        device_flow.start(fake.http(), APP, NOW)


@pytest.mark.parametrize(
    ("url", "call"),
    [
        (DEVICE_CODE_URL, lambda http: device_flow.start(http, APP, NOW)),
        (TOKEN_URL, lambda http: device_flow.refresh(http, APP, "ghr_old", NOW)),
    ],
)
def test_a_refusal_without_an_error_code_shows_the_status_and_githubs_message(url, call):
    fake = FakeGitHub()
    fake.on("POST", url, httpx.Response(429, json={"message": "rate limited"}))
    with pytest.raises(GitHubError) as raised:
        call(fake.http())
    assert (raised.value.message, raised.value.details) == (
        "GitHub refused the sign-in (HTTP 429).",
        "GitHub said: 'rate limited'. Start the sign-in again.",
    )


def started(fake: FakeGitHub) -> device_flow.PendingSignIn:
    fake.on("POST", DEVICE_CODE_URL, ok(CODE_REPLY))
    return device_flow.start(fake.http(), APP, NOW)


def test_poll_does_not_call_github_before_the_interval():
    fake = FakeGitHub()
    pending = started(fake)
    assert device_flow.poll(fake.http(), APP, pending, NOW + 4) == Pending()
    assert len(fake.requests) == 1


def test_poll_sends_the_device_code_in_the_body_not_the_url():
    fake = FakeGitHub()
    pending = started(fake)
    fake.on("POST", TOKEN_URL, ok({"error": "authorization_pending"}))
    device_flow.poll(fake.http(), APP, pending, NOW + 5)
    assert form(fake.requests[1])["device_code"] == [CODE_REPLY["device_code"]]


def test_poll_after_slow_down_waits_longer_before_the_next_call():
    fake = FakeGitHub()
    pending = started(fake)
    fake.on("POST", TOKEN_URL, ok({"error": "slow_down", "interval": 10}))
    device_flow.poll(fake.http(), APP, pending, NOW + 5)
    device_flow.poll(fake.http(), APP, pending, NOW + 14)
    assert (pending.next_poll_at, len(fake.requests)) == (NOW + 15, 2)


def test_a_poll_that_failed_still_waits_before_calling_again():
    fake = FakeGitHub()
    pending = started(fake)
    fake.on("POST", TOKEN_URL, httpx.Response(502, text="<html>Bad gateway</html>"))
    with pytest.raises(GitHubError):
        device_flow.poll(fake.http(), APP, pending, NOW + 5)
    assert device_flow.poll(fake.http(), APP, pending, NOW + 6) == Pending()
    assert len(fake.requests) == 2


def test_poll_after_the_code_ran_out_fails_without_calling_github():
    fake = FakeGitHub()
    pending = started(fake)
    result = device_flow.poll(fake.http(), APP, pending, NOW + 900)
    assert (isinstance(result, Failed), len(fake.requests)) == (True, 1)


def test_poll_returns_the_grant_when_the_user_is_done():
    fake = FakeGitHub()
    pending = started(fake)
    fake.on("POST", TOKEN_URL, ok(GRANT_REPLY))
    result = device_flow.poll(fake.http(), APP, pending, NOW + 5)
    assert isinstance(result, Granted)
    assert result.grant.access_token.get_secret_value() == "ghu_new"


def test_refresh_sends_no_client_secret_and_returns_a_new_pair():
    fake = FakeGitHub()
    fake.on("POST", TOKEN_URL, ok(GRANT_REPLY))
    grant = device_flow.refresh(fake.http(), APP, "ghr_old", NOW)
    assert grant.renewal is not None
    assert grant.renewal.refresh_token.get_secret_value() == "ghr_new"
    assert form(fake.requests[0]) == {
        "client_id": ["Iv23liTest"],
        "grant_type": ["refresh_token"],
        "refresh_token": ["ghr_old"],
    }


def test_a_refused_refresh_token_ends_the_sign_in():
    fake = FakeGitHub()
    fake.on("POST", TOKEN_URL, ok({"error": "bad_refresh_token"}))
    with pytest.raises(GitHubAuthEnded):
        device_flow.refresh(fake.http(), APP, "ghr_old", NOW)


def test_revoke_sends_both_tokens_without_authorization():
    fake = FakeGitHub()
    fake.on("POST", REVOKE_URL, httpx.Response(202, json={}))
    device_flow.revoke(fake.http(), ["ghu_x", "ghr_y"])
    request = fake.requests[0]
    assert ("authorization" in request.headers, json.loads(request.content)) == (
        False,
        {"credentials": ["ghu_x", "ghr_y"]},
    )


def test_revoke_that_github_did_not_accept_says_how_to_do_it_by_hand():
    fake = FakeGitHub()
    fake.on("POST", REVOKE_URL, httpx.Response(422, json={"message": "Validation Failed"}))
    with pytest.raises(GitHubError) as raised:
        device_flow.revoke(fake.http(), ["ghu_x"])
    assert "Authorized GitHub Apps" in raised.value.details
