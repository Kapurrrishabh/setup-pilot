"""Sign in with GitHub (device flow), and tie the sign-in to the browser that finished it.

The token never leaves the server. That browser gets a binding key once, keeps it in
localStorage and sends it as X-SetupPilot-Binding; only a hash of it is kept, with the token. A
cookie would not do: the local dev servers SetupPilot starts get this app's cookies too.
"""

import contextlib
import secrets
import threading
import time
from typing import Any

from flask import Flask, abort, jsonify, request

from setuppilot.errors import SetupPilotError
from setuppilot.github import device_flow
from setuppilot.github.api import FixedToken, GitHubAuthEnded, GitHubClient, GitHubError, RepoInfo
from setuppilot.github.device_flow import PendingSignIn, TokenGrant
from setuppilot.github.vault import GitHubCredentials, SignInNotRemoved, new_binding
from setuppilot.tools.clone import SIGN_IN_OFF, GitHubAccess, SignedIn
from setuppilot.tools.github_url import parse_github_url

BINDING_HEADER = "X-SetupPilot-Binding"
SIGN_IN_WARNING = (
    "Only type a code on github.com that this SetupPilot page showed you. Text from private repos "
    "you set up or ask about is sent to Groq, and saved in your database if Supabase is set up."
)

_SIGN_IN_HERE = (
    "This needs your GitHub sign-in, from this browser.",
    "Sign in with GitHub on this page. If you signed in from another browser, sign out and sign "
    "in again here.",
)


class SignInNeeded(SetupPilotError):
    """This needs the GitHub sign-in, sent from the browser that signed in."""


def is_bound(github: GitHubAccess, key: str | None) -> bool:
    return github.signed_in is not None and github.signed_in.vault.matches_binding(key)


def sent_key() -> str | None:
    return request.headers.get(BINDING_HEADER)


def require_binding(github: GitHubAccess) -> SignedIn:
    """The sign-in, when this request comes from the browser that signed in."""
    signed_in = _enabled(github)
    if not signed_in.vault.matches_binding(sent_key()):
        raise SignInNeeded(*_SIGN_IN_HERE)
    return signed_in


def require_user_id(github: GitHubAccess) -> int:
    """The signed-in GitHub user's id, when this request comes from the browser that signed in."""
    user = require_binding(github).vault.user
    if user is None:
        raise SignInNeeded(*_SIGN_IN_HERE)
    return user.id


def bound_user_id(github: GitHubAccess, key: str | None) -> int | None:
    """The signed-in GitHub user's id when key is the key of their browser, else None."""
    signed_in = github.signed_in
    if signed_in is None or not signed_in.vault.matches_binding(key):
        return None
    user = signed_in.vault.user
    return None if user is None else user.id


def _enabled(github: GitHubAccess) -> SignedIn:
    if github.signed_in is None:
        raise SignInNeeded(*SIGN_IN_OFF)
    return github.signed_in


def add_github_routes(app: Flask, github: GitHubAccess, max_repo_pages: int) -> None:
    lock = threading.Lock()
    # At most one sign-in waits on github.com; a new start replaces it. Memory only: it holds
    # the device code.
    waiting: dict[str, PendingSignIn] = {}

    @app.errorhandler(SignInNeeded)
    @app.errorhandler(GitHubAuthEnded)
    def sign_in_needed(error: SetupPilotError):
        return jsonify(error={"message": error.message, "details": error.details}), 401

    @app.get("/api/github/status")
    def github_status():
        """Booleans for every tab; the login name only for the browser that signed in."""
        signed_in = github.signed_in
        if signed_in is None:
            return jsonify(
                is_enabled=False,
                turn_on=" ".join(SIGN_IN_OFF),
                is_signed_in=False,
                is_this_browser=False,
                login=None,
                notice=None,
                install_url=None,
            )
        user = signed_in.vault.user
        is_this_browser = is_bound(github, sent_key())
        return jsonify(
            is_enabled=True,
            turn_on=None,
            is_signed_in=user is not None,
            is_this_browser=is_this_browser,
            login=user.login if user is not None and is_this_browser else None,
            notice=signed_in.vault.notice,
            install_url=signed_in.app.install_url,
        )

    @app.post("/api/github/signin")
    def start_sign_in():
        signed_in = _enabled(github)
        # Signing in over a sign-in would leave the old tokens working (and, on Windows, in the
        # keyring's copy), so the old one is signed out first.
        if signed_in.vault.user is not None:
            abort(409, "You are already signed in to GitHub. Sign out first, then sign in again.")
        pending = device_flow.start(signed_in.http, signed_in.app, signed_in.clock())
        pending_id = secrets.token_urlsafe(32)
        with lock:
            waiting.clear()
            waiting[pending_id] = pending
        code = pending.code
        return jsonify(
            pending_id=pending_id,
            user_code=code.user_code,
            verification_uri=code.verification_uri,
            expires_in=code.expires_in,
            warning=SIGN_IN_WARNING,
        )

    @app.post("/api/github/signin/poll")
    def poll_sign_in():
        """Safe to call every few seconds: GitHub is asked only as often as it allows."""
        signed_in = _enabled(github)
        body = request.get_json(silent=True)
        sent = body.get("pending_id") if isinstance(body, dict) else None
        # One lock for the whole poll, so two polls cannot both take the same grant.
        with lock:
            pending_id, pending = next(iter(waiting.items()), (None, None))
            if (
                pending is None
                or not isinstance(sent, str)
                or not secrets.compare_digest(sent.encode(), str(pending_id).encode())
            ):
                return jsonify(
                    state="failed",
                    message="This sign-in is no longer waiting.",
                    details="Start the sign-in again.",
                )
            result = device_flow.poll(signed_in.http, signed_in.app, pending, signed_in.clock())
            if isinstance(result, device_flow.Pending):
                return jsonify(state="pending")
            waiting.clear()
            if isinstance(result, device_flow.Failed):
                return jsonify(state="failed", message=result.message, details=result.details)
            login, key = _keep(signed_in, result.grant)
        # The only time the key leaves the server: the page keeps it, the server keeps its hash.
        return jsonify(state="done", login=login, binding_key=key)

    @app.post("/api/github/signout")
    def sign_out():
        """Needs no binding: forgetting the token is always safe, and a browser that lost its key
        must still be able to end the sign-in."""
        signed_in = _enabled(github)
        with lock:
            waiting.clear()
        try:
            credentials = signed_in.vault.clear()
        except SignInNotRemoved as error:
            # Cancelled at GitHub all the same, so the copy the keyring kept no longer works.
            at_github = (
                " Also remove SetupPilot under GitHub Settings > Applications > Authorized "
                "GitHub Apps, so GitHub cancels the sign-in."
            )
            if error.credentials is not None:
                with contextlib.suppress(GitHubError):
                    device_flow.revoke(signed_in.http, _tokens(error.credentials.grant))
                    at_github = " GitHub has cancelled the sign-in, so that copy no longer works."
            raise GitHubError(error.message, details=f"{error.details}{at_github}") from error
        if credentials is None:
            return jsonify(warning=None)
        try:
            device_flow.revoke(signed_in.http, _tokens(credentials.grant))
        except GitHubError as error:
            # Signed out here all the same; say how to finish it at GitHub.
            return jsonify(warning={"message": error.message, "details": error.details})
        return jsonify(warning=None)

    @app.get("/api/github/repos")
    def github_repos():
        """The repos the user installed the GitHub App on. install_url adds or removes repos."""
        signed_in = require_binding(github)
        installations = signed_in.client.installations(max_repo_pages)
        listings = [
            signed_in.client.installation_repos(installation.id, max_repo_pages)
            for installation in installations.items
        ]
        return jsonify(
            repos=[_repo_json(repo) for listing in listings for repo in listing.items],
            is_complete=installations.is_complete and all(x.is_complete for x in listings),
            install_url=signed_in.app.install_url,
        )


def _keep(signed_in: SignedIn, grant: TokenGrant) -> tuple[str, str]:
    """Save a finished sign-in. Returns the login name and the key for the browser."""
    try:
        reader = GitHubClient(
            signed_in.http, FixedToken(grant.access_token), signed_in.clock, time.sleep
        )
        user = reader.user()
        key, digest = new_binding()
        signed_in.vault.save(GitHubCredentials(grant=grant, user=user, binding_hash=digest))
    except GitHubError as error:
        # The new tokens work at GitHub until cancelled, so they are cancelled before the error.
        try:
            device_flow.revoke(signed_in.http, _tokens(grant))
        except GitHubError as not_revoked:
            raise GitHubError(
                error.message,
                details=f"{error.details} {not_revoked.message} {not_revoked.details}",
            ) from error
        raise
    return user.login, key


def _tokens(grant: TokenGrant) -> list[str]:
    renewal = grant.renewal
    refresh = [] if renewal is None else [renewal.refresh_token.get_secret_value()]
    return [grant.access_token.get_secret_value(), *refresh]


def _repo_json(repo: RepoInfo) -> dict[str, Any]:
    return {
        "full_name": repo.full_name,
        # Built here from the name, never copied from GitHub's links.
        "url": parse_github_url(f"https://github.com/{repo.full_name}").url,
        "description": repo.description,
        "visibility": repo.visibility,
        "language": repo.language,
        "default_branch": repo.default_branch,
        "is_archived": repo.is_archived,
        "is_fork": repo.is_fork,
        "pushed_at": repo.pushed_at.isoformat() if repo.pushed_at else None,
    }
