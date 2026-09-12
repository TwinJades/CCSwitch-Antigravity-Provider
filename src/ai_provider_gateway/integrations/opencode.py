"""OpenCode v1.18.29 configuration generation from Model Registry records."""

from __future__ import annotations

from typing import Any

from ..models import ModelRecord
from .common import available_models, environment_name, gateway_base_url, safe_id, selected_model


def generate_opencode_config(
    records: list[ModelRecord] | tuple[ModelRecord, ...],
    *,
    base_url: str = "http://127.0.0.1:8020/v1",
    provider_id: str = "personal-ai-gateway",
    api_key_env: str = "AIPG_GATEWAY_API_KEY",
    default_model: str | None = None,
) -> dict[str, Any]:
    """Return pinned OpenCode 1.x JSON without embedding the API-key value."""

    models = available_models(records)
    provider = safe_id(provider_id, "Provider ID", maximum=64)
    key_environment = environment_name(api_key_env)
    endpoint = gateway_base_url(base_url)
    chosen = selected_model(models, default_model)
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            provider: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Personal AI Provider Gateway",
                "options": {
                    "baseURL": endpoint,
                    "apiKey": f"{{env:{key_environment}}}",
                },
                "models": {
                    record.canonical_id: {"name": record.canonical_id}
                    for record in models
                },
            }
        },
        "model": f"{provider}/{chosen}",
    }
