"""Phase 4 statistics facade, separate from inference and OAuth control."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic

from ..health import HealthState, ProviderHealthSnapshot, ProviderHealthStore
from ..quota import QuotaCache, QuotaSnapshot
from ..usage import UsageQuery, UsageRecord, UsageRecorder, UsageSummary
from .phase2 import Phase2Control


@dataclass(slots=True)
class Phase4Control:
    """Expose independently stored Usage, Quota, and Provider Health data."""

    phase2: Phase2Control
    usage: UsageRecorder
    quota: QuotaCache
    health: ProviderHealthStore

    def usage_records(self, query: UsageQuery | None = None) -> list[UsageRecord]:
        return self.usage.query(query)

    def usage_summary(self, query: UsageQuery | None = None) -> UsageSummary:
        return self.usage.aggregate(query)

    def purge_usage(self, *, older_than: datetime | None = None) -> int:
        return self.usage.purge(older_than=older_than)

    async def quota_snapshots(self, *, force: bool = False) -> list[QuotaSnapshot]:
        return await self.quota.get_or_refresh("antigravity", force=force)

    async def provider_health(self) -> ProviderHealthSnapshot:
        started = monotonic()
        observed = await self.phase2.sidecar_health()
        latency_ms = max(0, round((monotonic() - started) * 1000))
        state = HealthState(observed.state.value)
        error_category = None if state is HealthState.AVAILABLE else state.value
        return self.health.record(
            ProviderHealthSnapshot(
                provider="antigravity",
                state=state,
                checked_at=datetime.now(UTC),
                latency_ms=latency_ms,
                error_category=error_category,
            )
        )

    async def dashboard_snapshot(
        self, query: UsageQuery | None = None
    ) -> tuple[UsageSummary, ProviderHealthSnapshot, list[QuotaSnapshot]]:
        usage = self.usage_summary(query)
        health, quota = await asyncio.gather(
            self.provider_health(), self.quota_snapshots()
        )
        return usage, health, quota

    async def maintain_background(
        self, stop: asyncio.Event, *, interval_seconds: float = 300.0
    ) -> None:
        """Refresh quota and enforce retention outside the inference process."""

        if interval_seconds <= 0:
            raise ValueError("Background interval must be positive.")
        while not stop.is_set():
            try:
                self.purge_usage()
                await self.quota_snapshots()
            except Exception:
                # A statistics maintenance failure must not stop Supervisor.
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue
