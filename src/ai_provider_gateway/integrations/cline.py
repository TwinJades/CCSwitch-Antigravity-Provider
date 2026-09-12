"""Public Cline SDK connection description without private settings formats."""

from __future__ import annotations

from typing import Any

from ..models import ModelRecord
from .common import available_models, environment_name, gateway_base_url, selected_model


def generate_cline_config(
    records: list[ModelRecord] | tuple[ModelRecord, ...],
    *,
    base_url: str = "http://127.0.0.1:8020/v1",
    api_key_env: str = "AIPG_GATEWAY_API_KEY",
    model: str | None = None,
) -> dict[str, Any]:
    """Return stable inputs for Cline SDK's OpenAI-compatible provider."""

    models = available_models(records)
    return {
        "format": "cline-sdk-openai-compatible",
        "providerId": "openai-compatible",
        "modelId": selected_model(models, model),
        "baseUrl": gateway_base_url(base_url),
        "apiKeyEnv": environment_name(api_key_env),
    }
