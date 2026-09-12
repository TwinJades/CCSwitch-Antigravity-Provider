from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from ai_provider_gateway.gateway.app import GatewayService, create_app
from ai_provider_gateway.gateway.authentication import GatewayApiKeyStore
from ai_provider_gateway.gateway.bootstrap import create_initial_key
from ai_provider_gateway.gateway.runtime import GatewayRuntimeSettings
from ai_provider_gateway.models.registry import (
    DiscoveredModel,
    DiscoveryState,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
    model_registry,
    provider_discovery_state,
)
from ai_provider_gateway.sidecars.cliproxy.client import (
    DiscoveredModel as SidecarDiscoveredModel,
    ModelDiscovery,
    SidecarApiKey,
    SidecarAuthenticationError,
    SidecarProtocolError,
    SidecarTransportError,
)
from ai_provider_gateway.sidecars.cliproxy.data_plane import CLIProxyAPIDataPlaneClient, SidecarUpstreamError

NOW = datetime(2026, 1, 1, tzinfo=UTC)

def record(canonical: str, *, available: bool = True, alias: str | None = None, provider: str | None = None) -> ModelRecord:
    provider_name, upstream = canonical.split("/", 1)
    return ModelRecord(canonical, provider or provider_name, upstream, upstream, alias, available, NOW, DiscoveryState.AVAILABLE if available else DiscoveryState.STALE, NOW)

class Registry:
    def __init__(self, models): self.models, self.refreshes = models, 0
    def list_models(self): return list(self.models)
    def resolve_model(self, identifier): return next((m for m in self.models if identifier in {m.canonical_id, m.alias}), None)

class DataPlane:
    def __init__(self, response=None, *, stream_lines=None, error=None):
        self.payloads, self.stream_lines, self.error, self.closed = [], stream_lines, error, False
        self.response = response or {"id": "chatcmpl-1", "object": "chat.completion", "model": "upstream", "choices": [], "usage": {"total_tokens": 2}}
    async def chat_completion(self, payload):
        self.payloads.append(payload)
        if self.error: raise self.error
        return dict(self.response)
    async def open_chat_completion_stream(self, payload):
        self.payloads.append(payload)
        if self.error: raise self.error
        owner = self
        class Response:
            async def aiter_lines(self):
                for line in owner.stream_lines or []: yield line
            async def aclose(self): owner.closed = True
        return Response()
    async def aclose(self): self.closed = True

def app_for(registry, data_plane, *, authorize=lambda token: object() if token == "good-key" else None, refresh=None):
    return create_app(GatewayService(registry=registry, data_plane=data_plane, authorize=authorize, refresh_models=refresh))

def client_for(app): return TestClient(app, client=("127.0.0.1", 12345))

def test_gateway_key_store_multiple_keys_prefix_verifier_only_revoke_and_safe_repr():
    engine = create_engine("sqlite:///:memory:")
    from ai_provider_gateway.gateway.authentication import gateway_api_keys
    gateway_api_keys.create(engine)
    secrets = iter(["a" * 40, "b" * 40])
    def hasher(secret): return "verifier:" + secret
    def verifier(encoded, supplied): return encoded == "verifier:" + supplied
    store = GatewayApiKeyStore(engine, hasher=hasher, verifier=verifier, secret_factory=lambda _: next(secrets))
    first, second = store.create("first"), store.create("second")
    assert first.secret == "a" * 40 and second.secret == "b" * 40
    assert first.secret not in repr(first)
    assert store.authenticate(first.secret).key_id == "first"
    assert store.authenticate("a" * 16 + "wrong") is None
    assert store.revoke("first") is True and store.authenticate(first.secret) is None
    with engine.connect() as connection: row = connection.execute(text("select prefix, verifier, active from gateway_api_keys where key_id='first'")).one()
    assert row[0] == "a" * 16 and row[1] == "verifier:" + ("a" * 40) and not row[2]

def test_phase3_migration_only_adds_gateway_api_keys_and_downgrades(tmp_path):
    db = tmp_path / "gateway.db"; env = os.environ.copy(); env["AIPG_DATABASE_URL"] = f"sqlite:///{db.as_posix()}"; root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0003_phase3_gateway_keys"], cwd=root, env=env, check=True, capture_output=True)
    engine = create_engine(f"sqlite:///{db.as_posix()}"); tables = set(inspect(engine).get_table_names())
    assert "gateway_api_keys" in tables
    assert {column["name"] for column in inspect(engine).get_columns("gateway_api_keys")} == {"key_id", "prefix", "verifier", "active", "created_at", "revoked_at"}
    assert not tables.intersection({"usage", "quota", "credentials", "requests", "request_payloads"})
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0002_phase2_model_registry"], cwd=root, env=env, check=True, capture_output=True)
    assert "gateway_api_keys" not in set(inspect(create_engine(f"sqlite:///{db.as_posix()}" )).get_table_names())

def test_unauthorized_gateway_requests_are_openai_errors_with_challenge():
    with client_for(app_for(Registry([record("antigravity/model")]), DataPlane())) as client:
        for headers in ({}, {"authorization": "Bearer wrong"}):
            response = client.get("/v1/models", headers=headers)
            assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_api_key" and response.json()["error"]["type"] == "authentication_error"
            assert response.headers.get("www-authenticate") == "Bearer"

def test_models_expose_canonical_openai_and_staleness_fields_and_alias():
    registry = Registry([record("antigravity/model", alias="fast"), record("antigravity/old", available=False)])
    with client_for(app_for(registry, DataPlane())) as client: response = client.get("/v1/models", headers={"authorization": "Bearer good-key"})
    assert response.status_code == 200; model = response.json()["data"][0]
    assert model["id"] == "antigravity/model" and model["object"] == "model" and model["owned_by"] == "antigravity" and model["alias"] == "fast" and model["available"] is True and model["discovery_state"] == "available"
    assert {"display_name", "upstream_model", "discovered_at", "last_success_at"}.issubset(model)

def test_canonical_and_alias_routes_rewrite_only_top_level_model_and_preserve_payload():
    registry = Registry([record("antigravity/upstream-model", alias="friendly")]); plane = DataPlane(response={"id": "x", "model": "upstream-model", "choices": [{"message": {"tool_calls": [{"function": {"arguments": "{}"}}]}}], "usage": {"total_tokens": 9}})
    original = {"model": "friendly", "messages": [{"role": "tool", "content": "result", "tool_call_id": "x"}], "tools": [{"type": "function", "function": {"name": "f"}}], "unknown": {"keep": [1, 2]}}
    with client_for(app_for(registry, plane)) as client: response = client.post("/v1/chat/completions", headers={"authorization": "Bearer good-key"}, json=original)
    assert response.status_code == 200 and plane.payloads[0]["model"] == "upstream-model"
    assert plane.payloads[0]["messages"] == original["messages"] and plane.payloads[0]["tools"] == original["tools"] and plane.payloads[0]["unknown"] == original["unknown"]
    assert response.json()["model"] == "antigravity/upstream-model" and response.json()["choices"][0]["message"]["tool_calls"] == plane.response["choices"][0]["message"]["tool_calls"]

def test_missing_or_unavailable_model_refreshes_once_then_returns_model_not_found():
    registry = Registry([record("antigravity/model", available=False)])
    async def refresh(): registry.refreshes += 1
    with client_for(app_for(registry, DataPlane(), refresh=refresh)) as client:
        response = client.post("/v1/chat/completions", headers={"authorization": "Bearer good-key"}, json={"model": "antigravity/missing"})
    assert response.status_code == 404 and response.json()["error"]["code"] == "model_not_found" and registry.refreshes == 1

def test_non_antigravity_provider_is_not_routed():
    plane = DataPlane()
    with client_for(app_for(Registry([record("other/model")]), plane)) as client:
        response = client.post("/v1/chat/completions", headers={"authorization": "Bearer good-key"}, json={"model": "other/model"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "provider_unavailable" and plane.payloads == []

def test_streaming_rewrites_each_chunk_model_preserves_tool_delta_usage_and_done_order():
    lines = [
        'data: {"id":"x","model":"upstream","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"a\\":"}}]}}]}',
        'data: {"id":"x","model":"upstream","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}],"finish_reason":null}],"usage":null}',
        'data: {"id":"x","model":"upstream","choices":[],"usage":{"total_tokens":3}}', "data: [DONE]",
    ]
    plane = DataPlane(stream_lines=lines)
    with client_for(app_for(Registry([record("antigravity/upstream", alias="a")]), plane)) as client:
        response = client.post("/v1/chat/completions", headers={"authorization": "Bearer good-key"}, json={"model": "a", "stream": True})
    body = response.text
    assert response.status_code == 200 and body.count('"model":"antigravity/upstream"') >= 2 and '"total_tokens":3' in body
    assert '"arguments":"{\\"a\\":"' in body and '"arguments":"1}"' in body and body.rfind("data: [DONE]") > body.rfind('"total_tokens":3') and plane.closed is True

@pytest.mark.parametrize("error,expected_status", [
    (SidecarAuthenticationError("safe"), 502), (SidecarTransportError("safe"), 503), (SidecarProtocolError("safe"), 502),
    (SidecarUpstreamError(400), 400), (SidecarUpstreamError(404), 404), (SidecarUpstreamError(429), 429), (SidecarUpstreamError(500), 500),
])
def test_sidecar_failures_map_to_safe_openai_errors(error, expected_status):
    with client_for(app_for(Registry([record("antigravity/model")]), DataPlane(error=error))) as client:
        response = client.post("/v1/chat/completions", headers={"authorization": "Bearer good-key"}, json={"model": "antigravity/model"})
    expected_type = "invalid_request_error" if expected_status in {400, 404} else "server_error"
    assert response.status_code == expected_status and "safe" not in response.text and response.json()["error"]["type"] == expected_type

def test_data_plane_maps_non_json_error_content_and_connection_failure_without_leaking_body():
    async def scenario():
        async def handler(request): return httpx.Response(502, content=b"credential=super-secret", headers={"content-type": "text/plain"})
        async_client = httpx.AsyncClient(base_url="http://127.0.0.1:8317", transport=httpx.MockTransport(handler), follow_redirects=False)
        plane = CLIProxyAPIDataPlaneClient(base_url="http://127.0.0.1:8317", api_key=SidecarApiKey("api"), client=async_client)
        with pytest.raises(SidecarUpstreamError) as caught: await plane.chat_completion({"model": "x"})
        assert caught.value.status_code == 502 and "super-secret" not in str(caught.value)
        await async_client.aclose()
        def fail(request): raise httpx.ConnectError("network-secret")
        async_client = httpx.AsyncClient(base_url="http://127.0.0.1:8317", transport=httpx.MockTransport(fail), follow_redirects=False)
        plane = CLIProxyAPIDataPlaneClient(base_url="http://127.0.0.1:8317", api_key=SidecarApiKey("api"), client=async_client)
        with pytest.raises(SidecarTransportError) as caught: await plane.chat_completion({"model": "x"})
        assert "network-secret" not in str(caught.value)
        await async_client.aclose()
    import asyncio
    asyncio.run(scenario())

def test_sidecar_429_preserves_safe_retry_after_metadata():
    async def scenario():
        async def handler(request):
            return httpx.Response(429, json={"error": {"type": "rate_limit_error", "param": "model", "code": "quota_exhausted", "message": "secret"}}, headers={"retry-after": "7"})
        transport = httpx.MockTransport(handler)
        async_client = httpx.AsyncClient(base_url="http://127.0.0.1:8317", transport=transport, follow_redirects=False)
        plane = CLIProxyAPIDataPlaneClient(base_url="http://127.0.0.1:8317", api_key=SidecarApiKey("api"), client=async_client)
        with pytest.raises(SidecarUpstreamError) as caught: await plane.chat_completion({"model": "x"})
        assert caught.value.retry_after == "7" and caught.value.error_type == "rate_limit_error" and caught.value.param == "model" and caught.value.code == "quota_exhausted"
        await async_client.aclose()
    import asyncio
    asyncio.run(scenario())

def test_runtime_factory_reads_only_gateway_database_url_base_url_and_sidecar_key(monkeypatch):
    monkeypatch.setenv("AIPG_DATABASE_URL", "sqlite:///:memory:"); monkeypatch.setenv("AIPG_SIDECAR_API_KEY", "sidecar-only")
    monkeypatch.delenv("AIPG_MANAGEMENT_KEY", raising=False); monkeypatch.delenv("AIPG_ADMIN_PASSWORD_VERIFIER", raising=False)
    settings = GatewayRuntimeSettings.from_env()
    assert settings.database_url == "sqlite:///:memory:" and settings.sidecar_base_url == "http://127.0.0.1:8317" and settings.sidecar_api_key == "sidecar-only"
    assert "management" not in repr(settings).lower()

def test_phase4_endpoints_and_responses_api_do_not_exist():
    with client_for(create_app()) as client:
        assert client.get("/v1/responses").status_code == 404 and client.post("/v1/responses").status_code == 404 and client.get("/api/control/keys").status_code == 404


def test_model_registry_prefers_canonical_id_then_alias_and_rejects_invalid_lookup():
    engine = create_engine("sqlite:///:memory:")
    model_registry.create(engine)
    provider_discovery_state.create(engine)
    registry = ModelRegistry(engine)
    registry.refresh_success("antigravity", [DiscoveredModel("model-a"), DiscoveredModel("model-b")])
    registry.set_alias("antigravity/model-b", "antigravity/model-a")

    assert registry.resolve_model("antigravity/model-a").upstream_id == "model-a"
    assert registry.resolve_model("antigravity/model-b").upstream_id == "model-b"
    with pytest.raises(ModelRegistryError):
        registry.resolve_model("bad\nidentifier")


def test_upstream_404_refreshes_then_returns_model_not_found():
    registry = Registry([record("antigravity/model")])
    plane = DataPlane(error=SidecarUpstreamError(404))

    async def refresh():
        registry.refreshes += 1

    with client_for(app_for(registry, plane, refresh=refresh)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer good-key"},
            json={"model": "antigravity/model"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    assert registry.refreshes == 1


def test_data_plane_200_non_json_is_protocol_error():
    async def scenario():
        async def handler(request):
            return httpx.Response(200, content=b"not-json", headers={"content-type": "text/plain"})

        http_client = httpx.AsyncClient(
            base_url="http://127.0.0.1:8317",
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        plane = CLIProxyAPIDataPlaneClient(
            base_url="http://127.0.0.1:8317",
            api_key=SidecarApiKey("api"),
            client=http_client,
        )
        with pytest.raises(SidecarProtocolError):
            await plane.chat_completion({"model": "x"})
        await http_client.aclose()

    import asyncio

    asyncio.run(scenario())


def test_sse_wrong_content_type_is_protocol_error_and_closes_response():
    async def scenario():
        async def handler(request):
            return httpx.Response(200, content=b"data: {}\n", headers={"content-type": "application/json"})

        http_client = httpx.AsyncClient(
            base_url="http://127.0.0.1:8317",
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        plane = CLIProxyAPIDataPlaneClient(
            base_url="http://127.0.0.1:8317",
            api_key=SidecarApiKey("api"),
            client=http_client,
        )
        with pytest.raises(SidecarProtocolError):
            await plane.open_chat_completion_stream({"model": "x"})
        await http_client.aclose()

    import asyncio

    asyncio.run(scenario())


def test_streaming_429_json_error_is_safe_and_closes_response():
    async def scenario():
        captured = {}

        async def handler(request):
            response = httpx.Response(
                429,
                json={
                    "error": {
                        "type": "rate_limit_error",
                        "param": "model",
                        "code": "quota_exhausted",
                        "message": "sensitive upstream detail",
                    }
                },
                headers={"content-type": "application/json", "retry-after": "11"},
            )
            captured["response"] = response
            return response

        http_client = httpx.AsyncClient(
            base_url="http://127.0.0.1:8317",
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        plane = CLIProxyAPIDataPlaneClient(
            base_url="http://127.0.0.1:8317",
            api_key=SidecarApiKey("api"),
            client=http_client,
        )
        with pytest.raises(SidecarUpstreamError) as caught:
            await plane.open_chat_completion_stream({"model": "x"})
        error = caught.value
        assert error.status_code == 429
        assert error.error_type == "rate_limit_error"
        assert error.param == "model"
        assert error.code == "quota_exhausted"
        assert error.retry_after == "11"
        assert captured["response"].is_closed is True
        assert "sensitive upstream detail" not in str(error)
        await http_client.aclose()

    import asyncio

    asyncio.run(scenario())


def test_closing_unconsumed_gateway_stream_closes_upstream_response():
    from ai_provider_gateway.gateway.app import _stream_response

    async def scenario():
        plane = DataPlane(stream_lines=["data: {\"model\":\"upstream\"}"])
        service = GatewayService(
            registry=Registry([record("antigravity/upstream")]),
            data_plane=plane,
            authorize=lambda token: object(),
        )
        response = await _stream_response(service, {"model": "upstream", "stream": True}, "antigravity/upstream")
        await response.body_iterator.aclose()
        assert plane.closed is True

    import asyncio

    asyncio.run(scenario())


def test_asgi_send_disconnect_closes_upstream_stream():
    from ai_provider_gateway.gateway.app import _stream_response

    async def scenario():
        plane = DataPlane(
            stream_lines=[
                'data: {"id":"x","model":"upstream","choices":[]}',
            ]
        )
        service = GatewayService(
            registry=Registry([record("antigravity/upstream")]),
            data_plane=plane,
            authorize=lambda token: object(),
        )
        response = await _stream_response(
            service,
            {"model": "upstream", "stream": True},
            "antigravity/upstream",
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
        }
        sent = []

        received_request = False

        async def receive():
            nonlocal received_request
            if not received_request:
                received_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                raise OSError("client disconnected")

        with pytest.raises(OSError, match="client disconnected"):
            await response(scope, receive, send)
        assert sent[0]["type"] == "http.response.start"
        assert plane.closed is True

    import asyncio

    asyncio.run(scenario())


def test_gateway_429_response_preserves_safe_metadata_and_retry_after():
    error = SidecarUpstreamError(
        429,
        "7",
        error_type="rate_limit_error",
        param="model",
        code="quota_exhausted",
    )
    with client_for(app_for(Registry([record("antigravity/model")]), DataPlane(error=error))) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer good-key"},
            json={"model": "antigravity/model"},
        )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.json()["error"] == {
        "message": "The provider request failed.",
        "type": "rate_limit_error",
        "param": "model",
        "code": "quota_exhausted",
    }


def test_bootstrap_returns_full_secret_once_with_fake_secret_and_hasher():
    engine = create_engine("sqlite:///:memory:")
    from ai_provider_gateway.gateway.authentication import gateway_api_keys

    gateway_api_keys.create(engine)
    calls = []
    store = GatewayApiKeyStore(
        engine,
        hasher=lambda value: calls.append(("hash", value)) or "fake-verifier",
        verifier=lambda encoded, supplied: encoded == "fake-verifier" and supplied.startswith("secret-once-"),
        secret_factory=lambda length: calls.append(("secret", length)) or "secret-once-" + ("0" * 28),
    )
    issued = create_initial_key(store, "bootstrap")
    assert issued.secret == "secret-once-" + ("0" * 28)
    assert calls == [("secret", 32), ("hash", issued.secret)]
    assert store.authenticate(issued.secret).key_id == "bootstrap"


def test_gateway_runtime_factory_and_launcher_are_wired_to_loopback_factory(monkeypatch, tmp_path):
    import ai_provider_gateway.gateway.runtime_app as runtime_app

    monkeypatch.setenv("AIPG_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("AIPG_SIDECAR_API_KEY", "sidecar-only")
    monkeypatch.setenv("AIPG_SIDECAR_BASE_URL", "http://127.0.0.1:8317")
    monkeypatch.setattr(runtime_app, "build_gateway_service", lambda settings: GatewayService(
        registry=Registry([]), data_plane=DataPlane(), authorize=lambda token: None,
    ))
    application = runtime_app.create_runtime_app()
    assert any(route.path == "/v1/models" for route in application.routes)

    script = Path("scripts/dev/run-gateway.ps1").read_text(encoding="utf-8")
    assert "ai_provider_gateway.gateway.runtime_app:create_runtime_app" in script
    assert "--factory" in script and "--host 127.0.0.1" in script


def test_gateway_drops_untrusted_error_metadata_and_retry_after():
    async def handler(request):
        return httpx.Response(
            429,
            json={
                "error": {
                    "type": "credential-secret",
                    "param": "account-secret",
                    "code": "internal-secret-code",
                    "message": "secret body",
                }
            },
            headers={"content-type": "application/json", "retry-after": "secret-retry-value"},
        )

    async_client = httpx.AsyncClient(
        base_url="http://127.0.0.1:8317",
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    data_plane = CLIProxyAPIDataPlaneClient(
        base_url="http://127.0.0.1:8317",
        api_key=SidecarApiKey("api"),
        client=async_client,
    )
    with client_for(app_for(Registry([record("antigravity/model")]), data_plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer good-key"},
            json={"model": "antigravity/model"},
        )
    assert response.status_code == 429
    assert "retry-after" not in response.headers
    body = response.json()["error"]
    assert body["type"] == "server_error"
    assert body["param"] is None
    assert body["code"] == "provider_error"
    assert "secret" not in response.text
    import asyncio

    asyncio.run(async_client.aclose())


def test_gateway_accepts_allowlisted_metadata_and_retry_after_forms():
    for retry_after in ("11", "Wed, 21 Oct 2015 07:28:00 GMT"):
        error = SidecarUpstreamError(
            429,
            retry_after,
            error_type="rate_limit_error",
            param="model",
            code="quota_exhausted",
        )
        with client_for(app_for(Registry([record("antigravity/model")]), DataPlane(error=error))) as client:
            response = client.post(
                "/v1/chat/completions",
                headers={"authorization": "Bearer good-key"},
                json={"model": "antigravity/model"},
            )
        assert response.headers["retry-after"] == retry_after
        assert response.json()["error"]["type"] == "rate_limit_error"
        assert response.json()["error"]["param"] == "model"
        assert response.json()["error"]["code"] == "quota_exhausted"


def test_antigravity_discovery_preserves_successful_registry_after_transport_failure():
    from ai_provider_gateway.providers.antigravity.discovery import AntigravityDiscovery

    class DiscoveryClient:
        def __init__(self):
            self.failure = None

        async def discover_models(self):
            if self.failure:
                raise self.failure
            return ModelDiscovery((SidecarDiscoveredModel("dynamic-model"),))

    async def scenario():
        engine = create_engine("sqlite:///:memory:")
        model_registry.create(engine)
        provider_discovery_state.create(engine)
        registry = ModelRegistry(engine)
        client = DiscoveryClient()
        discovery = AntigravityDiscovery(client, registry)
        success = await discovery.refresh()
        success_time = success.last_success_at
        client.failure = SidecarTransportError("hidden transport detail")
        failure = await discovery.refresh()
        assert success.state == DiscoveryState.AVAILABLE
        assert failure.state == DiscoveryState.STALE
        assert failure.error_category == "sidecar_unavailable"
        assert failure.last_success_at == success_time
        assert failure.models[0].canonical_id == "antigravity/dynamic-model"
        assert failure.models[0].available is True
        assert registry.discovery_state("antigravity").last_success_at == success_time

    import asyncio

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure,category",
    [
        (SidecarProtocolError("hidden"), "invalid_sidecar_response"),
        (None, "empty_catalog"),
    ],
)
def test_antigravity_discovery_failure_categories_are_stale_and_safe(failure, category):
    from ai_provider_gateway.providers.antigravity.discovery import AntigravityDiscovery

    class DiscoveryClient:
        async def discover_models(self):
            if failure is not None:
                raise failure
            return ModelDiscovery(())

    async def scenario():
        engine = create_engine("sqlite:///:memory:")
        model_registry.create(engine)
        provider_discovery_state.create(engine)
        outcome = await AntigravityDiscovery(
            DiscoveryClient(), ModelRegistry(engine)
        ).refresh()
        assert outcome.state == DiscoveryState.STALE
        assert outcome.error_category == category
        assert outcome.models == ()

    import asyncio

    asyncio.run(scenario())


def test_overlong_gateway_api_key_is_rejected_without_verifier_call():
    engine = create_engine("sqlite:///:memory:")
    from ai_provider_gateway.gateway.authentication import gateway_api_keys

    gateway_api_keys.create(engine)
    calls = []
    store = GatewayApiKeyStore(
        engine,
        verifier=lambda encoded, supplied: calls.append((encoded, supplied)) or True,
    )
    assert store.authenticate("x" * 513) is None
    assert calls == []
