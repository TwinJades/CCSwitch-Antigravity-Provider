import asyncio
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from sqlalchemy import inspect

from ai_provider_gateway.database import create_database_engine
from ai_provider_gateway.models import (
    DiscoveredModel,
    DiscoveryState,
    ModelRegistry,
    ModelRegistryError,
)
from ai_provider_gateway.providers.antigravity.discovery import AntigravityDiscovery
from ai_provider_gateway.sidecars.cliproxy.client import (
    CLIProxyAPIClient,
    ManagementKey,
    OAuthAuthorizationState,
    SidecarApiKey,
    SidecarAuthenticationError,
    SidecarConfigurationError,
    SidecarProtocolError,
    SidecarTransportError,
)
from ai_provider_gateway.sidecars.cliproxy.health import (
    SidecarHealthChecker,
    SidecarHealthState,
)
from ai_provider_gateway.sidecars.cliproxy.lifecycle import (
    CLIProxyAPILifecycle,
    SidecarLaunchSpec,
    SidecarProcessState,
)


def _client(handler):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8317")
    return CLIProxyAPIClient(
        base_url="http://127.0.0.1:8317",
        api_key=SidecarApiKey("api-secret"),
        management_key=ManagementKey("management-secret"),
        client=http,
    ), http


def test_client_rejects_non_loopback_and_userinfo_urls():
    for base in ("http://example.test", "http://user:pass@127.0.0.1:8317", "http://127.0.0.1:8317/?x=1"):
        with pytest.raises(SidecarConfigurationError):
            CLIProxyAPIClient(base_url=base, api_key=SidecarApiKey("a"), management_key=ManagementKey("m"))


def test_secret_credentials_do_not_leak_from_repr():
    for value in ("api-secret", "management-secret"):
        assert value not in repr(SidecarApiKey(value))
        assert value not in repr(ManagementKey(value))


def test_client_separates_data_and_management_authorization_and_parses_dynamic_models():
    seen = []
    async def handler(request):
        seen.append((request.url.path, request.headers.get("authorization")))
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "upstream.dynamic"}]})
        return httpx.Response(200, json={"url": "https://accounts.example/authorize", "state": "s-1"})
    client, http = _client(handler)
    async def run():
        models = await client.discover_models()
        oauth = await client.get_antigravity_auth_url()
        await http.aclose()
        return models, oauth
    models, oauth = asyncio.run(run())
    assert [item.model_id for item in models.models] == ["upstream.dynamic"]
    assert oauth.state == "s-1"
    assert seen == [("/v1/models", "Bearer api-secret"), ("/v0/management/antigravity-auth-url", "Bearer management-secret")]


def test_client_rejects_hardcoded_or_malformed_models_and_oauth_urls():
    async def bad_models(request):
        return httpx.Response(200, json={"data": [{"id": ""}]})
    client, http = _client(bad_models)
    async def run():
        with pytest.raises(SidecarProtocolError):
            await client.discover_models()
        await http.aclose()
    asyncio.run(run())

    async def bad_oauth(request):
        return httpx.Response(200, json={"url": "http://accounts.example/auth", "state": "s"})
    client, http = _client(bad_oauth)
    async def run_oauth():
        with pytest.raises(SidecarProtocolError):
            await client.get_antigravity_auth_url()
        await http.aclose()
    asyncio.run(run_oauth())


def test_client_normalizes_wait_to_pending_and_auth_files_only_count_antigravity():
    async def handler(request):
        if request.url.path.endswith("get-auth-status"):
            return httpx.Response(200, json={"status": "wait"})
        return httpx.Response(200, json={"files": [
            {"provider": "antigravity", "status": "active", "name": "secret.json", "email": "a@example"},
            {"provider": "antigravity", "disabled": True, "name": "disabled.json"},
            {"provider": "other", "status": "active"},
        ]})
    client, http = _client(handler)
    async def run():
        status = await client.get_auth_status("state")
        summary = await client.get_auth_file_summary()
        await http.aclose()
        return status, summary
    status, summary = asyncio.run(run())
    assert status.state is OAuthAuthorizationState.PENDING
    assert (summary.total, summary.active, summary.inactive, summary.unknown) == (2, 1, 1, 0)
    assert not hasattr(summary, "name") and not hasattr(summary, "email")


@pytest.mark.parametrize("status, expected", [(401, SidecarAuthenticationError), (500, SidecarTransportError)])
def test_client_classifies_http_errors(status, expected):
    async def handler(request):
        return httpx.Response(status)
    client, http = _client(handler)
    async def run():
        with pytest.raises(expected):
            await client.discover_models()
        await http.aclose()
    asyncio.run(run())


def test_client_classifies_transport_errors_without_raw_text():
    async def handler(request):
        raise httpx.ConnectError("secret endpoint text", request=request)
    client, http = _client(handler)
    async def run():
        with pytest.raises(SidecarTransportError) as caught:
            await client.discover_models()
        await http.aclose()
        return str(caught.value)
    assert "secret endpoint text" not in asyncio.run(run())


@dataclass
class FakeProcess:
    pid: int
    code: int | None = None
    terminated: bool = False
    killed: bool = False
    def poll(self): return self.code
    def terminate(self): self.terminated = True; self.code = 0
    def kill(self): self.killed = True; self.code = -9


def test_lifecycle_uses_safe_argv_hidden_options_and_owned_process_only(tmp_path):
    executable = tmp_path / "folder with spaces" / "cliproxy.exe"
    config = tmp_path / "folder with spaces" / "config file.yaml"
    executable.parent.mkdir(); executable.write_text("", encoding="utf-8"); config.write_text("", encoding="utf-8")
    calls = []
    def factory(command, cwd, flags, startup):
        calls.append((tuple(command), cwd, flags, startup))
        return FakeProcess(42)
    lifecycle = CLIProxyAPILifecycle(SidecarLaunchSpec(executable, config), process_factory=factory)
    state = asyncio.run(lifecycle.start())
    assert state.state is SidecarProcessState.RUNNING
    assert calls[0][0] == (str(executable.resolve()), "-config", str(config.resolve()))
    assert calls[0][1] == config.parent.resolve()
    assert lifecycle._process is not None
    process = lifecycle._process
    stopped = asyncio.run(lifecycle.stop())
    assert stopped.state is SidecarProcessState.STOPPED and process.terminated
    assert asyncio.run(lifecycle.stop()).pid is None


def test_lifecycle_start_is_idempotent_and_concurrent(tmp_path):
    executable = tmp_path / "x.exe"; config = tmp_path / "x.yaml"
    executable.write_text(""); config.write_text("")
    calls = []
    def factory(command, cwd, flags, startup): calls.append(1); return FakeProcess(9)
    lifecycle = CLIProxyAPILifecycle(SidecarLaunchSpec(executable, config), process_factory=factory)
    async def both():
        return await asyncio.gather(lifecycle.start(), lifecycle.start())
    results = asyncio.run(both())
    assert len(calls) == 1 and results[0].pid == results[1].pid == 9


def test_lifecycle_missing_paths_fail_without_starting(tmp_path):
    calls = []
    lifecycle = CLIProxyAPILifecycle(SidecarLaunchSpec(tmp_path / "none.exe", tmp_path / "none.yaml"), process_factory=lambda *args: calls.append(1))
    with pytest.raises(FileNotFoundError): asyncio.run(lifecycle.start())
    assert calls == []


def test_registry_refreshes_dynamically_marks_missing_unavailable_and_preserves_stale(tmp_path):
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'registry.db').as_posix()}")
    from alembic import command
    from alembic.config import Config
    config = Config("alembic.ini"); config.set_main_option("sqlalchemy.url", f"sqlite:///{(tmp_path / 'registry.db').as_posix()}")
    command.upgrade(config, "head")
    registry = ModelRegistry(engine)
    first = registry.refresh_success("Antigravity", [DiscoveredModel("one"), DiscoveredModel("two")])
    assert {m.canonical_id for m in first} == {"antigravity/one", "antigravity/two"}
    second = registry.refresh_success("antigravity", [DiscoveredModel("one")])
    listed = {m.canonical_id: m for m in registry.list_models()}
    assert listed["antigravity/two"].available is False
    assert all(m.discovery_state is DiscoveryState.AVAILABLE for m in second)
    with pytest.raises(ModelRegistryError): registry.refresh_success("antigravity", [])
    stale = registry.list_models()
    assert stale and all(m.discovery_state is DiscoveryState.STALE for m in stale)
    assert registry.discovery_state("antigravity").last_success_at is not None


def test_registry_aliases_are_global_and_control_characters_rejected(tmp_path):
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'registry.db').as_posix()}")
    from alembic import command
    from alembic.config import Config
    config = Config("alembic.ini"); config.set_main_option("sqlalchemy.url", f"sqlite:///{(tmp_path / 'registry.db').as_posix()}"); command.upgrade(config, "head")
    registry = ModelRegistry(engine)
    registry.refresh_success("one", [DiscoveredModel("a")]); registry.refresh_success("two", [DiscoveredModel("b")])
    registry.set_alias("one/a", "shared")
    with pytest.raises(ModelRegistryError): registry.set_alias("two/b", "shared")
    with pytest.raises(ModelRegistryError): registry.refresh_success("one", [DiscoveredModel("bad\nvalue")])


def test_antigravity_discovery_writes_registry_and_safe_failure_categories(tmp_path):
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'registry.db').as_posix()}")
    from alembic import command
    from alembic.config import Config
    config = Config("alembic.ini"); config.set_main_option("sqlalchemy.url", f"sqlite:///{(tmp_path / 'registry.db').as_posix()}"); command.upgrade(config, "head")
    registry = ModelRegistry(engine)
    class Client:
        async def discover_models(self):
            from ai_provider_gateway.sidecars.cliproxy.client import ModelDiscovery, DiscoveredModel as D
            return ModelDiscovery((D("dynamic-model"),))
    outcome = asyncio.run(AntigravityDiscovery(Client(), registry).refresh())
    assert outcome.models[0].canonical_id == "antigravity/dynamic-model"


def test_health_contract_keeps_quota_unknown_and_external_process_unmanaged():
    class Lifecycle:
        async def status(self):
            from ai_provider_gateway.sidecars.cliproxy.lifecycle import SidecarRuntimeState, SidecarProcessState
            return SidecarRuntimeState(SidecarProcessState.STOPPED, None)
    class Client:
        async def discover_models(self):
            from ai_provider_gateway.sidecars.cliproxy.client import ModelDiscovery, DiscoveredModel
            return ModelDiscovery((DiscoveredModel("x"),))
    health = asyncio.run(SidecarHealthChecker(Lifecycle(), Client()).check())
    assert health.state is SidecarHealthState.AVAILABLE
    assert health.managed is False and health.quota_status == "unknown"

@pytest.mark.parametrize("payload", [{}, {"files": ["not-an-object"]}])
def test_auth_files_requires_official_object_list(payload):
    async def handler(request):
        return httpx.Response(200, json=payload)
    client, http = _client(handler)
    async def run():
        with pytest.raises(SidecarProtocolError):
            await client.get_auth_file_summary()
        await http.aclose()
    asyncio.run(run())




def test_control_api_requires_authorizer_and_never_exposes_credentials():
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from ai_provider_gateway.control.app import create_app
    from ai_provider_gateway.control.phase2 import Phase2Control
    class FakePhase:
        async def sidecar_health(self):
            return SimpleNamespace(state=SidecarHealthState.AVAILABLE, model_count=1, quota_status="unknown", managed=True)
        async def start_sidecar(self):
            return SimpleNamespace(state=SidecarProcessState.RUNNING, pid=1, exit_code=None)
        async def stop_sidecar(self):
            return SimpleNamespace(state=SidecarProcessState.STOPPED, pid=None, exit_code=0)
        async def restart_sidecar(self):
            return SimpleNamespace(state=SidecarProcessState.RUNNING, pid=2, exit_code=None)
    with pytest.raises(ValueError):
        create_app(phase2=FakePhase())
    app = create_app(phase2=FakePhase(), authorize_control=lambda request: False)
    assert TestClient(app).get("/api/control/sidecar/health").status_code == 403
    app = create_app(phase2=FakePhase(), authorize_control=lambda request: True)
    response = TestClient(app).get("/api/control/sidecar/health")
    assert response.status_code == 200
    assert response.json() == {"state": "available", "model_count": 1, "quota_status": "unknown", "managed": True}
    assert "api-secret" not in response.text and "management-secret" not in response.text


def test_phase2_runtime_settings_requires_all_secrets_and_hides_repr(monkeypatch, tmp_path):
    from ai_provider_gateway.control.runtime import Phase2RuntimeSettings
    for key in ("AIPG_SIDECAR_EXECUTABLE", "AIPG_SIDECAR_CONFIG", "AIPG_SIDECAR_API_KEY", "AIPG_SIDECAR_MANAGEMENT_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError):
        Phase2RuntimeSettings.from_env()
    monkeypatch.setenv("AIPG_SIDECAR_EXECUTABLE", str(tmp_path / "sidecar.exe"))
    monkeypatch.setenv("AIPG_SIDECAR_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("AIPG_SIDECAR_API_KEY", "api-secret")
    monkeypatch.setenv("AIPG_SIDECAR_MANAGEMENT_KEY", "management-secret")
    settings = Phase2RuntimeSettings.from_env()
    assert "api-secret" not in repr(settings) and "management-secret" not in repr(settings)

@pytest.mark.parametrize("error, expected", [
    (SidecarAuthenticationError("x"), SidecarHealthState.API_AUTH_FAILED),
    (SidecarTransportError("x"), SidecarHealthState.UNAVAILABLE),
    (SidecarProtocolError("x"), SidecarHealthState.UNAVAILABLE),
])
def test_health_maps_probe_failures_without_quota_claims(error, expected):
    from ai_provider_gateway.sidecars.cliproxy.lifecycle import SidecarRuntimeState
    class Lifecycle:
        async def status(self):
            return SidecarRuntimeState(SidecarProcessState.RUNNING, 7)
    class Client:
        async def discover_models(self): raise error
    result = asyncio.run(SidecarHealthChecker(Lifecycle(), Client()).check())
    assert result.state is expected and result.quota_status == "unknown"


def test_health_classifies_crashed_and_starting():
    from ai_provider_gateway.sidecars.cliproxy.lifecycle import SidecarRuntimeState
    class Client:
        async def discover_models(self): raise SidecarTransportError("offline")
    for state, expected in ((SidecarProcessState.CRASHED, SidecarHealthState.CRASHED), (SidecarProcessState.STARTING, SidecarHealthState.STARTING)):
        class Lifecycle:
            async def status(self): return SidecarRuntimeState(state, 3, 1 if state is SidecarProcessState.CRASHED else None)
        result = asyncio.run(SidecarHealthChecker(Lifecycle(), Client()).check())
        assert result.state is expected and result.quota_status == "unknown"





def _migration_registry(tmp_path):
    from alembic import command
    from alembic.config import Config
    database_url = f"sqlite:///{(tmp_path / 'registry-extra.db').as_posix()}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    return create_database_engine(database_url)


def test_control_api_lifecycle_oauth_discovery_models_and_safe_errors(tmp_path):
    from datetime import UTC, datetime
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from ai_provider_gateway.control.app import create_app
    from ai_provider_gateway.models import ModelRecord
    from ai_provider_gateway.sidecars.cliproxy.client import OAuthAuthorization, OAuthAuthorizationState, OAuthStatus, SidecarProtocolError, SidecarTransportError
    class Phase:
        def __init__(self):
            self.calls = []
        async def start_sidecar(self):
            self.calls.append("start")
            return SimpleNamespace(state=SidecarProcessState.RUNNING, pid=11, exit_code=None)
        async def stop_sidecar(self):
            self.calls.append("stop")
            return SimpleNamespace(state=SidecarProcessState.STOPPED, pid=None, exit_code=0)
        async def restart_sidecar(self):
            self.calls.append("restart")
            return SimpleNamespace(state=SidecarProcessState.RUNNING, pid=12, exit_code=None)
        async def start_antigravity_oauth(self):
            self.calls.append("oauth-start")
            return OAuthAuthorization("https://accounts.example/authorize", "oauth-state")
        async def antigravity_oauth_status(self, state):
            self.calls.append(("oauth-status", state))
            return OAuthStatus(OAuthAuthorizationState.PENDING)
        async def discover_antigravity_models(self):
            self.calls.append("discover")
            return SimpleNamespace(state=DiscoveryState.AVAILABLE, stale=False, last_success_at=datetime.now(UTC), error_category=None, models=())
        def current_models(self):
            self.calls.append("models")
            return ()
    phase = Phase()
    client = TestClient(create_app(phase2=phase, authorize_control=lambda request: True))
    assert client.post("/api/control/sidecar/start").json()["state"] == "running"
    assert client.post("/api/control/sidecar/stop").json()["state"] == "stopped"
    assert client.post("/api/control/sidecar/restart").json()["state"] == "running"
    assert client.post("/api/control/providers/antigravity/oauth/start").json() == {"url": "https://accounts.example/authorize", "oauth_state": "oauth-state"}
    status = client.post("/api/control/providers/antigravity/oauth/status", json={"oauth_state": "oauth-state"})
    assert status.status_code == 200 and status.json() == {"state": "pending"}
    assert client.post("/api/provider/antigravity/oauth/status", json={"oauth_state": "oauth-state"}).status_code == 404
    assert client.post("/api/control/providers/antigravity/models/discover").status_code == 200
    assert client.get("/api/control/models").json() == {"data": []}
    assert phase.calls == ["start", "stop", "restart", "oauth-start", ("oauth-status", "oauth-state"), "discover", "models"]

    class FailingPhase(Phase):
        async def start_sidecar(self):
            raise SidecarTransportError("SECRET INTERNAL RESPONSE")
    failure = TestClient(create_app(phase2=FailingPhase(), authorize_control=lambda request: True)).post("/api/control/sidecar/start")
    assert failure.status_code == 503
    assert failure.json() == {"detail": {"code": "sidecar_unavailable"}}
    assert "SECRET INTERNAL RESPONSE" not in failure.text


def test_antigravity_discovery_failures_preserve_history_and_safe_categories(tmp_path):
    from ai_provider_gateway.sidecars.cliproxy.client import ModelDiscovery, DiscoveredModel as SidecarModel
    from ai_provider_gateway.sidecars.cliproxy.client import SidecarAuthenticationError, SidecarProtocolError, SidecarTransportError
    engine = _migration_registry(tmp_path)
    registry = ModelRegistry(engine)
    registry.refresh_success("antigravity", [DiscoveredModel("historical", "Historical")])
    previous = registry.discovery_state("antigravity").last_success_at
    exceptions = [
        (SidecarTransportError("transport secret"), "sidecar_unavailable"),
        (SidecarAuthenticationError("auth secret"), "sidecar_api_auth_failed"),
        (SidecarProtocolError("protocol secret"), "invalid_sidecar_response"),
    ]
    for error, category in exceptions:
        class Client:
            async def discover_models(self):
                raise error
        outcome = asyncio.run(AntigravityDiscovery(Client(), registry).refresh())
        assert outcome.state is DiscoveryState.STALE
        assert outcome.error_category == category
        assert outcome.last_success_at == previous
        assert outcome.models and outcome.models[0].canonical_id == "antigravity/historical"
    class EmptyClient:
        async def discover_models(self):
            return ModelDiscovery(( ))
    outcome = asyncio.run(AntigravityDiscovery(EmptyClient(), registry).refresh())
    assert outcome.state is DiscoveryState.STALE
    assert outcome.error_category == "empty_catalog"
    assert outcome.last_success_at == previous
    assert outcome.models[0].available is True


def test_lifecycle_restart_and_crashed_state(tmp_path):
    executable = tmp_path / "sidecar.exe"
    config = tmp_path / "config.yaml"
    executable.write_text("")
    config.write_text("")
    processes = []
    def factory(command, cwd, flags, startup):
        process = FakeProcess(100 + len(processes))
        processes.append(process)
        return process
    lifecycle = CLIProxyAPILifecycle(SidecarLaunchSpec(executable, config), process_factory=factory)
    first = asyncio.run(lifecycle.start())
    processes[0].code = 17
    crashed = asyncio.run(lifecycle.status())
    assert first.pid == 100 and crashed.state is SidecarProcessState.CRASHED and crashed.exit_code == 17
    restarted = asyncio.run(lifecycle.restart())
    assert restarted.state is SidecarProcessState.RUNNING and restarted.pid == 101
    assert processes[0].terminated is False


def test_health_full_stopped_auth_required_auth_failed_unavailable_matrix():
    from types import SimpleNamespace
    from ai_provider_gateway.sidecars.cliproxy.lifecycle import SidecarRuntimeState
    class Lifecycle:
        def __init__(self, state): self.state = state
        async def status(self): return SidecarRuntimeState(self.state, None)
    class EmptyClient:
        async def discover_models(self):
            from ai_provider_gateway.sidecars.cliproxy.client import ModelDiscovery
            return ModelDiscovery(())
        async def get_auth_file_summary(self): return SimpleNamespace(total=0, active=0, unknown=0)
    auth_required = asyncio.run(SidecarHealthChecker(Lifecycle(SidecarProcessState.RUNNING), EmptyClient()).check())
    assert auth_required.state is SidecarHealthState.AUTH_REQUIRED and auth_required.quota_status == "unknown"
    class UnknownStatusClient(EmptyClient):
        async def get_auth_file_summary(self): return SimpleNamespace(total=1, active=0, unknown=1)
    connected = asyncio.run(SidecarHealthChecker(Lifecycle(SidecarProcessState.RUNNING), UnknownStatusClient()).check())
    assert connected.state is SidecarHealthState.AVAILABLE and connected.model_count == 0
    class AuthFailClient:
        async def discover_models(self): raise SidecarAuthenticationError("auth")
    auth_failed = asyncio.run(SidecarHealthChecker(Lifecycle(SidecarProcessState.RUNNING), AuthFailClient()).check())
    assert auth_failed.state is SidecarHealthState.API_AUTH_FAILED
    class UnavailableClient:
        async def discover_models(self): raise SidecarTransportError("transport")
    unavailable = asyncio.run(SidecarHealthChecker(Lifecycle(SidecarProcessState.RUNNING), UnavailableClient()).check())
    assert unavailable.state is SidecarHealthState.UNAVAILABLE
    stopped = asyncio.run(SidecarHealthChecker(Lifecycle(SidecarProcessState.STOPPED), UnavailableClient()).check())
    assert stopped.state is SidecarHealthState.STOPPED and stopped.managed is False


def test_registry_rejects_control_characters_in_display_name_and_alias(tmp_path):
    registry = ModelRegistry(_migration_registry(tmp_path))
    with pytest.raises(ModelRegistryError):
        registry.refresh_success("antigravity", [DiscoveredModel("model", "bad\nname")])
    registry.refresh_success("antigravity", [DiscoveredModel("model", "safe")])
    with pytest.raises(ModelRegistryError):
        registry.set_alias("antigravity/model", "bad\talias")
