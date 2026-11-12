"""Keep the GitHub sign-in in the system keyring, renew it before it runs out, and tie it to the
browser that signed in."""

import hashlib
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import keyring
from keyring.backend import KeyringBackend
from keyring.backends.chainer import ChainerBackend
from keyring.errors import KeyringError, KeyringLocked, PasswordDeleteError
from pydantic import ValidationError

from setuppilot.github.api import GitHubAuthEnded, GitHubError, GitHubUser
from setuppilot.github.device_flow import StoredModel, TokenGrant

KEYRING_SERVICE = "setuppilot"
KEYRING_USER = "github"
# PROVISIONAL: renew this long before the 8-hour token ends, so a token handed out now still
# works for the clone that asked for it. Check it against how long real clones take.
_RENEW_MARGIN = timedelta(minutes=5)
# Only the operating system's own stores: never keyrings.alt, which can keep plain files.
_SYSTEM_BACKENDS = frozenset(
    {
        "keyring.backends.macOS",
        "keyring.backends.Windows",
        "keyring.backends.SecretService",
        "keyring.backends.libsecret",
        "keyring.backends.kwallet",
    }
)
_MEMORY_ONLY = "Sign-in is kept in memory only. Sign in again after a restart, because "
NO_KEYRING_NOTICE = f"{_MEMORY_ONLY}no system keyring was found."
LOCKED_NOTICE = f"{_MEMORY_ONLY}the system keyring is locked. Unlock it and restart to keep it."
_BINDING_BYTES = 32

Refresher = Callable[[str, float], TokenGrant]


class GitHubCredentials(StoredModel):
    grant: TokenGrant
    user: GitHubUser
    # Only a hash of the browser's key is kept, so the saved entry cannot be used as the key.
    binding_hash: str


def new_binding() -> tuple[str, str]:
    """A key for the browser that signed in (sent to it once), and the hash that is saved."""
    key = secrets.token_urlsafe(_BINDING_BYTES)
    return key, binding_hash(key)


def binding_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def pick_keyring(chosen: KeyringBackend) -> KeyringBackend | None:
    """The best system keyring among what keyring chose, or None when there is none."""
    candidates = chosen.backends if isinstance(chosen, ChainerBackend) else [chosen]
    return next((b for b in candidates if type(b).__module__ in _SYSTEM_BACKENDS), None)


def system_keyring() -> KeyringBackend | None:
    return pick_keyring(keyring.get_keyring())


def _call_keyring[**P, T](call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    # The Windows and libsecret backends let the OS's own errors through. They are keyring
    # failures all the same, so they get the same memory-only mode and clear messages.
    try:
        return call(*args, **kwargs)
    except KeyringError:
        raise
    except Exception as error:
        raise KeyringError(f"{type(error).__name__}: {error}") from error


class TokenVault:
    """The one place the GitHub sign-in lives. Its tokens leave only through access_token()."""

    def __init__(
        self,
        backend: KeyringBackend | None,
        refresh: Refresher,
        clock: Callable[[], float],
        credentials: GitHubCredentials | None,
        notice: str | None,
    ) -> None:
        self._backend = backend
        self._refresh = refresh
        self._clock = clock
        self._credentials = credentials
        self._notice = notice
        self._lock = threading.Lock()

    @classmethod
    def load(
        cls, backend: KeyringBackend | None, refresh: Refresher, clock: Callable[[], float]
    ) -> "TokenVault":
        """backend is None when there is no system keyring; then sign-in lives in memory only."""
        if backend is None:
            return cls(None, refresh, clock, None, NO_KEYRING_NOTICE)
        try:
            saved = _call_keyring(backend.get_password, KEYRING_SERVICE, KEYRING_USER)
        except KeyringLocked:
            return cls(None, refresh, clock, None, LOCKED_NOTICE)
        except KeyringError as error:
            notice = f"{_MEMORY_ONLY}the system keyring failed ({type(error).__name__}: {error})."
            return cls(None, refresh, clock, None, notice)
        if saved is None:
            return cls(backend, refresh, clock, None, None)
        try:
            return cls(backend, refresh, clock, GitHubCredentials.model_validate_json(saved), None)
        except ValidationError:
            notice = "The saved GitHub sign-in could not be read, so it was removed. Sign in again."
            vault = cls(backend, refresh, clock, None, notice)
            vault._forget()
            return vault

    @property
    def storage(self) -> Literal["keyring", "memory"]:
        return "memory" if self._backend is None else "keyring"

    @property
    def notice(self) -> str | None:
        """Something the user must be told, such as memory-only mode."""
        return self._notice

    @property
    def user(self) -> GitHubUser | None:
        credentials = self._credentials
        return None if credentials is None else credentials.user

    def save(self, credentials: GitHubCredentials) -> None:
        with self._lock:
            self._write(credentials)

    def access_token(self) -> str:
        """A working token, renewed first when it has under 5 minutes left."""
        with self._lock:
            credentials = self._credentials
            if credentials is None:
                raise GitHubAuthEnded(
                    "You are not signed in to GitHub.",
                    details="Sign in with GitHub, then try again.",
                )
            renewal = credentials.grant.renewal
            now = datetime.fromtimestamp(self._clock(), UTC)
            if renewal is None or renewal.access_expires_at - now > _RENEW_MARGIN:
                return credentials.grant.access_token.get_secret_value()
            if renewal.refresh_expires_at <= now:
                self._forget()
                raise GitHubAuthEnded(
                    "Your GitHub sign-in ended; sign in again.",
                    details="It was not renewed for 6 months, so GitHub let it run out.",
                )
            try:
                grant = self._refresh(renewal.refresh_token.get_secret_value(), self._clock())
            except GitHubAuthEnded:
                self._forget()
                raise
            try:
                self._write(credentials.model_copy(update={"grant": grant}))
            except GitHubError:
                # GitHub has already stopped the old pair, so nothing usable is left.
                self._credentials = None
                raise
            return grant.access_token.get_secret_value()

    def sign_in_ended(self, access_token: str) -> None:
        """GitHub refused this token: forget the sign-in, unless it was renewed since."""
        with self._lock:
            credentials = self._credentials
            if credentials and credentials.grant.access_token.get_secret_value() == access_token:
                self._forget()

    def matches_binding(self, key: str | None) -> bool:
        """True only for the browser that signed in: it holds the key whose hash is saved."""
        credentials = self._credentials
        if credentials is None or not key:
            return False
        return secrets.compare_digest(binding_hash(key), credentials.binding_hash)

    def clear(self) -> GitHubCredentials | None:
        """Forget the sign-in here and in the keyring. Returns it, so its tokens can be revoked."""
        with self._lock:
            credentials = self._credentials
            self._forget()
            return credentials

    def _write(self, credentials: GitHubCredentials) -> None:
        # Keyring first: a sign-in that cannot be saved there is not used at all.
        if self._backend is not None:
            try:
                saved = credentials.model_dump_json()
                _call_keyring(self._backend.set_password, KEYRING_SERVICE, KEYRING_USER, saved)
            except KeyringError as error:
                raise GitHubError(
                    "Could not save your GitHub sign-in in the system keyring.",
                    details=f"{type(error).__name__}: {error}. Unlock it, then sign in again.",
                ) from error
        self._credentials = credentials

    def _forget(self) -> None:
        self._credentials = None
        if self._backend is None:
            return
        try:
            try:
                _call_keyring(self._backend.delete_password, KEYRING_SERVICE, KEYRING_USER)
            except PasswordDeleteError:
                # Deleting an entry that is not there fails too; only one left behind is a problem.
                left = _call_keyring(self._backend.get_password, KEYRING_SERVICE, KEYRING_USER)
                if left is not None:
                    raise
        except KeyringError as error:
            raise GitHubError(
                "Could not remove your GitHub sign-in from the system keyring.",
                details=f"{type(error).__name__}: {error}. Delete the '{KEYRING_SERVICE}' entry "
                "yourself: in Keychain Access on macOS, or in Credential Manager on Windows, with "
                f"its copy '{KEYRING_USER}@{KEYRING_SERVICE}'.",
            ) from error
