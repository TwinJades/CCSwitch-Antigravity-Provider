"""Best-effort Antigravity quota probe with an explicit unknown fallback.

CLIProxyAPI v7.2.153 does not expose a versioned, stable Antigravity quota
contract that this project can safely depend on.  The production probe therefore
reports an honest unknown snapshot until a compatible adapter is verified.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ...quota import QuotaSnapshot, QuotaStatus


class AntigravityQuotaProbe:
    """Represent the currently verified quota capability without guessing."""

    async def probe(self, provider: str) -> QuotaSnapshot:
        if provider.strip().lower() != "antigravity":
            raise ValueError("The Antigravity quota probe only accepts its provider.")
        return QuotaSnapshot(
            provider="antigravity",
            bucket_id="unknown",
            status=QuotaStatus.UNKNOWN,
            source="cliproxy_management",
            updated_at=datetime.now(UTC),
            error_category="quota_contract_unverified",
        )
