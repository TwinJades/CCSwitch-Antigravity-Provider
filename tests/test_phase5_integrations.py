from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from ai_provider_gateway.control.app import create_app
from ai_provider_gateway.control.phase5 import Phase5Control
from ai_provider_gateway.control.runtime import Phase2RuntimeSettings, build_phase5_control
from ai_provider_gateway.integrations import (
    HarnessExportSelectionStore,
    IntegrationConfigError,
    generate_cline_config,
    generate_opencode_config,
)
from ai_provider_gateway.integrations.selection import export_provider_preferences
from ai_provider_gateway.models import (
    DiscoveredModel,
    DiscoveryState,
    ModelRecord,
    ModelRegistry,
)
from ai_provider_gateway.models.registry import model_registry, provider_discovery_state


NOW = __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC)


def record(canonical: str, *, available: bool = True, alias: str | None = None, display: str | None = None) -> ModelRecord:
    provider, upstream = canonical.split("/", 1)
    return ModelRecord(
        canonical,
        provider,
        upstream,
        display or upstream,
        alias,
        available,
        NOW,
        DiscoveryState.AVAILABLE if available else DiscoveryState.STALE,
        NOW,
    )


def test_opencode_exports_sorted_available_canonical_models_and_alias_selection():
    config = generate_opencode_config(
        [
            record("antigravity/z-model", alias="z"),
            record("antigravity/a-model", alias="preferred", display="A Model"),
            record("antigravity/hidden", available=False),
        ],
        provider_id="gateway",
        default_model="preferred",
        api_key_env="MY_GATEWAY_KEY",
    )
    assert config["$schema"] == "https://opencode.ai/config.json"
    provider = config["provider"]["gateway"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {
        "baseURL": "http://127.0.0.1:8020/v1",
        "apiKey": "{env:MY_GATEWAY_KEY}",
    }
    assert list(provider["models"]) == ["antigravity/a-model", "antigravity/z-model"]
    assert provider["models"]["antigravity/a-model"]["name"] == "antigravity/a-model"
    assert config["model"] == "gateway/antigravity/a-model"
    assert "hidden" not in str(config)
    assert "MY_GATEWAY_KEY" in str(config)


def test_opencode_model_names_are_schema_compatible_when_registry_names_are_none():
    records = [
        ModelRecord(
            "antigravity/no-name",
            "antigravity",
            "no-name",
            None,
            None,
            True,
            NOW,
            DiscoveryState.AVAILABLE,
            NOW,
        ),
        ModelRecord(
            "school/chat-model",
            "school",
            "chat-model",
            None,
            None,
            True,
            NOW,
            DiscoveryState.AVAILABLE,
            NOW,
        ),
    ]

    config = generate_opencode_config(records, default_model="school/chat-model")
    exported = config["provider"]["personal-ai-gateway"]["models"]

    assert list(exported) == ["antigravity/no-name", "school/chat-model"]
    assert exported["antigravity/no-name"] == {"name": "antigravity/no-name"}
    assert exported["school/chat-model"] == {"name": "school/chat-model"}
    assert all(
        "name" not in model or isinstance(model["name"], str)
        for model in exported.values()
    )
    assert '"name": null' not in json.dumps(config)
    assert config["model"] == "personal-ai-gateway/school/chat-model"


def test_cline_exports_stable_public_fields_and_canonical_selected_model():
    config = generate_cline_config(
        [record("antigravity/model", alias="friendly")],
        model="friendly",
        api_key_env="AIPG_KEY",
    )
    assert config == {
        "format": "cline-sdk-openai-compatible",
        "providerId": "openai-compatible",
        "modelId": "antigravity/model",
        "baseUrl": "http://127.0.0.1:8020/v1",
        "apiKeyEnv": "AIPG_KEY",
    }


@pytest.mark.parametrize(
    "records,kwargs",
    [
        ([], {}),
        ([record("antigravity/model", available=False)], {}),
        ([record("antigravity/model"), record("antigravity/model")], {}),
    ],
)
def test_generators_reject_empty_unavailable_or_duplicate_canonical_catalogues(records, kwargs):
    with pytest.raises(IntegrationConfigError):
        generate_opencode_config(records, **kwargs)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.com/v1",
        "http://127.0.0.1/api",
        "http://127.0.0.1/v1?secret=value",
        "http://user:pass@127.0.0.1/v1",
        "http://127.0.0.1:bad/v1",
        "http://127.0.0.1:65536/v1",
        "http://127.0.0.1/v1",
    ],
)
def test_generators_reject_non_loopback_or_wrong_gateway_paths(base_url):
    with pytest.raises(IntegrationConfigError):
        generate_cline_config([record("antigravity/model")], base_url=base_url)


def test_generators_reject_invalid_provider_and_environment_names_without_real_key_values():
    with pytest.raises(IntegrationConfigError):
        generate_opencode_config([record("antigravity/model")], provider_id="bad id")
    with pytest.raises(IntegrationConfigError):
        generate_cline_config([record("antigravity/model")], api_key_env="not-an-env")
    output = generate_cline_config([record("antigravity/model")], api_key_env="AIPG_KEY")
    assert "real-secret" not in str(output)


def test_opencode_e2e_script_reads_gateway_key_only_from_parent_environment():
    script = (Path(__file__).parents[1] / "scripts" / "dev" / "run-opencode-gateway-e2e.ps1").read_text(
        encoding="utf-8"
    )
    assert "[string]$GatewayApiKey" not in script
    assert "$gatewayApiKey = $env:AIPG_GATEWAY_API_KEY" in script
    assert "must be set in the parent process environment" in script


class Phase2Stub:
    def __init__(self, records):
        self.records = records

    def current_models(self):
        return tuple(self.records)

    async def aclose(self):
        return None


def test_phase5_control_routes_are_authorized_and_use_live_models():
    phase2 = Phase2Stub([record("antigravity/model", alias="friendly")])
    phase5 = Phase5Control(phase2=phase2)
    app = create_app(
        phase2=phase2,
        phase5=phase5,
        authorize_control=lambda request: request.headers.get("x-auth") == "yes",
    )
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        assert client.get("/api/control/integrations/opencode/config").status_code == 403
        headers = {"x-auth": "yes"}
        opencode = client.get(
            "/api/control/integrations/opencode/config?default_model=friendly",
            headers=headers,
        )
        cline = client.get(
            "/api/control/integrations/cline/config?model=friendly",
            headers=headers,
        )
        bad = client.get(
            "/api/control/integrations/opencode/config?provider_id=bad%20id",
            headers=headers,
        )
        assert client.get("/v1/responses", headers=headers).status_code == 404
    assert opencode.status_code == 200
    assert opencode.json()["model"] == "personal-ai-gateway/antigravity/model"
    assert cline.status_code == 200
    assert cline.json()["modelId"] == "antigravity/model"
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "invalid_harness_configuration"
    assert "bad id" not in bad.text


def test_export_provider_selection_control_api_filters_opencode_and_cline(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'api-selection.db').as_posix()}")
    export_provider_preferences.create(engine)
    phase2 = Phase2Stub([
        record("antigravity/model-a"),
        record("school/model-b"),
    ])
    phase5 = Phase5Control(
        phase2=phase2,
        export_selection=HarnessExportSelectionStore(engine),
    )
    app = create_app(
        phase2=phase2,
        phase5=phase5,
        authorize_control=lambda request: request.headers.get("x-auth") == "yes",
    )
    headers = {"x-auth": "yes"}
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        initial = client.get(
            "/api/control/integrations/export-providers", headers=headers
        )
        saved = client.post(
            "/api/control/integrations/export-providers",
            headers=headers,
            json={"provider_ids": ["school"]},
        )
        opencode = client.get(
            "/api/control/integrations/opencode/config", headers=headers
        )
        cline = client.get(
            "/api/control/integrations/cline/config", headers=headers
        )

    assert initial.status_code == 200
    assert all(item["enabled"] for item in initial.json()["data"])
    assert saved.status_code == 200
    assert {item["provider_id"]: item["enabled"] for item in saved.json()["data"]} == {
        "antigravity": False,
        "school": True,
    }
    assert set(opencode.json()["provider"]["personal-ai-gateway"]["models"]) == {
        "school/model-b"
    }
    assert cline.json()["modelId"] == "school/model-b"


def test_opencode_control_api_never_returns_null_model_names():
    phase2 = Phase2Stub(
        [
            ModelRecord(
                "school/no-display-name",
                "school",
                "no-display-name",
                None,
                None,
                True,
                NOW,
                DiscoveryState.AVAILABLE,
                NOW,
            )
        ]
    )
    app = create_app(
        phase2=phase2,
        phase5=Phase5Control(phase2=phase2),
        authorize_control=lambda request: True,
    )

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        response = client.get("/api/control/integrations/opencode/config")

    assert response.status_code == 200
    exported = response.json()["provider"]["personal-ai-gateway"]["models"]
    assert exported == {
        "school/no-display-name": {"name": "school/no-display-name"}
    }


def test_phase5_runtime_uses_gateway_base_url_without_reading_gateway_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("AIPG_SIDECAR_EXECUTABLE", str(tmp_path / "sidecar.exe"))
    monkeypatch.setenv("AIPG_SIDECAR_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("AIPG_SIDECAR_API_KEY", "sidecar-api")
    monkeypatch.setenv("AIPG_SIDECAR_MANAGEMENT_KEY", "management-key")
    database_url = f"sqlite:///{(tmp_path / 'phase5.db').as_posix()}"
    engine = create_engine(database_url)
    export_provider_preferences.create(engine)
    monkeypatch.setenv("AIPG_DATABASE_URL", database_url)
    monkeypatch.setenv("AIPG_GATEWAY_BASE_URL", "http://127.0.0.1:9000/v1")
    monkeypatch.setenv("AIPG_GATEWAY_API_KEY", "do-not-read-this-value")
    settings = Phase2RuntimeSettings.from_env()
    assert settings.gateway_base_url == "http://127.0.0.1:9000/v1"
    assert "do-not-read-this-value" not in repr(settings)
    phase5 = build_phase5_control(settings, Phase2Stub([record("antigravity/model")]))
    assert phase5.gateway_base_url == "http://127.0.0.1:9000/v1"
    assert "do-not-read-this-value" not in str(phase5.opencode_config())


def test_one_persisted_provider_selection_filters_every_harness_export(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'selection.db').as_posix()}")
    export_provider_preferences.create(engine)
    phase2 = Phase2Stub([
        record("antigravity/model-a", display="Antigravity Model"),
        record("school/model-b", display="School Model"),
    ])
    selection = HarnessExportSelectionStore(engine)
    control = Phase5Control(phase2=phase2, export_selection=selection)

    assert {item["provider_id"] for item in control.export_provider_options()} == {
        "antigravity", "school"
    }
    assert set(control.opencode_config()["provider"]["personal-ai-gateway"]["models"]) == {
        "antigravity/model-a", "school/model-b"
    }

    control.replace_export_providers({"school"})
    assert set(control.opencode_config()["provider"]["personal-ai-gateway"]["models"]) == {
        "school/model-b"
    }
    assert control.cline_config()["modelId"] == "school/model-b"

    restarted = Phase5Control(
        phase2=phase2,
        export_selection=HarnessExportSelectionStore(engine),
    )
    assert [item["enabled"] for item in restarted.export_provider_options()] == [False, True]
    assert set(restarted.opencode_config()["provider"]["personal-ai-gateway"]["models"]) == {
        "school/model-b"
    }
    restarted.replace_export_providers(set())
    with pytest.raises(IntegrationConfigError):
        restarted.opencode_config()


def test_phase5_routes_do_not_mount_phase6_or_responses_api():
    phase2 = Phase2Stub([record("antigravity/model")])
    app = create_app(
        phase2=phase2,
        phase5=Phase5Control(phase2=phase2),
        authorize_control=lambda request: True,
    )
    paths = {route.path for route in app.routes}
    assert "/v1/responses" not in paths
    assert "/api/control/usage" not in paths
    assert "/api/control/providers/antigravity/quota" not in paths


def _export_database(tmp_path):
    database = tmp_path / "registry.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    model_registry.create(engine)
    provider_discovery_state.create(engine)
    ModelRegistry(engine).refresh_success(
        "antigravity",
        [DiscoveredModel("z-model"), DiscoveredModel("a-model")],
    )
    return database


@pytest.mark.parametrize("kind", ["opencode", "cline"])
def test_export_config_reads_registry_and_writes_consistent_json_without_key_value(tmp_path, kind):
    from ai_provider_gateway.integrations.export import export_config

    database = _export_database(tmp_path)
    output = tmp_path / f"{kind}.json"
    path = export_config(
        kind=kind,
        database_url=f"sqlite:///{database.as_posix()}",
        output=output,
        api_key_env="AIPG_GATEWAY_API_KEY",
    )
    assert path == output.resolve()
    payload = json.loads(output.read_text(encoding="utf-8"))
    text = output.read_text(encoding="utf-8")
    assert "real-secret" not in text
    if kind == "opencode":
        assert list(payload["provider"]["personal-ai-gateway"]["models"]) == [
            "antigravity/a-model",
            "antigravity/z-model",
        ]
        assert payload["model"] == "personal-ai-gateway/antigravity/a-model"
        assert payload["provider"]["personal-ai-gateway"]["options"]["apiKey"] == "{env:AIPG_GATEWAY_API_KEY}"
    else:
        assert payload["modelId"] == "antigravity/a-model"
        assert payload["apiKeyEnv"] == "AIPG_GATEWAY_API_KEY"


def test_export_config_refuses_overwrite_and_rejects_unsupported_kind(tmp_path):
    from ai_provider_gateway.integrations.export import export_config

    database = _export_database(tmp_path)
    output = tmp_path / "existing.json"
    output.write_text("do not replace", encoding="utf-8")
    with pytest.raises(FileExistsError):
        export_config(
            kind="opencode",
            database_url=f"sqlite:///{database.as_posix()}",
            output=output,
        )
    with pytest.raises(ValueError, match="Unsupported"):
        export_config(
            kind="unknown",
            database_url=f"sqlite:///{database.as_posix()}",
            output=tmp_path / "unknown.json",
        )


def test_export_config_uses_exclusive_create_for_a_racing_output(tmp_path, monkeypatch):
    from ai_provider_gateway.integrations.export import export_config

    database = _export_database(tmp_path)
    output = tmp_path / "race.json"
    original_open = Path.open

    def racing_open(path, mode="r", *args, **kwargs):
        if path == output and mode == "x":
            output.write_text("created elsewhere", encoding="utf-8")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        export_config(
            kind="opencode",
            database_url=f"sqlite:///{database.as_posix()}",
            output=output,
        )
    assert output.read_text(encoding="utf-8") == "created elsewhere"
