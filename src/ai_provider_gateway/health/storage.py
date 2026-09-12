"""Provider health history; this deliberately does not infer quota state."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import re
from sqlalchemy import Column, DateTime, Index, Integer, MetaData, String, Table, insert, select
from sqlalchemy.engine import Engine, RowMapping

class HealthStoreError(ValueError): pass
class HealthState(StrEnum): AVAILABLE="available"; UNAVAILABLE="unavailable"; AUTH_REQUIRED="auth_required"; STARTING="starting"; STOPPED="stopped"; CRASHED="crashed"; API_AUTH_FAILED="api_auth_failed"
@dataclass(frozen=True, slots=True)
class HealthRecord:
    provider: str
    state: HealthState
    checked_at: datetime
    latency_ms: int | None = None
    error_category: str | None = None

_metadata=MetaData()
provider_health_records=Table("provider_health_records", _metadata, Column("id", Integer, primary_key=True, autoincrement=True), Column("provider", String(128), nullable=False), Column("state", String(32), nullable=False), Column("checked_at", DateTime(timezone=True), nullable=False), Column("latency_ms", Integer), Column("error_category", String(64)))
Index("ix_provider_health_provider_checked", provider_health_records.c.provider, provider_health_records.c.checked_at)
_SAFE=re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
class HealthStore:
    def __init__(self, engine: Engine) -> None: self._engine=engine
    def record(self, record: HealthRecord) -> HealthRecord:
        item=_validate(record)
        with self._engine.begin() as connection: connection.execute(insert(provider_health_records).values(provider=item.provider, state=item.state.value, checked_at=item.checked_at, latency_ms=item.latency_ms, error_category=item.error_category))
        return item
    def latest(self, provider: str) -> HealthRecord | None:
        provider=_safe(provider, "provider")
        with self._engine.connect() as connection: row=connection.execute(select(provider_health_records).where(provider_health_records.c.provider == provider).order_by(provider_health_records.c.checked_at.desc(), provider_health_records.c.id.desc()).limit(1)).mappings().first()
        return _record(row) if row else None
def _validate(value: HealthRecord) -> HealthRecord:
    if not isinstance(value, HealthRecord): raise HealthStoreError("record must be a HealthRecord")
    latency=value.latency_ms
    if latency is not None and (not isinstance(latency, int) or isinstance(latency, bool) or latency < 0): raise HealthStoreError("latency must be non-negative")
    return HealthRecord(_safe(value.provider, "provider"), HealthState(value.state), _utc(value.checked_at), latency, _safe(value.error_category, "error category") if value.error_category else None)
def _record(row: RowMapping) -> HealthRecord: return HealthRecord(row["provider"], HealthState(row["state"]), _utc(row["checked_at"]), row["latency_ms"], row["error_category"])
def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime): raise HealthStoreError("checked_at must be a datetime")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
def _safe(value: str, label: str) -> str:
    if not isinstance(value,str) or not (out:=value.strip().lower()) or len(out)>128 or not _SAFE.fullmatch(out): raise HealthStoreError(f"{label} is invalid")
    return out


ProviderHealthSnapshot = HealthRecord
ProviderHealthStore = HealthStore
