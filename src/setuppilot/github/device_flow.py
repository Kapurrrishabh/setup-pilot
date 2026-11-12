"""Sign in with a GitHub App through GitHub's device flow, which needs no client secret.

The reply parsing is pure; start, poll, refresh and revoke do the network calls around it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer, SecretStr

from setuppilot.config import GitHubApp
from setuppilot.github.api import (
    API_HEADERS,
    API_URL,
    USER_AGENT,
    GitHubAuthEnded,
    GitHubError,
    GitHubReply,
    parse_reply,
    unreachable,
)

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 - a URL, not a secret
REVOKE_URL = f"{API_URL}/credentials/revoke"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_OAUTH_HEADERS = {"Accept": "application/json", "User-Agent": USER_AGENT}
# RFC 8628: after slow_down, wait 5 seconds longer between polls than before.
_SLOW_DOWN_SECONDS = 5
_RESTART = "Start the sign-in again."
_WRONG_CLIENT_ID = (
    "GitHub does not know the Client ID in config.yaml.",
    "github.app.client_id must be your GitHub App's Client ID (it starts with Iv), not its App ID.",
)

# Saved as plain text only in the keyring's JSON; repr() and str() still show stars.
StoredSecret = Annotated[
    SecretStr, PlainSerializer(lambda s: s.get_secret_value(), return_type=str, when_used="json")
]


class StoredModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Renewal(StoredModel):
    access_expires_at: AwareDatetime
    # GitHub takes each refresh token once: a renewal makes the old pair stop working.
    refresh_token: StoredSecret
    refresh_expires_at: AwareDatetime


class TokenGrant(StoredModel):
    access_token: StoredSecret
    # None when the GitHub App was set to never expire its tokens.
    renewal: Renewal | None


class DeviceCode(GitHubReply):
    # device_code is a secret: only the server polls with it, and it is never logged.
    device_code: SecretStr
    user_code: str = Field(pattern=r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
    # Any other page could be a fake asking for the code, so only GitHub's own is accepted.
    verification_uri: Literal["https://github.com/login/device"]
    expires_in: int = Field(gt=0)
    interval: int = Field(gt=0)


class _TokenReply(GitHubReply):
    access_token: str = Field(min_length=1)
    token_type: Literal["bearer"]
    expires_in: int | None = Field(default=None, gt=0)
    refresh_token: str | None = Field(default=None, min_length=1)
    refresh_token_expires_in: int | None = Field(default=None, gt=0)


@dataclass
class PendingSignIn:
    """A sign-in waiting for the user on github.com. Memory only, because of the device code.
    Times are seconds since the epoch."""

    code: DeviceCode
    expires_at: float
    interval: float
    next_poll_at: float


@dataclass(frozen=True)
class Pending:
    """The user has not finished on github.com yet."""


@dataclass(frozen=True)
class SlowDown:
    interval: float


@dataclass(frozen=True)
class Granted:
    grant: TokenGrant


@dataclass(frozen=True)
class Failed:
    code: str
    message: str
    details: str


def parse_grant(body: dict[str, Any], now: float) -> TokenGrant:
    reply = parse_reply(_TokenReply, body)
    token = SecretStr(reply.access_token)
    lives, refresh, refresh_lives = (
        reply.expires_in,
        reply.refresh_token,
        reply.refresh_token_expires_in,
    )
    if lives is None and refresh is None and refresh_lives is None:
        return TokenGrant(access_token=token, renewal=None)
    if lives is None or refresh is None or refresh_lives is None:
        raise GitHubError(
            "GitHub's sign-in answer had only part of the expiry details.",
            details="It needs expires_in, refresh_token and refresh_token_expires_in together. "
            + _RESTART,
        )
    renewal = Renewal(
        access_expires_at=datetime.fromtimestamp(now + lives, UTC),
        refresh_token=SecretStr(refresh),
        refresh_expires_at=datetime.fromtimestamp(now + refresh_lives, UTC),
    )
    return TokenGrant(access_token=token, renewal=renewal)


def parse_poll_reply(
    body: dict[str, Any], interval: float, now: float, app: GitHubApp
) -> Pending | SlowDown | Granted | Failed:
    """GitHub answers a poll with HTTP 200 and an error field until the user is done."""
    error = body.get("error")
    if error is None:
        return Granted(parse_grant(body, now))
    if error == "authorization_pending":
        return Pending()
    if error == "slow_down":
        new_interval = body.get("interval")
        if isinstance(new_interval, int) and new_interval > 0:
            return SlowDown(float(new_interval))
        return SlowDown(interval + _SLOW_DOWN_SECONDS)
    return _failure(body, app)


def _failure(body: dict[str, Any], app: GitHubApp) -> Failed:
    """What a GitHub sign-in error means, and what to do about it."""
    code = str(body.get("error"))
    expired = ("The sign-in code ran out before it was used.", _RESTART)
    bad_code = ("GitHub did not accept the sign-in code.", _RESTART)
    known = {
        # GitHub's docs spell this one both ways.
        "expired_token": expired,
        "token_expired": expired,
        "access_denied": (
            "You pressed Cancel on GitHub, so you are not signed in.",
            "Start the sign-in again if you want to sign in.",
        ),
        "incorrect_client_credentials": _WRONG_CLIENT_ID,
        # Measured: an unknown Client ID gets HTTP 404 with {"error": "Not Found"}.
        "Not Found": _WRONG_CLIENT_ID,
        "incorrect_device_code": bad_code,
        "bad_verification_code": bad_code,
        "unsupported_grant_type": (
            "GitHub did not accept SetupPilot's sign-in request.",
            "This is a SetupPilot bug. Please report it.",
        ),
        "device_flow_disabled": (
            "Device sign-in is turned off for your GitHub App.",
            f"Tick Enable Device Flow at {app.settings_url}, then start the sign-in again.",
        ),
    }
    said = str(body.get("error_description") or body.get("message") or "")[:300]
    unknown = (
        f"GitHub refused the sign-in ({code[:60]}).",
        f"GitHub said: {said!r}. {_RESTART}" if said else _RESTART,
    )
    message, details = known.get(code, unknown)
    return Failed(code, message, details)


def start(http: httpx.Client, app: GitHubApp, now: float) -> PendingSignIn:
    """Ask GitHub for a code that the user types in at github.com/login/device."""
    response = _post(http, DEVICE_CODE_URL, {"client_id": app.client_id})
    body = _body(response)
    _raise_if_refused(response, body, app)
    code = parse_reply(DeviceCode, body)
    return PendingSignIn(
        code=code,
        expires_at=now + code.expires_in,
        interval=float(code.interval),
        next_poll_at=now + code.interval,
    )


def poll(
    http: httpx.Client, app: GitHubApp, pending: PendingSignIn, now: float
) -> Pending | Granted | Failed:
    """Ask GitHub whether the user is done, but never sooner than GitHub allows."""
    if now >= pending.expires_at:
        return _failure({"error": "expired_token"}, app)
    if now < pending.next_poll_at:
        return Pending()
    form = {
        "client_id": app.client_id,
        "device_code": pending.code.device_code.get_secret_value(),
        "grant_type": _DEVICE_GRANT,
    }
    # Set before the call, so a failed call also waits before the next one.
    pending.next_poll_at = now + pending.interval
    result = parse_poll_reply(_body(_post(http, TOKEN_URL, form)), pending.interval, now, app)
    if isinstance(result, SlowDown):
        pending.interval = result.interval
        pending.next_poll_at = now + result.interval
        return Pending()
    return result


def refresh(http: httpx.Client, app: GitHubApp, refresh_token: str, now: float) -> TokenGrant:
    """Swap the refresh token for a new pair. The device flow needs no client secret here."""
    form = {
        "client_id": app.client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    response = _post(http, TOKEN_URL, form)
    body = _body(response)
    error = body.get("error")
    if error == "bad_refresh_token":
        raise GitHubAuthEnded(
            "Your GitHub sign-in ended; sign in again.",
            details="GitHub no longer accepts its refresh token.",
        )
    _raise_if_refused(response, body, app)
    return parse_grant(body, now)


def revoke(http: httpx.Client, credentials: Sequence[str]) -> None:
    """Ask GitHub to cancel these tokens. Sent without Authorization: GitHub refuses it with one."""
    try:
        response = http.post(
            REVOKE_URL, json={"credentials": list(credentials)}, headers=API_HEADERS
        )
        is_revoked = response.status_code == 202
        status = f"HTTP {response.status_code}"
    except httpx.HTTPError as error:
        is_revoked, status = False, type(error).__name__
    if not is_revoked:
        raise GitHubError(
            "GitHub did not confirm that it cancelled your sign-in.",
            details=f"GitHub answered {status}. To cancel it yourself, remove SetupPilot under "
            "GitHub Settings > Applications > Authorized GitHub Apps.",
        )


def _raise_if_refused(response: httpx.Response, body: dict[str, Any], app: GitHubApp) -> None:
    if response.status_code == 200 and "error" not in body:
        return
    # Some refusals, such as GitHub's limit on new codes an hour, have a message but no error code.
    failed = _failure({"error": f"HTTP {response.status_code}"} | body, app)
    raise GitHubError(failed.message, details=failed.details)


def _post(http: httpx.Client, url: str, form: dict[str, str]) -> httpx.Response:
    # The form goes in the body, so the device code and tokens are never part of a URL.
    try:
        return http.post(url, data=form, headers=_OAUTH_HEADERS)
    except httpx.HTTPError as error:
        raise unreachable(error) from error


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as error:
        raise GitHubError(
            f"GitHub's sign-in answer could not be read (HTTP {response.status_code}).",
            details="It was not JSON. Try again in a few minutes.",
        ) from error
    if not isinstance(body, dict):
        raise GitHubError(
            "GitHub's sign-in answer was not what SetupPilot expected.",
            details=f"HTTP {response.status_code}, not a JSON object. Try again in a few minutes.",
        )
    return body
