import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ai_provider_gateway.control.app import create_app
from ai_provider_gateway.control.auth import AdminSessionManager
from ai_provider_gateway.control.runtime import Phase2RuntimeSettings
from ai_provider_gateway.sidecars.cliproxy.client import (
    CLIProxyAPIClient,
    ManagementKey,
    SidecarApiKey,
)
from ai_provider_gateway.sidecars.cliproxy.health import SidecarHealthState


class _NoopPhase:
    async def aclose(self):
        return None

    async def sidecar_health(self):
        return SimpleNamespace(
            state=SidecarHealthState.AVAILABLE,
            model_count=1,
            quota_status="unknown",
            managed=True,
        )


def _client(app):
    return TestClient(app, client=("127.0.0.1", 12345))


def test_runtime_settings_rejects_same_api_and_management_credentials(tmp_path):
    with pytest.raises(ValueError, match="different"):
        Phase2RuntimeSettings(
            executable=tmp_path / "sidecar.exe",
            config_path=tmp_path / "config.yaml",
            database_url="sqlite:///:memory:",
            api_key="same-secret",
            management_key="same-secret",
        )


def test_default_client_disables_proxy_environment():
    client = CLIProxyAPIClient(
        base_url="http://127.0.0.1:8317",
        api_key=SidecarApiKey("api-secret"),
        management_key=ManagementKey("management-secret"),
    )
    try:
        assert client._client._trust_env is False
    finally:
        asyncio.run(client.aclose())


def test_admin_session_manager_boundaries_login_expiry_logout_without_password_backend():
    now = [100.0]
    verifier_calls = []

    def verifier(encoded, supplied):
        verifier_calls.append((encoded, supplied))
        return encoded == "encoded" and supplied == "correct"

    sessions = AdminSessionManager(
        "encoded",
        password_verifier=verifier,
        ttl_seconds=10,
        clock=lambda: now[0],
    )
    app = create_app(
        phase2=_NoopPhase(),
        authorize_control=sessions.authorize,
        admin_sessions=sessions,
    )
    with _client(app) as client:
        boundary = {"host": "127.0.0.1", "origin": "http://127.0.0.1"}
        assert client.get(
            "/api/control/sidecar/health",
            headers={"host": "127.0.0.1"},
        ).status_code == 403
        assert client.post(
            "/api/control/session/login",
            json={"password": "correct"},
            headers={"host": "127.0.0.1", "origin": "http://evil.example"},
        ).status_code == 403
        wrong = client.post(
            "/api/control/session/login",
            json={"password": "wrong"},
            headers=boundary,
        )
        assert wrong.status_code == 401
        response = client.post(
            "/api/control/session/login",
            json={"password": "correct"},
            headers=boundary,
        )
        assert response.status_code == 204
        set_cookie = response.headers["set-cookie"].lower()
        assert "httponly" in set_cookie and "samesite=strict" in set_cookie
        assert "aipg_admin_session=" in set_cookie
        assert client.get(
            "/api/control/sidecar/health",
            headers={"host": "127.0.0.1"},
        ).status_code == 200
        now[0] += 11
        assert client.get(
            "/api/control/sidecar/health",
            headers={"host": "127.0.0.1"},
        ).status_code == 403
        now[0] = 100.0
        login = client.post(
            "/api/control/session/login",
            json={"password": "correct"},
            headers=boundary,
        )
        assert login.status_code == 204
        assert client.post(
            "/api/control/session/logout", headers=boundary
        ).status_code == 204
        assert client.get(
            "/api/control/sidecar/health",
            headers={"host": "127.0.0.1"},
        ).status_code == 403
    assert verifier_calls == [
        ("encoded", "wrong"),
        ("encoded", "correct"),
        ("encoded", "correct"),
    ]


def test_admin_session_manager_rejects_non_loopback_host_and_cross_origin_post():
    sessions = AdminSessionManager(
        "encoded", password_verifier=lambda *_: True, clock=lambda: 1.0
    )
    app = create_app(
        phase2=_NoopPhase(),
        authorize_control=sessions.authorize,
        admin_sessions=sessions,
    )
    with _client(app) as client:
        assert client.post(
            "/api/control/session/login",
            json={"password": "x"},
            headers={"host": "example.test", "origin": "http://example.test"},
        ).status_code == 403
        assert client.post(
            "/api/control/session/login",
            json={"password": "x"},
            headers={"host": "127.0.0.1", "origin": "http://localhost"},
        ).status_code == 403


def test_app_shutdown_closes_phase2_and_login_enables_control():
    class Phase:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

        async def sidecar_health(self):
            return SimpleNamespace(
                state=SidecarHealthState.AVAILABLE,
                model_count=1,
                quota_status="unknown",
                managed=True,
            )

    phase = Phase()
    sessions = AdminSessionManager(
        "encoded", password_verifier=lambda encoded, password: password == "ok"
    )
    app = create_app(
        phase2=phase,
        authorize_control=sessions.authorize,
        admin_sessions=sessions,
    )
    with _client(app) as client:
        headers = {"host": "127.0.0.1", "origin": "http://127.0.0.1"}
        assert client.get(
            "/api/control/sidecar/health",
            headers={"host": "127.0.0.1"},
        ).status_code == 403
        assert client.post(
            "/api/control/session/login",
            json={"password": "ok"},
            headers=headers,
        ).status_code == 204
        assert client.get(
            "/api/control/sidecar/health",
            headers={"host": "127.0.0.1"},
        ).status_code == 200
    assert phase.closed is True


def test_runtime_app_factory_composes_phase2_and_sessions(monkeypatch, tmp_path):
    import ai_provider_gateway.control.runtime_app as runtime_app

    calls = []
    settings = Phase2RuntimeSettings(
        tmp_path / "x.exe",
        tmp_path / "x.yaml",
        "sqlite:///:memory:",
        api_key="api",
        management_key="management",
    )
    phase = object()

    class FakeSessions:
        def __init__(self, verifier):
            calls.append(("sessions", verifier))

        def authorize(self, request):
            return False

    monkeypatch.setattr(
        runtime_app.Phase2RuntimeSettings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        runtime_app,
        "build_phase2_control",
        lambda received: calls.append(("phase2", received)) or phase,
    )
    monkeypatch.setattr(runtime_app, "AdminSessionManager", FakeSessions)
    monkeypatch.setenv("AIPG_ADMIN_PASSWORD_VERIFIER", "encoded-verifier")
    application = runtime_app.create_runtime_app()
    assert calls == [("phase2", settings), ("sessions", "encoded-verifier")]
    assert any(route.path == "/api/control/session/login" for route in application.routes)
    assert any(route.path == "/api/control/sidecar/health" for route in application.routes)


def test_supervisor_launcher_targets_runtime_app_factory():
    script = Path("scripts/dev/run-supervisor.ps1").read_text(encoding="utf-8")
    assert "ai_provider_gateway.control.runtime_app:create_runtime_app" in script
    assert "--factory" in script
