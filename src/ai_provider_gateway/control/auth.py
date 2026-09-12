"""Loopback-bound administrator sessions for Supervisor control routes."""

from __future__ import annotations

from collections.abc import Callable
from ipaddress import ip_address
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from fastapi import Request

from ..security import generate_secret, verify_password


PasswordVerifier = Callable[[str, str], bool]


class AdminSessionManager:
    """Issue bounded in-memory sessions after administrator password checks."""

    cookie_name = "aipg_admin_session"

    def __init__(
        self,
        encoded_password_verifier: str,
        *,
        password_verifier: PasswordVerifier = verify_password,
        ttl_seconds: int = 8 * 60 * 60,
        max_sessions: int = 16,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not encoded_password_verifier:
            raise ValueError("Administrator password verifier is required.")
        if ttl_seconds <= 0 or max_sessions <= 0:
            raise ValueError("Session limits must be positive.")
        self._encoded_password_verifier = encoded_password_verifier
        self._password_verifier = password_verifier
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._sessions: dict[str, float] = {}
        self._lock = Lock()

    @property
    def max_age_seconds(self) -> int:
        return self._ttl_seconds

    def login(self, password: str) -> str | None:
        with self._lock:
            # Verify and issue under one lock so password replacement cannot
            # leave a session authenticated with a stale verifier.
            if not self._password_verifier(self._encoded_password_verifier, password):
                return None
            token = generate_secret()
            now = self._clock()
            self._remove_expired(now)
            while len(self._sessions) >= self._max_sessions:
                oldest = min(self._sessions, key=self._sessions.__getitem__)
                self._sessions.pop(oldest, None)
            self._sessions[token] = now + self._ttl_seconds
        return token

    def replace_password_verifier(self, encoded: str) -> None:
        """Install a verifier and invalidate every existing session."""

        if not isinstance(encoded, str) or not encoded:
            raise ValueError("Administrator password verifier is required.")
        with self._lock:
            self._encoded_password_verifier = encoded
            self._sessions.clear()

    def authorize(self, request: Request) -> bool:
        if not self.request_boundary_allows(request):
            return False
        token = request.cookies.get(self.cookie_name)
        if not token:
            return False
        now = self._clock()
        with self._lock:
            self._remove_expired(now)
            return token in self._sessions

    def logout(self, request: Request) -> None:
        token = request.cookies.get(self.cookie_name)
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    @staticmethod
    def request_boundary_allows(request: Request) -> bool:
        """Require a loopback peer/Host and same-origin browser mutations."""

        client_host = request.client.host if request.client is not None else ""
        if not _is_loopback(client_host):
            return False
        host = _hostname_from_authority(request.headers.get("host", ""))
        if not _is_loopback(host):
            return False
        if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
            return True
        origin = request.headers.get("origin", "")
        if not origin:
            return False
        parsed_origin = urlsplit(origin)
        if parsed_origin.scheme != request.url.scheme:
            return False
        if not _is_loopback(parsed_origin.hostname or ""):
            return False
        return parsed_origin.netloc.lower() == request.headers.get("host", "").lower()

    def _remove_expired(self, now: float) -> None:
        expired = [token for token, expiry in self._sessions.items() if expiry <= now]
        for token in expired:
            self._sessions.pop(token, None)


def authorize_local_control(request: Request) -> bool:
    """Bypass password sessions while retaining the complete loopback boundary."""

    return AdminSessionManager.request_boundary_allows(request)


def _hostname_from_authority(authority: str) -> str:
    try:
        return urlsplit(f"//{authority}").hostname or ""
    except ValueError:
        return ""


def _is_loopback(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False
