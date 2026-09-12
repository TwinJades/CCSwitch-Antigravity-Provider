"""Local-only administrator password persistence and recovery primitives.

No password, recovery challenge, session or credential is persisted here.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from hmac import compare_digest
from threading import Lock
from time import monotonic
from typing import Protocol

from sqlalchemy import CheckConstraint, Column, DateTime, Integer, MetaData, String, Table, insert, select, update
from sqlalchemy.engine import Engine

from ..security import hash_password
from .auth import AdminSessionManager


_metadata = MetaData()
admin_security_state = Table(
    "admin_security_state",
    _metadata,
    Column("singleton_id", Integer, primary_key=True),
    Column("password_verifier", String(1024), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("singleton_id = 1", name="ck_admin_security_state_singleton"),
)


class AdminPasswordStore:
    """Transaction-safe access to the single irreversible admin verifier."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def current_verifier(self) -> str | None:
        with self._engine.connect() as connection:
            return connection.execute(
                select(admin_security_state.c.password_verifier).where(
                    admin_security_state.c.singleton_id == 1
                )
            ).scalar_one_or_none()

    def initialize_if_missing(self, verifier: str) -> str:
        """Persist *verifier* once, returning the verifier currently in force."""

        _validate_verifier(verifier)
        try:
            with self._engine.begin() as connection:
                current = connection.execute(
                    select(admin_security_state.c.password_verifier).where(
                        admin_security_state.c.singleton_id == 1
                    )
                ).scalar_one_or_none()
                if current is not None:
                    return current
                connection.execute(
                    insert(admin_security_state).values(
                        singleton_id=1,
                        password_verifier=verifier,
                        updated_at=datetime.now(UTC),
                    )
                )
                return verifier
        except Exception:
            raise RuntimeError("Administrator password verifier could not be initialized") from None

    def replace(self, verifier: str) -> None:
        """Atomically replace the singleton verifier without retaining the old one."""

        _validate_verifier(verifier)
        try:
            with self._engine.begin() as connection:
                result = connection.execute(
                    update(admin_security_state)
                    .where(admin_security_state.c.singleton_id == 1)
                    .values(password_verifier=verifier, updated_at=datetime.now(UTC))
                )
                if result.rowcount != 1:
                    raise RuntimeError("Administrator password verifier is not initialized")
        except Exception:
            raise RuntimeError("Administrator password verifier could not be replaced") from None


class RecoveryConfirmation(Protocol):
    """An explicit local desktop confirmation boundary."""

    def confirm(self) -> bool: ...


class WindowsMessageBoxConfirmation:
    """Confirm recovery at the active Windows desktop; reject elsewhere."""

    def confirm(self) -> bool:
        if os.name != "nt":
            return False
        # MB_YESNO | MB_ICONWARNING; never use MB_SERVICE_NOTIFICATION.
        result = ctypes.windll.user32.MessageBoxW(
            None,
            "Allow a one-time administrator password recovery challenge?",
            "AI Provider Gateway password recovery",
            0x00000004 | 0x00000030,
        )
        return result == 6  # IDYES


class PasswordRecoveryManager:
    """Issue local-confirmed, short-lived, single-use password recovery tokens."""

    def __init__(
        self,
        store: AdminPasswordStore,
        sessions: AdminSessionManager,
        confirmer: RecoveryConfirmation,
        *,
        ttl_seconds: int = 120,
        max_challenges: int = 4,
        clock: Callable[[], float] = monotonic,
        hasher: Callable[[str], str] = hash_password,
    ) -> None:
        if ttl_seconds <= 0 or max_challenges <= 0:
            raise ValueError("Recovery limits must be positive.")
        self._store = store
        self._sessions = sessions
        self._confirmer = confirmer
        self._ttl_seconds = ttl_seconds
        self._max_challenges = max_challenges
        self._clock = clock
        self._hasher = hasher
        self._challenges: dict[str, float] = {}
        self._lock = Lock()
        # A native dialog is an operator-facing resource.  Keep at most one
        # outstanding confirmation so concurrent recovery requests cannot
        # flood the active desktop with message boxes.
        self._confirmation_gate = Lock()

    async def start(self) -> str | None:
        """Ask the active local desktop before creating an in-memory challenge."""

        if not self._confirmation_gate.acquire(blocking=False):
            return None
        try:
            try:
                confirmed = await asyncio.to_thread(self._confirmer.confirm)
            except Exception:
                return None
            if not confirmed:
                return None
            token = secrets.token_urlsafe(32)
            now = self._clock()
            with self._lock:
                self._remove_expired(now)
                if len(self._challenges) >= self._max_challenges:
                    return None
                self._challenges[token] = now + self._ttl_seconds
            return token
        finally:
            self._confirmation_gate.release()

    def complete(self, token: str, new_password: str) -> bool:
        """Consume a challenge once and replace the verifier on durable success."""

        if not _valid_password(new_password):
            return False
        if not isinstance(token, str) or not token:
            return False
        now = self._clock()
        with self._lock:
            self._remove_expired(now)
            matched_token = next(
                (candidate for candidate in self._challenges if compare_digest(candidate, token)),
                None,
            )
            if matched_token is None:
                return False
            # Consume before hashing/storage: failures must not preserve an old
            # password as a reusable recovery path.
            self._challenges.pop(matched_token, None)
            try:
                verifier = self._hasher(new_password)
                _validate_verifier(verifier)
                self._store.replace(verifier)
            except Exception:
                return False
            self._sessions.replace_password_verifier(verifier)
            return True

    def _remove_expired(self, now: float) -> None:
        expired = [token for token, expiry in self._challenges.items() if expiry <= now]
        for token in expired:
            self._challenges.pop(token, None)


def _validate_verifier(verifier: str) -> None:
    if not isinstance(verifier, str) or not verifier or len(verifier) > 1024:
        raise ValueError("Administrator password verifier is invalid.")


def _valid_password(password: str) -> bool:
    return isinstance(password, str) and 12 <= len(password) <= 1024
