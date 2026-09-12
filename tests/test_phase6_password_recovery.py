import asyncio
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import create_engine

from ai_provider_gateway.control.auth import AdminSessionManager
from ai_provider_gateway.control.password_recovery import (
    AdminPasswordStore,
    PasswordRecoveryManager,
    WindowsMessageBoxConfirmation,
    _metadata,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Confirmation:
    def __init__(self, result=True):
        self.result = result
        self.calls = 0

    def confirm(self):
        self.calls += 1
        return self.result


def _components(tmp_path, *, clock=lambda: 100.0, confirmation=True):
    engine = create_engine(f"sqlite:///{tmp_path / 'security.db'}")
    _metadata.create_all(engine)
    store = AdminPasswordStore(engine)
    store.initialize_if_missing("old-verifier")
    sessions = AdminSessionManager(
        "old-verifier", password_verifier=lambda verifier, password: verifier == f"{password}-verifier"
    )
    return store, sessions, PasswordRecoveryManager(
        store, sessions, _Confirmation(confirmation), clock=clock, hasher=lambda password: f"{password}-verifier"
    )


def test_migration_persists_only_verifier_and_timestamp(tmp_path):
    path = tmp_path / "phase6.db"
    env = os.environ.copy()
    env["AIPG_DATABASE_URL"] = f"sqlite:///{path.as_posix()}"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=PROJECT_ROOT, env=env, check=True)
    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("pragma table_info(admin_security_state)")}
    assert columns == {"singleton_id", "password_verifier", "updated_at"}
    connection.close()


def test_store_initializes_once_and_replaces_transactionally(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'store.db'}")
    _metadata.create_all(engine)
    store = AdminPasswordStore(engine)
    assert store.current_verifier() is None
    assert store.initialize_if_missing("first") == "first"
    assert store.initialize_if_missing("ignored") == "first"
    store.replace("second")
    assert store.current_verifier() == "second"


def test_recovery_requires_confirmation_expires_and_is_single_use(tmp_path):
    now = [100.0]
    store, sessions, manager = _components(tmp_path, clock=lambda: now[0])
    assert asyncio.run(manager.start()) is not None
    denied = PasswordRecoveryManager(store, sessions, _Confirmation(False), clock=lambda: now[0])
    assert asyncio.run(denied.start()) is None
    token = asyncio.run(manager.start())
    now[0] += 121
    assert manager.complete(token, "new-password-123") is False
    token = asyncio.run(manager.start())
    assert manager.complete(token, "new-password-123") is True
    assert manager.complete(token, "different-password-456") is False


def test_recovery_password_policy_and_failed_store_do_not_change_sessions(tmp_path):
    store, sessions, manager = _components(tmp_path)
    token = asyncio.run(manager.start())
    assert manager.complete(token, "too-short") is False
    assert sessions.login("old") is not None

    class _FailingStore:
        def replace(self, verifier):
            raise OSError("storage unavailable")

    failed = PasswordRecoveryManager(
        _FailingStore(), sessions, _Confirmation(True), hasher=lambda _: "new-verifier"
    )
    token = asyncio.run(failed.start())
    assert failed.complete(token, "valid-password-123") is False
    assert sessions.login("old") is not None


def test_recovery_replaces_verifier_invalidates_sessions_and_allows_new_password(tmp_path):
    store, sessions, manager = _components(tmp_path)
    old_session = sessions.login("old")
    token = asyncio.run(manager.start())
    assert manager.complete(token, "new-password-123") is True
    assert old_session not in sessions._sessions
    assert sessions.login("old") is None
    assert sessions.login("new-password-123") is not None
    assert store.current_verifier() == "new-password-123-verifier"


def test_concurrent_completion_consumes_a_challenge_once(tmp_path):
    store, sessions, manager = _components(tmp_path)
    token = asyncio.run(manager.start())
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: manager.complete(token, "new-password-123"), range(2)))
    assert results.count(True) == 1
    assert results.count(False) == 1


def test_concurrent_starts_show_at_most_one_confirmation(tmp_path):
    class _BlockingConfirmation:
        def __init__(self):
            self.calls = 0
            self.entered = Event()
            self.release = Event()

        def confirm(self):
            self.calls += 1
            self.entered.set()
            self.release.wait(timeout=2)
            return True

    store, sessions, _ = _components(tmp_path)
    confirmation = _BlockingConfirmation()
    manager = PasswordRecoveryManager(store, sessions, confirmation)

    async def exercise():
        first = asyncio.create_task(manager.start())
        await asyncio.to_thread(confirmation.entered.wait, 2)
        assert await manager.start() is None
        confirmation.release.set()
        assert await first is not None

    asyncio.run(exercise())
    assert confirmation.calls == 1


def test_confirmation_exception_releases_gate_for_a_later_request(tmp_path):
    class _FailThenConfirm:
        def __init__(self):
            self.calls = 0

        def confirm(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("desktop confirmation unavailable")
            return True

    store, sessions, _ = _components(tmp_path)
    confirmation = _FailThenConfirm()
    manager = PasswordRecoveryManager(store, sessions, confirmation)
    assert asyncio.run(manager.start()) is None
    assert asyncio.run(manager.start()) is not None
    assert confirmation.calls == 2


def test_windows_confirmation_uses_active_desktop_message_box(monkeypatch):
    import ai_provider_gateway.control.password_recovery as recovery

    calls = []

    class _User32:
        def MessageBoxW(self, *args):
            calls.append(args)
            return 6

    class _Windll:
        user32 = _User32()

    monkeypatch.setattr(recovery.os, "name", "nt")
    monkeypatch.setattr(recovery.ctypes, "windll", _Windll(), raising=False)
    assert WindowsMessageBoxConfirmation().confirm() is True
    assert calls and calls[0][-1] == 0x00000004 | 0x00000030


def test_non_windows_confirmation_safely_rejects(monkeypatch):
    import ai_provider_gateway.control.password_recovery as recovery

    monkeypatch.setattr(recovery.os, "name", "posix")
    assert WindowsMessageBoxConfirmation().confirm() is False
