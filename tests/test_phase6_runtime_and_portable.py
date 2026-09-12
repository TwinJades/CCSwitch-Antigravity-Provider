from __future__ import annotations

import base64
import bcrypt
import asyncio
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from ai_provider_gateway.control.app import create_app
from ai_provider_gateway.control.auth import AdminSessionManager, authorize_local_control
from ai_provider_gateway.control.runtime import AntigravityProxyControl
from ai_provider_gateway.control.password_recovery import AdminPasswordStore, PasswordRecoveryManager, _metadata
from ai_provider_gateway.control.shutdown import ShutdownRequest
from ai_provider_gateway.gateway.authentication import GatewayApiKeyStore, gateway_api_keys
from ai_provider_gateway.launcher import PortableLauncher
from ai_provider_gateway.portable import (
    PortableLayout, PortableSecretStore, RuntimeSecrets, WindowsDataProtector,
    antigravity_proxy_url, bootstrap_database, normalize_antigravity_proxy_url,
    runtime_environment, upgrade_database, validate_sidecar_install,
    write_antigravity_proxy_url, write_sidecar_config,
)
from ai_provider_gateway.sidecars.cliproxy.lifecycle import SidecarProcessState, SidecarRuntimeState


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8010"
MUTATION_HEADERS = {"origin": ORIGIN}


class _Phase2:
    async def aclose(self):
        return None


class _Confirmation:
    def confirm(self):
        return True


class _Updates:
    def __init__(self):
        self.installed = []
        self.rolled_back = 0

    def list_candidates(self):
        return [{"candidate_id": "fixed-v2", "version": "2.0.0"}]

    async def install(self, candidate_id):
        self.installed.append(candidate_id)
        return type("Result", (), {"status": "installed", "message": "ok", "version": "2.0.0"})()

    async def rollback(self):
        self.rolled_back += 1
        return type("Result", (), {"status": "rolled_back", "message": "ok", "version": "1.0.0"})()


def _phase6_client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'admin.db'}")
    _metadata.create_all(engine)
    gateway_api_keys.create(engine, checkfirst=True)
    store = AdminPasswordStore(engine)
    store.initialize_if_missing("old-verifier")
    sessions = AdminSessionManager(
        "old-verifier",
        password_verifier=lambda verifier, password: verifier == f"{password}-verifier",
    )
    recovery = PasswordRecoveryManager(
        store, sessions, _Confirmation(), hasher=lambda password: f"{password}-verifier"
    )
    updates = _Updates()
    shutdown = ShutdownRequest(tmp_path / "runtime" / "shutdown.request")
    app = create_app(
        phase2=_Phase2(), authorize_control=sessions.authorize,
        admin_sessions=sessions, password_recovery=recovery,
        sidecar_updates=updates, shutdown_request=shutdown,
        gateway_keys=GatewayApiKeyStore(engine, hasher=lambda value: f"hashed:{value}"),
    )
    return TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)), updates, shutdown


def test_dashboard_recovery_invalidates_session_and_secures_browser_boundary(tmp_path):
    client, _, _ = _phase6_client(tmp_path)
    with client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert 'id="logout"' in page.text
        assert 'keyResult.textContent = ""' in page.text
        assert 'window.addEventListener("pagehide", clearGatewayKey)' in page.text
        logout_handler = page.text.split(
            'document.getElementById("logout")', 1
        )[1].split('document.getElementById("shutdown")', 1)[0]
        assert logout_handler.index("leaveSignedIn();") < logout_handler.index(
            'request("/api/control/session/logout")'
        )
        shutdown_handler = page.text.split(
            'document.getElementById("shutdown")', 1
        )[1].split('document.getElementById("key-form")', 1)[0]
        assert shutdown_handler.index("leaveSignedIn();") < shutdown_handler.index(
            'request("/api/control/application/shutdown")'
        )
        assert client.post("/api/control/session/login", json={"password": "old"}).status_code == 403
        assert client.post(
            "/api/control/session/login", headers=MUTATION_HEADERS, json={"password": "old"}
        ).status_code == 204
        assert client.get("/api/control/sidecar/updates").status_code == 200
        challenge = client.post("/api/control/password-recovery/challenge", headers=MUTATION_HEADERS)
        completed = client.post(
            "/api/control/password-recovery/complete", headers=MUTATION_HEADERS,
            json={"recovery_token": challenge.json()["recovery_token"], "new_password": "new-password-123"},
        )
        assert completed.status_code == 204
        assert client.get("/api/control/sidecar/updates").status_code == 403
        assert client.post(
            "/api/control/session/login", headers=MUTATION_HEADERS, json={"password": "old"}
        ).status_code == 401
        assert client.post(
            "/api/control/session/login", headers=MUTATION_HEADERS, json={"password": "new-password-123"}
        ).status_code == 204
        issued = client.post(
            "/api/control/gateway-keys",
            headers=MUTATION_HEADERS,
            json={"label": "local-client"},
        )
        assert issued.status_code == 201
        assert issued.headers["cache-control"] == "no-store"
        assert issued.json()["key_id"] == "local-client"
        assert len(issued.json()["secret"]) >= 32
        assert client.post(
            "/api/control/gateway-keys",
            headers=MUTATION_HEADERS,
            json={"label": "local-client"},
        ).status_code == 400


def test_direct_dashboard_keeps_loopback_boundary_and_key_deletion_is_immediate(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'direct.db'}")
    gateway_api_keys.create(engine, checkfirst=True)
    keys = GatewayApiKeyStore(
        engine,
        hasher=lambda value: f"hashed:{value}",
        verifier=lambda encoded, supplied: encoded == f"hashed:{supplied}",
        secret_factory=lambda _: "phase9-test-key-value-that-is-not-a-real-secret",
    )
    app = create_app(
        phase2=_Phase2(),
        authorize_control=authorize_local_control,
        gateway_keys=keys,
        dashboard_password_bypass=True,
    )
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert 'data-control-mode="direct"' in page.text
        assert client.post("/api/control/session/login", headers=MUTATION_HEADERS, json={"password": "unused"}).status_code == 404
        issued = client.post(
            "/api/control/gateway-keys",
            headers=MUTATION_HEADERS,
            json={"label": "opencode"},
        )
        assert issued.status_code == 201
        supplied = issued.json()["secret"]
        assert keys.authenticate(supplied) is not None

        listed = client.get("/api/control/gateway-keys")
        assert listed.status_code == 200
        assert listed.headers["cache-control"] == "no-store"
        assert listed.json()["data"][0]["last_used_at"] is not None
        assert "secret" not in listed.text
        assert "verifier" not in listed.text

        deleted = client.delete(
            "/api/control/gateway-keys/opencode",
            headers=MUTATION_HEADERS,
        )
        assert deleted.status_code == 204
        assert keys.authenticate(supplied) is None
        assert client.delete(
            "/api/control/gateway-keys/opencode",
            headers=MUTATION_HEADERS,
        ).status_code == 404

        assert client.post(
            "/api/control/gateway-keys",
            json={"label": "missing-origin"},
        ).status_code == 403

    with TestClient(app, base_url=ORIGIN, client=("192.0.2.10", 12345)) as remote:
        assert remote.get("/").status_code == 403
        assert remote.get("/api/control/gateway-keys").status_code == 403


def test_update_and_shutdown_routes_require_authenticated_session(tmp_path):
    client, updates, shutdown = _phase6_client(tmp_path)
    with client:
        assert client.post(
            "/api/control/sidecar/updates/install", headers=MUTATION_HEADERS,
            json={"candidate_id": "fixed-v2"},
        ).status_code == 403
        client.post("/api/control/session/login", headers=MUTATION_HEADERS, json={"password": "old"})
        assert client.post(
            "/api/control/sidecar/updates/install", headers=MUTATION_HEADERS,
            json={"candidate_id": "fixed-v2"},
        ).json()["status"] == "installed"
        assert client.post(
            "/api/control/sidecar/updates/rollback", headers=MUTATION_HEADERS
        ).json()["status"] == "rolled_back"
        assert client.post(
            "/api/control/application/shutdown", headers=MUTATION_HEADERS
        ).status_code == 202
    assert updates.installed == ["fixed-v2"]
    assert updates.rolled_back == 1
    assert shutdown.path.read_text(encoding="utf-8") == "shutdown-requested\n"


class _ReversingProtector:
    def protect(self, value):
        return value[::-1]

    def unprotect(self, value):
        return value[::-1]


def _runtime_secrets():
    return RuntimeSecrets("s" * 40, "m" * 40)


def test_portable_secret_store_round_trips_and_never_repr_exposes_values(tmp_path):
    path = tmp_path / "data" / "runtime-secrets.dat"
    store = PortableSecretStore(path, _ReversingProtector())
    created = store.load_or_create()
    assert created == store.load_or_create()
    assert "<redacted>" in repr(created)
    assert created.sidecar_api_key not in repr(created)
    encrypted = base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
    assert b"sidecar_api_key" not in encrypted


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is required")
def test_windows_dpapi_round_trip_uses_current_user_context():
    protector = WindowsDataProtector()
    plaintext = b"phase6-dpapi-round-trip"
    protected = protector.protect(plaintext)
    assert protected != plaintext
    assert protector.unprotect(protected) == plaintext


def test_portable_layout_config_environment_and_database_bootstrap(tmp_path):
    layout = PortableLayout(tmp_path / "bundle")
    layout.ensure_directories()
    runtime = _runtime_secrets()
    write_sidecar_config(layout, runtime)
    original = layout.sidecar_config.read_bytes()
    write_sidecar_config(layout, runtime)
    assert layout.sidecar_config.read_bytes() == original
    config = layout.sidecar_config.read_text(encoding="utf-8")
    assert 'host: "127.0.0.1"' in config
    assert "allow-remote: false" in config
    assert "g" * 40 not in config
    assert "m" * 40 not in config
    configured_hash = config.split('  secret-key: "', 1)[1].split('"', 1)[0]
    assert configured_hash.startswith("$2a$")
    assert bcrypt.checkpw(runtime.management_key.encode(), configured_hash.encode())
    environment = runtime_environment(layout, runtime)
    assert environment["AIPG_PHASE6_ENABLED"] == "1"
    assert environment["AIPG_AUTOSTART_SIDECAR"] == "1"
    assert environment["AIPG_SIDECAR_BASE_URL"] == "http://127.0.0.1:8317"
    assert environment["AIPG_GATEWAY_BASE_URL"] == "http://127.0.0.1:8020/v1"
    assert "AIPG_GATEWAY_API_KEY" not in environment
    upgrade_database(layout.database_url, PROJECT_ROOT)
    bootstrap_database(layout.database_url)
    bootstrap_database(layout.database_url)
    engine = create_engine(layout.database_url)
    with engine.connect() as connection:
        assert connection.execute(select(gateway_api_keys.c.key_id)).all() == []


def test_sidecar_config_accepts_matching_cli_proxy_bcrypt_rewrite_and_rejects_mismatch(tmp_path):
    layout = PortableLayout(tmp_path / "bundle")
    layout.ensure_directories()
    runtime = _runtime_secrets()
    write_sidecar_config(layout, runtime)
    content = layout.sidecar_config.read_text(encoding="utf-8")
    current_hash = content.split('  secret-key: "', 1)[1].split('"', 1)[0]
    rewritten = bcrypt.hashpw(runtime.management_key.encode(), bcrypt.gensalt()).decode()
    rewritten = "$2a$" + rewritten[4:]
    layout.sidecar_config.write_text(content.replace(current_hash, rewritten), encoding="utf-8")
    write_sidecar_config(layout, runtime)

    wrong_hash = bcrypt.hashpw(b"different-management-key", bcrypt.gensalt()).decode()
    wrong_hash = "$2a$" + wrong_hash[4:]
    layout.sidecar_config.write_text(
        layout.sidecar_config.read_text(encoding="utf-8").replace(rewritten, wrong_hash),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="does not match protected runtime state"):
        write_sidecar_config(layout, runtime)


def test_sidecar_config_rejects_changed_api_key_without_overwriting(tmp_path):
    layout = PortableLayout(tmp_path / "bundle")
    layout.ensure_directories()
    runtime = _runtime_secrets()
    write_sidecar_config(layout, runtime)
    original = layout.sidecar_config.read_text(encoding="utf-8")
    changed = original.replace(runtime.sidecar_api_key, "x" * 40)
    layout.sidecar_config.write_text(changed, encoding="utf-8")
    with pytest.raises(Exception, match="does not match protected runtime state"):
        write_sidecar_config(layout, runtime)
    assert layout.sidecar_config.read_text(encoding="utf-8") == changed


def test_antigravity_proxy_is_local_http_only_persists_and_preserves_config_guard(tmp_path):
    layout = PortableLayout(tmp_path / "bundle")
    layout.ensure_directories()
    runtime = _runtime_secrets()
    write_sidecar_config(layout, runtime)
    assert antigravity_proxy_url(layout, runtime) is None

    assert write_antigravity_proxy_url(
        layout, runtime, "http://127.0.0.1:18080/"
    ) is True
    assert antigravity_proxy_url(layout, runtime) == "http://127.0.0.1:18080"
    write_sidecar_config(layout, runtime)

    assert write_antigravity_proxy_url(layout, runtime, None) is False
    assert antigravity_proxy_url(layout, runtime) is None
    for invalid in (
        "http://user:password@127.0.0.1:18080",
        "https://127.0.0.1:18080",
        "http://example.test:18080",
        "http://127.0.0.1",
    ):
        with pytest.raises(ValueError):
            normalize_antigravity_proxy_url(invalid)


def test_antigravity_proxy_control_restarts_only_sidecar_and_keeps_setting(tmp_path):
    layout = PortableLayout(tmp_path / "bundle")
    layout.ensure_directories()
    runtime = _runtime_secrets()
    write_sidecar_config(layout, runtime)
    events = []
    class Phase2:
        async def stop_sidecar(self):
            events.append("stop")
            return SidecarRuntimeState(SidecarProcessState.STOPPED, None)
        async def start_sidecar(self):
            events.append("start")
            return SidecarRuntimeState(SidecarProcessState.RUNNING, 1)
    control = AntigravityProxyControl(layout, runtime, Phase2())
    configured, state = asyncio.run(control.replace("http://localhost:18080"))
    assert configured is True and state.state is SidecarProcessState.RUNNING
    assert control.configured() is True
    assert events == ["stop", "start"]


def test_antigravity_proxy_api_requires_auth_and_never_returns_address():
    class Proxy:
        def configured(self): return True
        async def replace(self, value):
            return value is not None, SidecarRuntimeState(SidecarProcessState.RUNNING, 1)
    app = create_app(
        phase2=_Phase2(),
        authorize_control=lambda request: request.headers.get("x-admin") == "yes",
        antigravity_proxy=Proxy(),
    )
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345)) as client:
        assert client.get("/api/control/providers/antigravity/proxy").status_code == 403
        headers = {"x-admin": "yes", **MUTATION_HEADERS}
        status = client.get(
            "/api/control/providers/antigravity/proxy", headers={"x-admin": "yes"}
        )
        changed = client.post(
            "/api/control/providers/antigravity/proxy", headers=headers,
            json={"proxy_url": "http://127.0.0.1:18080"},
        )
    assert status.json() == {"configured": True}
    assert changed.json() == {"configured": True, "sidecar_state": "running"}
    assert "18080" not in changed.text


def test_portable_start_validates_fixed_tested_sidecar_pair(tmp_path):
    layout = PortableLayout(tmp_path)
    layout.ensure_directories()
    layout.sidecar_executable.write_bytes(b"fixed-sidecar")
    layout.sidecar_manifest.write_text(
        '{"name":"CLIProxyAPI","version":"1.0.0","commit":"abc123",'
        '"sha256":"' + "a" * 64 + '","download_source":"https://example.test/release",'
        '"installed_at":"2026-01-01T00:00:00Z","tested":true}',
        encoding="utf-8",
    )
    assert validate_sidecar_install(layout, lambda *_: True).version == "1.0.0"
    with pytest.raises(Exception):
        validate_sidecar_install(layout, lambda *_: False)


class _Process:
    def __init__(self, polls=None):
        self.pid = 1
        self.polls = list(polls or [])
        self.stopped = False

    def poll(self):
        if self.stopped:
            return 0
        return self.polls.pop(0) if self.polls else None

    def terminate(self):
        self.stopped = True

    def kill(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0


def test_launcher_starts_services_restarts_gateway_and_stops_only_owned_handles(tmp_path):
    layout = PortableLayout(tmp_path)
    layout.ensure_directories()
    created = []
    opened = []

    def factory(command, cwd, environment):
        service = command[-1]
        first_gateway = service == "gateway" and not any(item[0] == "gateway" for item in created)
        process = _Process([None, 1] if first_gateway else [])
        created.append((service, process, cwd, environment))
        return process

    sleep_count = [0]

    def sleeper(_):
        sleep_count[0] += 1
        if sleep_count[0] >= 1:
            ShutdownRequest(layout.shutdown_request).request()

    launcher_environment = {
        "PATH": "safe-path",
        "AIPG_DATABASE_URL": "sqlite:///local.db",
        "AIPG_SIDECAR_BASE_URL": "http://127.0.0.1:8317",
        "AIPG_SIDECAR_API_KEY": "sidecar-api",
        "AIPG_SIDECAR_MANAGEMENT_KEY": "management-key",
        "AIPG_GATEWAY_API_KEY": "must-not-be-inherited",
        "AIPG_SIDECAR_MANIFEST": "manifest.json",
    }
    launcher = PortableLauncher(
        layout, launcher_environment, process_factory=factory, sleeper=sleeper,
        browser_open=opened.append, ready_probe=lambda _: True,
    )
    assert launcher.run() == 0
    assert [item[0] for item in created] == ["supervisor", "gateway", "gateway"]
    assert opened == ["http://127.0.0.1:8010/"]
    assert created[0][1].stopped
    assert not created[1][1].stopped  # It had already exited before the restart.
    assert created[2][1].stopped
    supervisor_environment = created[0][3]
    gateway_environment = created[1][3]
    assert "AIPG_GATEWAY_API_KEY" not in supervisor_environment
    assert "AIPG_GATEWAY_API_KEY" not in gateway_environment
    assert "AIPG_SIDECAR_MANAGEMENT_KEY" in supervisor_environment
    assert "AIPG_SIDECAR_MANAGEMENT_KEY" not in gateway_environment
    assert set(key for key in gateway_environment if key.startswith("AIPG_")) == {
        "AIPG_DATABASE_URL", "AIPG_SIDECAR_BASE_URL", "AIPG_SIDECAR_API_KEY"
    }
    assert not layout.shutdown_request.exists()
    assert len(list((layout.runtime_dir / "shutdown-history").glob("*.request"))) == 1


def test_portable_build_inputs_are_fixed_and_do_not_download_or_embed_credentials():
    script = (PROJECT_ROOT / "scripts" / "build-portable.ps1").read_text(encoding="utf-8")
    spec = (PROJECT_ROOT / "packaging" / "start.spec").read_text(encoding="utf-8")
    assert "SidecarExecutable" in script and "SidecarManifest" in script
    assert "A fixed Sidecar version is required" in script
    assert "Get-FileHash" in script
    assert "does not match the trusted manifest" in script
    assert "Compress-Archive" in script
    assert "Invoke-WebRequest" not in script
    assert "Start-Process" not in script
    assert "env.ps1" not in script
    assert 'root / "packaging" / "start.py"' in spec
    assert "console=False" in spec
    assert 'name="start"' in spec


def test_portable_build_entrypoint_uses_package_import():
    source = (PROJECT_ROOT / "packaging" / "start.py").read_text(encoding="utf-8")
    assert "from ai_provider_gateway.launcher import main" in source
    assert "from .launcher" not in source


def test_launcher_cli_exposes_background_acceptance_mode():
    source = (PROJECT_ROOT / "src" / "ai_provider_gateway" / "launcher.py").read_text(
        encoding="utf-8"
    )
    assert '"--no-browser"' in source
    assert "options.no_browser" in source


def test_portable_launcher_stops_its_windows_process_tree():
    source = (PROJECT_ROOT / "src" / "ai_provider_gateway" / "launcher.py").read_text(
        encoding="utf-8"
    )
    assert '["taskkill", "/PID", str(process.pid), "/T", "/F"]' in source
    assert "isinstance(process, subprocess.Popen)" in source
