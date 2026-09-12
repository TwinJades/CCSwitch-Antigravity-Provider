from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from ai_provider_gateway.control.app import create_app as create_control_app
from ai_provider_gateway.control.phase4 import Phase4Control
from ai_provider_gateway.gateway.app import GatewayService, create_app as create_gateway_app
from ai_provider_gateway.health import HealthRecord, HealthState, HealthStore
from ai_provider_gateway.models.registry import DiscoveryState, ModelRecord
from ai_provider_gateway.quota import QuotaCache, QuotaSnapshot, QuotaStatus, QuotaStore
from ai_provider_gateway.quota.cache import QuotaStoreError
from ai_provider_gateway.sidecars.cliproxy.client import SidecarProtocolError, SidecarTransportError
from ai_provider_gateway.sidecars.cliproxy.health import SidecarHealthState
from ai_provider_gateway.sidecars.cliproxy.data_plane import SidecarUpstreamError
from ai_provider_gateway.usage import (
    TokenSource,
    UsageFilter,
    UsageRecord,
    UsageStatus,
    UsageStore,
)
from ai_provider_gateway.usage.storage import UsageStoreError


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def sqlite_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def model() -> ModelRecord:
    return ModelRecord(
        "antigravity/model",
        "antigravity",
        "model",
        "Model",
        None,
        True,
        NOW,
        DiscoveryState.AVAILABLE,
        NOW,
    )


class Registry:
    def list_models(self):
        return [model()]

    def resolve_model(self, identifier):
        return model() if identifier == "antigravity/model" else None


class DataPlane:
    def __init__(self, *, error=None, stream_lines=None):
        self.error = error
        self.stream_lines = stream_lines
        self.payloads = []
        self.closed = False

    async def chat_completion(self, payload):
        self.payloads.append(payload)
        if self.error:
            raise self.error
        return {
            "id": "completion-1",
            "object": "chat.completion",
            "model": "model",
            "choices": [],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }

    async def open_chat_completion_stream(self, payload):
        self.payloads.append(payload)
        if self.error:
            raise self.error
        owner = self

        class Response:
            async def aiter_lines(self):
                for line in owner.stream_lines or []:
                    yield line

            async def aclose(self):
                owner.closed = True

        return Response()

    async def aclose(self):
        self.closed = True


def gateway_app(engine, data_plane):
    from ai_provider_gateway.gateway.app import GatewayService

    recorder = UsageStore(engine)
    service = GatewayService(
        registry=Registry(),
        data_plane=data_plane,
        authorize=lambda token: SimpleNamespace(key_id="client-1") if token == "good" else None,
        usage_recorder=recorder,
    )
    return create_gateway_app(service), recorder


def create_phase4_tables(engine):
    from ai_provider_gateway.health.storage import provider_health_records
    from ai_provider_gateway.quota.cache import quota_snapshots
    from ai_provider_gateway.usage.storage import usage_records

    usage_records.create(engine)
    quota_snapshots.create(engine)
    provider_health_records.create(engine)


def test_phase4_migration_creates_and_drops_only_three_statistics_tables(tmp_path):
    db = tmp_path / "phase4.db"
    env = os.environ.copy()
    env["AIPG_DATABASE_URL"] = f"sqlite:///{db.as_posix()}"
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0004_phase4_statistics"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
    )
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    tables = set(inspect(engine).get_table_names())
    assert {"usage_records", "quota_snapshots", "provider_health_records"}.issubset(tables)
    assert {column["name"] for column in inspect(engine).get_columns("usage_records")} == {
        "request_id", "timestamp", "client_key_id", "provider", "canonical_model", "stream",
        "status", "latency_ms", "input_tokens", "output_tokens", "total_tokens",
        "token_source", "error_category",
    }
    assert {column["name"] for column in inspect(engine).get_columns("quota_snapshots")} == {
        "provider", "bucket_id", "status", "group_display", "bucket_display", "window",
        "remaining_ratio", "used_ratio", "reset_at", "source", "updated_at", "error_category",
    }
    assert {column["name"] for column in inspect(engine).get_columns("provider_health_records")} == {
        "id", "provider", "state", "checked_at", "latency_ms", "error_category",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0003_phase3_gateway_keys"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
    )
    assert not {"usage_records", "quota_snapshots", "provider_health_records"}.intersection(
        inspect(create_engine(f"sqlite:///{db.as_posix()}" )).get_table_names()
    )


def test_usage_store_filters_aggregates_unknown_tokens_and_purges_retention():
    engine = sqlite_engine()
    create_phase4_tables(engine)
    store = UsageStore(engine)
    store.record(UsageRecord("old", NOW - timedelta(days=31), "client-a", "antigravity", "antigravity/model", False, UsageStatus.SUCCEEDED, 10, 2, 3, 5, TokenSource.UPSTREAM_REPORTED))
    store.record(UsageRecord("new", NOW, "client-a", "antigravity", "antigravity/model", False, UsageStatus.FAILED, 20, token_source=TokenSource.UNKNOWN, error_category="provider_error"))
    store.record(UsageRecord("other", NOW, "client-b", "other", "other/model", True, UsageStatus.ABORTED, 30, token_source=TokenSource.UNKNOWN))
    summary = store.aggregate(UsageFilter(provider="antigravity", client_key_id="client-a"))
    assert summary.request_count == 2
    assert summary.succeeded_count == 1 and summary.failed_count == 1 and summary.aborted_count == 0
    assert summary.total_tokens == 5 and summary.unknown_token_count == 1
    assert len(store.query(UsageFilter(model="antigravity/model"))) == 2
    assert store.purge(older_than=NOW - timedelta(days=30)) == 1


def test_quota_cache_ttl_force_throttle_and_unknown_on_probe_failure():
    engine = sqlite_engine()
    create_phase4_tables(engine)
    calls = []

    class Probe:
        async def probe(self, provider):
            calls.append(provider)
            raise RuntimeError("quota unavailable")

    async def scenario():
        cache = QuotaCache(QuotaStore(engine), Probe(), ttl=timedelta(minutes=5), min_refresh_interval=timedelta(minutes=1))
        first = await cache.get_or_refresh("antigravity")
        second = await cache.get_or_refresh("antigravity", force=True)
        assert first[0].status is QuotaStatus.UNKNOWN
        assert second[0].status is QuotaStatus.UNKNOWN
        assert len(calls) == 1
        assert second[0].error_category == "probe_failed"

    asyncio.run(scenario())


def test_health_store_is_independent_and_validates_state():
    engine = sqlite_engine()
    create_phase4_tables(engine)
    store = HealthStore(engine)
    item = store.record(HealthRecord("antigravity", HealthState.UNAVAILABLE, NOW, 12, "transport"))
    assert store.latest("antigravity") == item


def test_usage_token_source_integrity_rejects_unknown_values_and_missing_reported_values():
    engine = sqlite_engine()
    create_phase4_tables(engine)
    store = UsageStore(engine)
    with pytest.raises(UsageStoreError):
        store.record(
            UsageRecord(
                "unknown-with-value",
                NOW,
                "client",
                "antigravity",
                "antigravity/model",
                False,
                UsageStatus.SUCCEEDED,
                1,
                input_tokens=1,
                token_source=TokenSource.UNKNOWN,
            )
        )
    with pytest.raises(UsageStoreError):
        store.record(
            UsageRecord(
                "reported-without-value",
                NOW,
                "client",
                "antigravity",
                "antigravity/model",
                False,
                UsageStatus.SUCCEEDED,
                1,
                token_source=TokenSource.UPSTREAM_REPORTED,
            )
        )


def test_quota_integrity_rejects_unknown_values_and_noncomplementary_available_ratios():
    engine = sqlite_engine()
    create_phase4_tables(engine)
    store = QuotaStore(engine)
    with pytest.raises(QuotaStoreError):
        store.write(
            QuotaSnapshot(
                "antigravity",
                "unknown-ratio",
                QuotaStatus.UNKNOWN,
                "probe",
                NOW,
                remaining_ratio=0.5,
            )
        )
    with pytest.raises(QuotaStoreError):
        store.write(
            QuotaSnapshot(
                "antigravity",
                "unknown-reset",
                QuotaStatus.UNKNOWN,
                "probe",
                NOW,
                reset_at=NOW,
            )
        )
    with pytest.raises(QuotaStoreError):
        store.write(
            QuotaSnapshot(
                "antigravity",
                "bad-sum",
                QuotaStatus.AVAILABLE,
                "probe",
                NOW,
                remaining_ratio=0.2,
                used_ratio=0.2,
            )
        )


def test_gateway_records_normal_error_and_completed_sse_usage_without_payload():
    engine = sqlite_engine()
    create_phase4_tables(engine)
    app, recorder = gateway_app(engine, DataPlane())
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        success = client.post("/v1/chat/completions", headers={"authorization": "Bearer good"}, json={"model": "antigravity/model"})
        error = client.post("/v1/chat/completions", headers={"authorization": "Bearer good"}, json={"model": "missing"})
    assert success.status_code == 200 and error.status_code == 404
    records = recorder.query()
    assert {item.status for item in records} == {UsageStatus.SUCCEEDED, UsageStatus.FAILED}
    successful = next(item for item in records if item.status is UsageStatus.SUCCEEDED)
    assert successful.client_key_id == "client-1" and successful.total_tokens == 5

    engine = sqlite_engine()
    create_phase4_tables(engine)
    lines = [
        'data: {"id":"x","model":"model","choices":[{"delta":{"content":"hi"}}]}',
        'data: {"id":"x","model":"model","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}',
        "data: [DONE]",
    ]
    app, recorder = gateway_app(engine, DataPlane(stream_lines=lines))
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        response = client.post("/v1/chat/completions", headers={"authorization": "Bearer good"}, json={"model": "antigravity/model", "stream": True, "stream_options": {"include_usage": True}})
    assert response.status_code == 200
    item = recorder.query()[0]
    assert item.status is UsageStatus.SUCCEEDED and item.stream is True and item.total_tokens == 3


def test_gateway_records_aborted_sse_without_done_and_direct_close():
    engine = sqlite_engine()
    create_phase4_tables(engine)
    from ai_provider_gateway.gateway.app import _stream_response

    recorder = UsageStore(engine)
    plane = DataPlane(stream_lines=['data: {"id":"x","model":"model","choices":[]}'])
    captured = []

    def finalize(status, usage, error_category):
        captured.append((status, usage, error_category))

    async def scenario():
        from ai_provider_gateway.gateway.app import _ClosingStreamingResponse

        service = GatewayService(
            registry=Registry(),
            data_plane=plane,
            authorize=lambda token: SimpleNamespace(key_id="client-1"),
            usage_recorder=recorder,
        )
        response = await _stream_response(
            service,
            {"model": "model", "stream": True},
            "antigravity/model",
            finalize,
        )
        assert isinstance(response, _ClosingStreamingResponse)
        await response.body_iterator.aclose()

    asyncio.run(scenario())
    assert captured[0][0] is UsageStatus.ABORTED


def test_gateway_records_failed_stream_usage():
    engine = sqlite_engine()
    create_phase4_tables(engine)
    app, recorder = gateway_app(engine, DataPlane(error=SidecarTransportError("offline")))
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        response = client.post("/v1/chat/completions", headers={"authorization": "Bearer good"}, json={"model": "antigravity/model", "stream": True})
    assert response.status_code == 503
    assert recorder.query()[0].status is UsageStatus.FAILED


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (SidecarUpstreamError(429, code="quota_exhausted"), "quota_exhausted"),
        (SidecarUpstreamError(429), "rate_limit_exceeded"),
        (SidecarUpstreamError(500), "provider_error"),
    ],
)
def test_gateway_usage_preserves_safe_upstream_error_category(error, category):
    engine = sqlite_engine()
    create_phase4_tables(engine)
    app, recorder = gateway_app(engine, DataPlane(error=error))
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer good"},
            json={"model": "antigravity/model"},
        )
    assert recorder.query()[0].error_category == category


def test_cancelled_stream_is_persisted_as_aborted_not_failed():
    from ai_provider_gateway.gateway.app import _ClosingSSEStream

    engine = sqlite_engine()
    create_phase4_tables(engine)
    recorder = UsageStore(engine)

    class CancelledResponse:
        async def aiter_lines(self):
            raise asyncio.CancelledError
            yield ""

        async def aclose(self):
            return None

    def finalize(status, usage, error_category):
        recorder.record(
            UsageRecord(
                "cancelled-request",
                NOW,
                "client-a",
                "antigravity",
                "antigravity/model",
                True,
                status,
                1,
                token_source=usage[3],
                error_category=error_category,
            )
        )

    async def scenario():
        stream = _ClosingSSEStream(
            CancelledResponse(), "antigravity/model", finalize
        )
        with pytest.raises(asyncio.CancelledError):
            await stream.__anext__()

    asyncio.run(scenario())
    item = recorder.query()[0]
    assert item.status is UsageStatus.ABORTED
    assert item.error_category == "client_disconnected"


def test_quota_refresh_atomically_replaces_stale_buckets_and_failure_state():
    engine = sqlite_engine()
    create_phase4_tables(engine)
    store = QuotaStore(engine)
    store.write_many(
        [
            QuotaSnapshot("antigravity", "one", QuotaStatus.AVAILABLE, "test", NOW, remaining_ratio=0.8),
            QuotaSnapshot("antigravity", "two", QuotaStatus.AVAILABLE, "test", NOW, remaining_ratio=0.5),
        ]
    )
    store.write_many(
        [QuotaSnapshot("antigravity", "one", QuotaStatus.AVAILABLE, "test", NOW, remaining_ratio=0.7)]
    )
    assert [item.bucket_id for item in store.read("antigravity")] == ["one"]

    class FailingProbe:
        async def probe(self, provider):
            raise RuntimeError("unavailable")

    async def scenario():
        cache = QuotaCache(
            store,
            FailingProbe(),
            ttl=timedelta(seconds=0),
            min_refresh_interval=timedelta(seconds=0),
        )
        return await cache.get_or_refresh("antigravity", force=True)

    result = asyncio.run(scenario())
    assert len(result) == 1
    assert result[0].status is QuotaStatus.UNKNOWN
    assert [item.status for item in store.read("antigravity")] == [QuotaStatus.UNKNOWN]


def test_control_api_requires_authorizer_and_keeps_usage_health_quota_separate():
    engine = sqlite_engine()
    create_phase4_tables(engine)
    usage = UsageStore(engine)
    usage.record(UsageRecord("request-1", datetime.now(UTC), "client-a", "antigravity", "antigravity/model", False, UsageStatus.SUCCEEDED, 10, 1, 2, 3, TokenSource.UPSTREAM_REPORTED))
    health_store = HealthStore(engine)

    class Phase2:
        async def sidecar_health(self):
            return SimpleNamespace(state=SidecarHealthState.AVAILABLE)

        async def aclose(self):
            return None

    class Probe:
        async def probe(self, provider):
            return QuotaSnapshot(provider, "contract", QuotaStatus.UNKNOWN, "unverified", NOW, error_category="quota_contract_unverified")

    phase4 = Phase4Control(
        phase2=Phase2(),
        usage=usage,
        quota=QuotaCache(QuotaStore(engine), Probe(), ttl=timedelta(seconds=0), min_refresh_interval=timedelta(seconds=0)),
        health=health_store,
    )
    app = create_control_app(
        phase2=phase4.phase2,
        phase4=phase4,
        authorize_control=lambda request: request.headers.get("x-auth") == "yes",
    )
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        assert client.get("/api/control/usage").status_code == 403
        headers = {"x-auth": "yes"}
        usage_response = client.get("/api/control/usage?provider=antigravity&client_key_id=client-a", headers=headers)
        summary = client.get("/api/control/statistics/summary", headers=headers)
        health = client.get("/api/control/providers/antigravity/health", headers=headers)
        quota = client.get("/api/control/providers/antigravity/quota", headers=headers)
        refreshed = client.post("/api/control/providers/antigravity/quota/refresh", headers=headers)

    assert usage_response.status_code == 200 and len(usage_response.json()["data"]) == 1
    assert summary.status_code == 200
    body = summary.json()
    assert set(body) == {"usage", "health", "quota"}
    assert body["usage"]["request_count"] == 1
    assert body["health"]["provider"] == "antigravity"
    assert body["quota"]["status"] == "unknown"
    assert health.json()["state"] == "available"
    assert quota.json()["status"] == "unknown"
    assert refreshed.json()["data"][0]["error_category"] == "quota_contract_unverified"
    assert "/v1/responses" not in {route.path for route in app.routes}
