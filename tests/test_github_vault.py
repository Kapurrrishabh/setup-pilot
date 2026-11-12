import threading
import time

import pytest
from keyring.backends import fail
from keyring.errors import KeyringLocked, PasswordDeleteError, PasswordSetError

from setuppilot.github.api import GitHubAuthEnded, GitHubError, GitHubUser
from setuppilot.github.device_flow import TokenGrant, parse_grant
from setuppilot.github.vault import (
    KEYRING_SERVICE,
    KEYRING_USER,
    LOCKED_NOTICE,
    NO_KEYRING_NOTICE,
    GitHubCredentials,
    TokenVault,
    binding_hash,
    new_binding,
    pick_keyring,
)
from tests.fakes import FakeClock
from tests.github_fakes import MemoryKeyring

EIGHT_HOURS = 28800
SIX_MONTHS = 15897600
# Windows keeps at most 2,560 bytes per credential, as UTF-16 (2 bytes a character).
WINDOWS_CREDENTIAL_CHARS = 1280


def grant(token: str, refresh: str, now: float, lives: int = EIGHT_HOURS) -> TokenGrant:
    reply = {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": lives,
        "refresh_token": refresh,
        "refresh_token_expires_in": SIX_MONTHS,
    }
    return parse_grant(reply, now)


def credentials(grant: TokenGrant, key: str = "browser-key") -> GitHubCredentials:
    user = GitHubUser(login="octocat", id=583231)
    return GitHubCredentials(grant=grant, user=user, binding_hash=binding_hash(key))


class FakeRefresh:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.error = error

    def __call__(self, refresh_token: str, now: float) -> TokenGrant:
        self.calls.append(refresh_token)
        if self.error is not None:
            raise self.error
        return grant("ghu_new", "ghr_new", now)


def signed_in(lives: int = EIGHT_HOURS, refresh: FakeRefresh | None = None):
    clock, keyring, refresh = FakeClock(), MemoryKeyring(), refresh or FakeRefresh()
    vault = TokenVault.load(keyring, refresh, clock)
    vault.save(credentials(grant("ghu_old", "ghr_old", clock(), lives)))
    return vault, keyring, refresh, clock


def saved(keyring: MemoryKeyring) -> str | None:
    return keyring.items.get((KEYRING_SERVICE, KEYRING_USER))


class CredentialManagerError(Exception):
    """Like pywintypes.error: a plain Exception that keyring's Windows backend lets through."""


def logon_session_gone() -> CredentialManagerError:
    # What Credential Manager gives in an OpenSSH key-auth session or under a service account.
    return CredentialManagerError(1312, "CredRead", "A specified logon session does not exist.")


def test_without_a_system_keyring_sign_in_is_kept_in_memory_and_says_so():
    vault = TokenVault.load(None, FakeRefresh(), FakeClock())
    vault.save(credentials(grant("ghu_old", "ghr_old", FakeClock()())))
    assert (vault.storage, vault.notice, vault.access_token()) == (
        "memory",
        NO_KEYRING_NOTICE,
        "ghu_old",
    )


def test_a_locked_keyring_says_to_unlock_it():
    vault = TokenVault.load(MemoryKeyring(get_error=KeyringLocked()), FakeRefresh(), FakeClock())
    assert (vault.storage, vault.notice) == ("memory", LOCKED_NOTICE)


def test_a_keyring_failing_with_an_os_error_gives_memory_only_mode_and_says_why():
    keyring = MemoryKeyring(get_error=logon_session_gone())
    vault = TokenVault.load(keyring, FakeRefresh(), FakeClock())
    assert (vault.storage, "1312" in (vault.notice or "")) == ("memory", True)


def test_the_sign_in_survives_a_restart_through_the_keyring():
    _, keyring, refresh, clock = signed_in()
    restarted = TokenVault.load(keyring, refresh, clock)
    assert (restarted.access_token(), restarted.user) == (
        "ghu_old",
        GitHubUser(login="octocat", id=583231),
    )


def test_the_saved_sign_in_fits_in_a_windows_credential():
    keyring = MemoryKeyring()
    vault = TokenVault.load(keyring, FakeRefresh(), FakeClock())
    # Real sizes: ghu_ tokens are 40 characters, ghr_ tokens 80, logins at most 39.
    long_grant = grant("ghu_" + "a" * 36, "ghr_" + "b" * 76, FakeClock()())
    user = GitHubUser(login="x" * 39, id=2**40)
    _, digest = new_binding()
    vault.save(GitHubCredentials(grant=long_grant, user=user, binding_hash=digest))
    assert len(saved(keyring) or "") < WINDOWS_CREDENTIAL_CHARS


def test_a_fresh_token_is_not_renewed():
    vault, _, refresh, _ = signed_in()
    vault.access_token()
    assert refresh.calls == []


def test_a_token_close_to_running_out_is_renewed_and_saved_before_use():
    vault, keyring, refresh, clock = signed_in(lives=4 * 60)
    token = vault.access_token()
    restarted = TokenVault.load(keyring, refresh, clock)
    assert (token, refresh.calls, restarted.access_token()) == ("ghu_new", ["ghr_old"], "ghu_new")


def test_a_refused_refresh_token_deletes_the_sign_in():
    ended = GitHubAuthEnded("ended", details="bad_refresh_token")
    vault, keyring, _, _ = signed_in(lives=60, refresh=FakeRefresh(error=ended))
    with pytest.raises(GitHubAuthEnded):
        vault.access_token()
    assert (vault.user, saved(keyring)) == (None, None)


def test_a_refresh_token_that_ran_out_ends_the_sign_in_without_asking_github():
    vault, keyring, refresh, clock = signed_in(lives=60)
    clock.sleep(SIX_MONTHS)
    with pytest.raises(GitHubAuthEnded):
        vault.access_token()
    assert (refresh.calls, saved(keyring)) == ([], None)


@pytest.mark.parametrize("error", [PasswordSetError("keychain is read-only"), logon_session_gone()])
def test_a_renewal_that_cannot_be_saved_leaves_no_sign_in(error):
    vault, keyring, _, _ = signed_in(lives=60)
    keyring.set_error = error
    with pytest.raises(GitHubError, match="Could not save"):
        vault.access_token()
    assert vault.user is None


def test_two_requests_renewing_at_once_ask_github_once():
    clock, keyring = FakeClock(), MemoryKeyring()
    is_inside, may_finish = threading.Event(), threading.Event()
    calls: list[str] = []

    def slow_refresh(refresh_token: str, now: float) -> TokenGrant:
        calls.append(refresh_token)
        is_inside.set()
        may_finish.wait(5)
        return grant("ghu_new", "ghr_new", now)

    vault = TokenVault.load(keyring, slow_refresh, clock)
    vault.save(credentials(grant("ghu_old", "ghr_old", clock(), lives=60)))
    tokens: list[str] = []
    threads = [threading.Thread(target=lambda: tokens.append(vault.access_token())) for _ in "ab"]
    threads[0].start()
    is_inside.wait(5)
    threads[1].start()
    time.sleep(0.1)  # lets the second request reach the vault while the first is renewing
    may_finish.set()
    for thread in threads:
        thread.join(5)
    assert (calls, tokens) == (["ghr_old"], ["ghu_new", "ghu_new"])


@pytest.mark.parametrize(("key", "is_match"), [("browser-key", True), ("other", False)])
def test_only_the_browser_that_signed_in_matches(key, is_match):
    vault, _, _, _ = signed_in()
    assert vault.matches_binding(key) is is_match


@pytest.mark.parametrize("key", [None, ""])
def test_no_binding_key_never_matches(key):
    vault, _, _, _ = signed_in()
    assert vault.matches_binding(key) is False


def test_the_key_from_an_old_sign_in_does_not_match_a_new_one():
    old_key, _ = new_binding()
    vault, _, _, clock = signed_in()
    new_key, new_hash = new_binding()
    user = GitHubUser(login="octocat", id=583231)
    fresh = grant("ghu_2", "ghr_2", clock())
    vault.save(GitHubCredentials(grant=fresh, user=user, binding_hash=new_hash))
    assert (vault.matches_binding(old_key), vault.matches_binding(new_key)) == (False, True)


def test_a_refused_token_ends_the_sign_in():
    vault, keyring, _, _ = signed_in()
    vault.sign_in_ended("ghu_old")
    assert (vault.user, saved(keyring)) == (None, None)


def test_a_refused_old_token_does_not_end_a_renewed_sign_in():
    vault, _, _, _ = signed_in(lives=60)
    vault.access_token()
    vault.sign_in_ended("ghu_old")
    assert vault.access_token() == "ghu_new"


def test_signing_out_returns_the_tokens_to_revoke_and_removes_them():
    vault, keyring, _, _ = signed_in()
    removed = vault.clear()
    assert removed is not None
    assert (removed.grant.access_token.get_secret_value(), saved(keyring)) == ("ghu_old", None)


@pytest.mark.parametrize(
    ("delete_error", "get_error"),
    [(logon_session_gone(), None), (PasswordDeleteError("failed"), logon_session_gone())],
)
def test_a_sign_in_the_keyring_cannot_remove_says_how_to_remove_both_entries(
    delete_error, get_error
):
    vault, keyring, _, _ = signed_in()
    keyring.delete_error, keyring.get_error = delete_error, get_error
    with pytest.raises(GitHubError, match="Could not remove") as raised:
        vault.clear()
    assert "'setuppilot'" in raised.value.details and "'github@setuppilot'" in raised.value.details


def test_a_saved_sign_in_that_cannot_be_read_is_removed_with_a_notice():
    keyring = MemoryKeyring()
    keyring.items[(KEYRING_SERVICE, KEYRING_USER)] = '{"old": "format"}'
    vault = TokenVault.load(keyring, FakeRefresh(), FakeClock())
    assert (vault.user, saved(keyring), vault.notice is not None) == (None, None, True)


def test_signed_out_vault_asks_to_sign_in():
    vault = TokenVault.load(MemoryKeyring(), FakeRefresh(), FakeClock())
    with pytest.raises(GitHubAuthEnded, match="not signed in"):
        vault.access_token()


def system_backend() -> object:
    backend_class = type("Keyring", (MemoryKeyring,), {"__module__": "keyring.backends.macOS"})
    return backend_class()


def test_the_system_keyring_is_used():
    backend = system_backend()
    assert pick_keyring(backend) is backend


@pytest.mark.parametrize("module", ["keyrings.alt.file", "keyring.backends.null"])
def test_a_keyring_that_is_not_the_systems_is_never_used(module):
    backend = type("Keyring", (MemoryKeyring,), {"__module__": module})()
    assert pick_keyring(backend) is None


def test_no_keyring_found_means_memory_only():
    assert pick_keyring(fail.Keyring()) is None
