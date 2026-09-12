"""Phase 5 Harness configuration facade."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..integrations import (
    HarnessExportSelectionStore,
    IntegrationConfigError,
    generate_cline_config,
    generate_opencode_config,
)
from ..models import ModelRecord
from .phase2 import Phase2Control


@dataclass(slots=True)
class Phase5Control:
    """Generate Harness-specific output from the live Model Registry only."""

    phase2: Phase2Control
    gateway_base_url: str = "http://127.0.0.1:8020/v1"
    export_selection: HarnessExportSelectionStore | None = None

    def export_provider_options(self) -> tuple[dict[str, object], ...]:
        records = self.phase2.current_models()
        provider_ids = {record.provider for record in records}
        enabled = (
            self.export_selection.enabled_from(provider_ids)
            if self.export_selection is not None
            else frozenset(provider_ids)
        )
        return tuple(
            {
                "provider_id": provider_id,
                "enabled": provider_id in enabled,
                "model_count": sum(
                    1
                    for record in records
                    if record.provider == provider_id and record.available
                ),
            }
            for provider_id in sorted(provider_ids)
        )

    def replace_export_providers(
        self, enabled_provider_ids: set[str]
    ) -> tuple[dict[str, object], ...]:
        if self.export_selection is None:
            raise IntegrationConfigError("Export selection persistence is unavailable.")
        records = self.phase2.current_models()
        provider_ids = {record.provider for record in records}
        self.export_selection.replace(provider_ids, enabled_provider_ids)
        return self.export_provider_options()

    def _export_models(self) -> tuple[ModelRecord, ...]:
        records = self.phase2.current_models()
        provider_ids = {record.provider for record in records}
        enabled = (
            self.export_selection.enabled_from(provider_ids)
            if self.export_selection is not None
            else frozenset(provider_ids)
        )
        return tuple(record for record in records if record.provider in enabled)

    def opencode_config(
        self,
        *,
        provider_id: str = "personal-ai-gateway",
        default_model: str | None = None,
        api_key_env: str = "AIPG_GATEWAY_API_KEY",
    ) -> dict[str, Any]:
        return generate_opencode_config(
            self._export_models(),
            base_url=self.gateway_base_url,
            provider_id=provider_id,
            default_model=default_model,
            api_key_env=api_key_env,
        )

    def cline_config(
        self,
        *,
        model: str | None = None,
        api_key_env: str = "AIPG_GATEWAY_API_KEY",
    ) -> dict[str, Any]:
        return generate_cline_config(
            self._export_models(),
            base_url=self.gateway_base_url,
            model=model,
            api_key_env=api_key_env,
        )
