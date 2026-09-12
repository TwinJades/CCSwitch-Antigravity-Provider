from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from ai_provider_gateway.control.app import create_app as create_control_app
from ai_provider_gateway.control.phase2 import Phase2Control
from ai_provider_gateway.control.phase5 import Phase5Control
from ai_provider_gateway.gateway.app import GatewayService, create_app as create_gateway_app
from ai_provider_gateway.models.registry import model_registry, provider_discovery_state
from ai_provider_gateway.portable import WindowsDataProtector
from ai_provider_gateway.providers.openai_compatible import (
    CustomProviderError, CustomProviderStore, OpenAICompatibleDataPlane,
    OpenAICompatibleProviderError,
    OpenAICompatibleProvider, ProviderHeader, ProviderModel,
)
from ai_provider_gateway.providers.openai_compatible.storage import openai_compatible_providers


class Protector:
    def protect(self, value: bytes) -> bytes: return b"dpapi:" + value[::-1]
    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(b"dpapi:"): raise ValueError("bad protected value")
        return value[6:][::-1]


class Phase2:
    async def aclose(self): pass


def _store(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'gateway.db').as_posix()}")
    model_registry.create(engine); provider_discovery_state.create(engine); openai_compatible_providers.create(engine)
    return engine, CustomProviderStore(engine, Protector())


def _provider(**overrides):
    values = dict(provider_id="school", display_name="School API", base_url="https://api.school.example/v1", api_key="secret-key", models=(ProviderModel("chat-1", "Chat One"),), headers=(ProviderHeader("X-School", "header-secret"),))
    values.update(overrides)
    return OpenAICompatibleProvider(**values)


def test_storage_protects_secrets_refreshes_models_and_rejects_unsafe_inputs(tmp_path):
    engine, store = _store(tmp_path)
    metadata = store.replace(_provider())
    assert metadata.provider_id == "school" and metadata.api_key_configured and metadata.header_names == ("X-School",)
    with engine.connect() as connection:
        row = connection.execute(text("select api_key_protected, headers_protected from openai_compatible_providers")).one()
    assert b"secret-key" not in row[0] and b"header-secret" not in row[1]
    loaded = store.load("school")
    assert loaded and loaded.api_key == "secret-key" and loaded.models[0].model_id == "chat-1"
    assert store._registry.resolve_model("school/chat-1").upstream_id == "chat-1"
    for provider in (_provider(base_url="http://api.school.example/v1"), _provider(base_url="https://api.school.example/v1?x=1"), _provider(headers=(ProviderHeader("Host", "x"),)), _provider(headers=(ProviderHeader("User-Agent", "x"),)), _provider(headers=(ProviderHeader("X-Test", "a\r\nb"),)), _provider(provider_id="antigravity")):
        try:
            store.replace(provider)
        except CustomProviderError:
            pass
        else:
            raise AssertionError("unsafe provider was accepted")


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is required")
def test_custom_provider_store_round_trips_with_current_user_dpapi(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'gateway.db').as_posix()}")
    model_registry.create(engine)
    provider_discovery_state.create(engine)
    openai_compatible_providers.create(engine)
    store = CustomProviderStore(engine, WindowsDataProtector())
    store.replace(_provider())
    loaded = store.load("school")
    assert loaded is not None
    assert loaded.api_key == "secret-key"
    assert loaded.headers == (ProviderHeader("X-School", "header-secret"),)


def test_provider_configuration_and_model_catalogue_roll_back_together(tmp_path):
    engine, store = _store(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("""
            create trigger reject_provider_insert
            before insert on openai_compatible_providers
            begin select raise(abort, 'synthetic provider write failure'); end
        """))
    with pytest.raises(CustomProviderError):
        store.replace(_provider())
    assert store._registry.list_models("school") == []
    assert store._registry.discovery_state("school") is None


def test_control_api_requires_auth_and_never_echoes_secrets(tmp_path):
    _, store = _store(tmp_path)
    class FailedProbe:
        async def discover_models(self):
            raise OpenAICompatibleProviderError("provider_unavailable")
        async def aclose(self): pass
    app = create_control_app(
        phase2=Phase2(),
        authorize_control=lambda request: request.headers.get("x-admin") == "yes",
        custom_providers=store,
        custom_provider_probe_factory=lambda provider: FailedProbe(),
    )
    payload = {"provider_id": "school", "display_name": "School API", "base_url": "https://api.school.example/v1", "api_key": "api-secret", "models": [{"model_id": "chat"}], "headers": [{"name": "X-School", "value": "header-secret"}]}
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        assert client.post("/api/control/providers/openai-compatible", json=payload).status_code == 403
        response = client.post("/api/control/providers/openai-compatible", headers={"x-admin": "yes"}, json=payload)
        assert response.status_code == 201
        assert response.json() == {
            "provider_id": "school", "display_name": "School API",
            "base_url": "https://api.school.example/v1", "api_key_configured": True,
            "header_names": ["X-School"], "model_count": 1,
            "verification_status": "connection_failed",
            "verification_error": "provider_unavailable",
        }
        listed = client.get("/api/control/providers/openai-compatible", headers={"x-admin": "yes"})
        assert "api-secret" not in listed.text and "header-secret" not in listed.text
        assert listed.headers["cache-control"] == "no-store"


def test_control_api_saves_first_then_syncs_models_without_chat_request(tmp_path):
    _, store = _store(tmp_path)
    calls = []
    class Probe:
        async def discover_models(self):
            calls.append("models")
            return (ProviderModel("live-a"), ProviderModel("live-b"))
        async def aclose(self): calls.append("closed")
    app = create_control_app(
        phase2=Phase2(), authorize_control=lambda request: True,
        custom_providers=store,
        custom_provider_probe_factory=lambda provider: Probe(),
    )
    payload = {
        "provider_id": "school", "display_name": "School API",
        "base_url": "https://api.school.example/v1", "api_key": "api-secret",
        "models": [{"model_id": "manual"}], "headers": [],
    }
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        response = client.post(
            "/api/control/providers/openai-compatible",
            headers={"origin": "http://testserver"}, json=payload,
        )
    assert response.status_code == 201
    assert response.json()["verification_status"] == "verified"
    assert response.json()["model_count"] == 2
    assert calls == ["models", "closed"]
    assert [item.canonical_id for item in store._registry.list_models() if item.available] == [
        "school/live-a", "school/live-b"
    ]


def test_control_api_discovers_models_without_requiring_manual_entries(tmp_path):
    _, store = _store(tmp_path)

    class Probe:
        async def discover_models(self):
            return (ProviderModel("live-from-provider"),)

        async def aclose(self):
            pass

    app = create_control_app(
        phase2=Phase2(), authorize_control=lambda request: True,
        custom_providers=store,
        custom_provider_probe_factory=lambda provider: Probe(),
    )
    payload = {
        "provider_id": "school", "display_name": "School API",
        "base_url": "https://api.school.example/v1", "api_key": "api-secret",
        "headers": [],
    }
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        response = client.post("/api/control/providers/openai-compatible", json=payload)
    assert response.status_code == 201
    assert response.json()["verification_status"] == "verified"
    assert response.json()["model_count"] == 1
    assert store._registry.resolve_model("school/live-from-provider").upstream_id == "live-from-provider"


def test_control_api_keeps_provider_when_discovery_fails_without_manual_models(tmp_path):
    _, store = _store(tmp_path)

    class FailedProbe:
        async def discover_models(self):
            raise OpenAICompatibleProviderError("provider_connect_error")

        async def aclose(self):
            pass

    app = create_control_app(
        phase2=Phase2(), authorize_control=lambda request: True,
        custom_providers=store,
        custom_provider_probe_factory=lambda provider: FailedProbe(),
    )
    payload = {
        "provider_id": "school", "display_name": "School API",
        "base_url": "https://api.school.example/v1", "api_key": "api-secret",
        "models": [], "headers": [],
    }
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        response = client.post("/api/control/providers/openai-compatible", json=payload)
    assert response.status_code == 201
    assert response.json()["verification_status"] == "connection_failed"
    assert response.json()["model_count"] == 0
    assert response.json()["api_key_configured"] is True
    assert store.load("school") is not None


def test_control_api_retains_existing_api_key_when_secret_field_is_blank(tmp_path):
    _, store = _store(tmp_path)
    seen_keys = []

    class FailedProbe:
        def __init__(self, provider):
            seen_keys.append(provider.api_key)

        async def discover_models(self):
            raise OpenAICompatibleProviderError("provider_connect_error")

        async def aclose(self):
            pass

    app = create_control_app(
        phase2=Phase2(),
        authorize_control=lambda request: True,
        custom_providers=store,
        custom_provider_probe_factory=FailedProbe,
    )
    payload = {
        "provider_id": "school", "display_name": "School API",
        "base_url": "https://api.school.example/v1", "api_key": "first-secret",
        "models": [{"model_id": "manual"}], "headers": [],
    }
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        assert client.post("/api/control/providers/openai-compatible", json=payload).status_code == 201
        payload["api_key"] = None
        response = client.post("/api/control/providers/openai-compatible", json=payload)
    assert response.status_code == 201
    assert response.json()["api_key_configured"] is True
    assert seen_keys == ["first-secret", "first-secret"]
    assert store.load("school").api_key == "first-secret"


def test_adapter_sends_safe_auth_and_forwards_json_and_sse():
    async def scenario():
        seen = []
        async def handler(request):
            seen.append(request)
            if request.headers.get("accept") == "text/event-stream":
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b'data: {"model":"chat"}\n\ndata: [DONE]\n')
            return httpx.Response(200, json={"model": "chat", "choices": []})
        client = httpx.AsyncClient(base_url="https://api.school.example/v1", transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False)
        plane = OpenAICompatibleDataPlane(_provider(), client=client)
        assert (await plane.chat_completion({"model": "chat"}))["model"] == "chat"
        response = await plane.open_chat_completion_stream({"model": "chat", "stream": True})
        assert "[DONE]" in "\n".join([line async for line in response.aiter_lines()])
        assert seen[0].headers["authorization"] == "Bearer secret-key" and seen[0].headers["x-school"] == "header-secret"
        await response.aclose(); await client.aclose()
    asyncio.run(scenario())


def test_adapter_discovers_standard_models_without_chat_request():
    async def scenario():
        seen = []
        async def handler(request):
            seen.append((request.method, request.url.path))
            return httpx.Response(200, json={"data": [{"id": "live-a"}, {"id": "live-b"}]})
        client = httpx.AsyncClient(
            base_url="https://api.school.example/v1",
            transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False,
        )
        plane = OpenAICompatibleDataPlane(_provider(), client=client)
        models = await plane.discover_models()
        await client.aclose()
        return models, seen
    models, seen = asyncio.run(scenario())
    assert [item.model_id for item in models] == ["live-a", "live-b"]
    assert seen == [("GET", "/v1/models")]


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (401, "provider_authentication_failed"),
        (403, "provider_access_denied"),
        (404, "provider_endpoint_not_found"),
        (429, "provider_rate_limited"),
        (500, "provider_upstream_error"),
    ],
)
def test_adapter_classifies_safe_upstream_status_without_response_content(status, category):
    async def scenario():
        async def handler(request):
            return httpx.Response(status, json={"secret": "must-not-escape"})

        client = httpx.AsyncClient(
            base_url="https://api.school.example/v1",
            transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False,
        )
        plane = OpenAICompatibleDataPlane(_provider(), client=client)
        with pytest.raises(OpenAICompatibleProviderError) as caught:
            await plane.discover_models()
        await client.aclose()
        return caught.value

    error = asyncio.run(scenario())
    assert error.category == category
    assert error.status_code == status
    assert "must-not-escape" not in str(error)


def test_adapter_classifies_connection_failure_without_leaking_exception_text():
    async def scenario():
        async def handler(request):
            raise httpx.ConnectError("sensitive transport detail", request=request)

        client = httpx.AsyncClient(
            base_url="https://api.school.example/v1",
            transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False,
        )
        plane = OpenAICompatibleDataPlane(_provider(), client=client)
        with pytest.raises(OpenAICompatibleProviderError) as caught:
            await plane.discover_models()
        await client.aclose()
        return caught.value

    error = asyncio.run(scenario())
    assert error.category == "provider_connect_error"
    assert "sensitive transport detail" not in str(error)


def test_gateway_routes_custom_models_and_preserves_antigravity(tmp_path, monkeypatch):
    _, store = _store(tmp_path); store.replace(_provider())
    class Registry:
        def list_models(self): return store._registry.list_models()
        def resolve_model(self, value): return store._registry.resolve_model(value)
    class Sidecar:
        async def aclose(self): pass
        async def chat_completion(self, payload): return {"model": payload["model"], "choices": []}
    class FakeCustom:
        payloads = []
        def __init__(self, provider): self.provider = provider
        async def aclose(self): pass
        async def chat_completion(self, payload):
            self.payloads.append(payload); return {"model": payload["model"], "choices": []}
    import importlib
    gateway_module = importlib.import_module("ai_provider_gateway.gateway.app")
    monkeypatch.setattr(gateway_module, "OpenAICompatibleDataPlane", FakeCustom)
    service = GatewayService(registry=Registry(), data_plane=Sidecar(), authorize=lambda token: object() if token == "ok" else None, custom_providers=store)
    with TestClient(create_gateway_app(service), client=("127.0.0.1", 12345)) as client:
        response = client.post("/v1/chat/completions", headers={"authorization": "Bearer ok"}, json={"model": "school/chat-1", "messages": []})
    assert response.status_code == 200 and response.json()["model"] == "school/chat-1"
    assert FakeCustom.payloads == [{"model": "chat-1", "messages": []}]


def test_control_and_harness_configs_use_the_aggregated_provider_catalogue(tmp_path):
    _, store = _store(tmp_path)
    store.replace(_provider())
    phase2 = Phase2Control(
        lifecycle=object(), client=object(), health_checker=object(),
        discovery=object(), registry=store._registry,
    )
    assert [item.canonical_id for item in phase2.current_models() if item.available] == [
        "school/chat-1"
    ]
    config = Phase5Control(phase2=phase2).opencode_config(
        default_model="school/chat-1"
    )
    assert config["model"] == "personal-ai-gateway/school/chat-1"


def test_gateway_custom_provider_stream_rewrites_model_and_closes_resources(tmp_path, monkeypatch):
    _, store = _store(tmp_path)
    store.replace(_provider())

    class Registry:
        def list_models(self): return store._registry.list_models()
        def resolve_model(self, value): return store._registry.resolve_model(value)

    class Sidecar:
        async def aclose(self): pass

    class FakeCustom:
        payloads = []
        upstream_closed = False
        client_closed = False
        def __init__(self, provider): self.provider = provider
        async def open_chat_completion_stream(self, payload):
            self.payloads.append(payload)
            class Response:
                async def aiter_lines(self):
                    yield 'data: {"model":"chat-1","choices":[]}'
                    yield "data: [DONE]"
                async def aclose(self): FakeCustom.upstream_closed = True
            return Response()
        async def aclose(self): FakeCustom.client_closed = True

    import importlib
    gateway_module = importlib.import_module("ai_provider_gateway.gateway.app")
    monkeypatch.setattr(gateway_module, "OpenAICompatibleDataPlane", FakeCustom)
    service = GatewayService(
        registry=Registry(), data_plane=Sidecar(),
        authorize=lambda token: object() if token == "ok" else None,
        custom_providers=store,
    )
    with TestClient(create_gateway_app(service), client=("127.0.0.1", 12345)) as client:
        response = client.post(
            "/v1/chat/completions", headers={"authorization": "Bearer ok"},
            json={"model": "school/chat-1", "messages": [], "stream": True},
        )
    assert response.status_code == 200
    assert '"model":"school/chat-1"' in response.text
    assert response.text.rstrip().endswith("data: [DONE]")
    assert FakeCustom.payloads == [{"model": "chat-1", "messages": [], "stream": True}]
    assert FakeCustom.upstream_closed is True
    assert FakeCustom.client_closed is True
