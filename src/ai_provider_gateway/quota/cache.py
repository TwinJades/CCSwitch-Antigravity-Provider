"""Safe quota-cache persistence and asynchronous, throttled refreshes."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import inspect
import re
from typing import Protocol

from sqlalchemy import Column, DateTime, Float, Index, MetaData, String, Table, delete, insert, select, update
from sqlalchemy.engine import Engine, RowMapping


class QuotaStoreError(ValueError): pass
class QuotaStatus(StrEnum): AVAILABLE = "available"; UNKNOWN = "unknown"

@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    provider: str
    bucket_id: str
    status: QuotaStatus
    source: str
    updated_at: datetime
    group_display: str | None = None
    bucket_display: str | None = None
    window: str | None = None
    remaining_ratio: float | None = None
    used_ratio: float | None = None
    reset_at: datetime | None = None
    error_category: str | None = None

class QuotaProbe(Protocol):
    async def probe(self, provider: str) -> QuotaSnapshot | list[QuotaSnapshot]: ...

_metadata = MetaData()
quota_snapshots = Table("quota_snapshots", _metadata,
    Column("provider", String(128), primary_key=True), Column("bucket_id", String(256), primary_key=True),
    Column("status", String(16), nullable=False), Column("group_display", String(256)), Column("bucket_display", String(256)), Column("window", String(128)), Column("remaining_ratio", Float), Column("used_ratio", Float), Column("reset_at", DateTime(timezone=True)), Column("source", String(64), nullable=False), Column("updated_at", DateTime(timezone=True), nullable=False), Column("error_category", String(64)),
)
Index("ix_quota_snapshots_updated_at", quota_snapshots.c.updated_at)
_SAFE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")

class QuotaStore:
    def __init__(self, engine: Engine) -> None: self._engine = engine
    def write(self, snapshot: QuotaSnapshot) -> QuotaSnapshot:
        item = _validate(snapshot)
        values = _values(item)
        with self._engine.begin() as connection:
            existing = connection.execute(select(quota_snapshots.c.provider).where(quota_snapshots.c.provider == item.provider, quota_snapshots.c.bucket_id == item.bucket_id)).first()
            if existing: connection.execute(update(quota_snapshots).where(quota_snapshots.c.provider == item.provider, quota_snapshots.c.bucket_id == item.bucket_id).values(**values))
            else: connection.execute(insert(quota_snapshots).values(**values))
        return item
    def write_many(self, snapshots: list[QuotaSnapshot]) -> list[QuotaSnapshot]:
        validated = [_validate(item) for item in snapshots]
        if not validated: return []
        provider = validated[0].provider
        if any(item.provider != provider for item in validated): raise QuotaStoreError("snapshots must belong to one provider")
        with self._engine.begin() as connection:
            connection.execute(delete(quota_snapshots).where(quota_snapshots.c.provider == provider))
            for item in validated:
                connection.execute(insert(quota_snapshots).values(**_values(item)))
        return validated
    def read(self, provider: str) -> list[QuotaSnapshot]:
        provider = _safe(provider, "provider")
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(quota_snapshots)
                .where(quota_snapshots.c.provider == provider)
                .order_by(quota_snapshots.c.bucket_id)
            ).mappings().all()
        return [_snapshot(row) for row in rows]

class QuotaCache:
    """Read cache with TTL and a per-provider minimum probe interval.

    Probe exceptions are converted to a persisted unknown snapshot, ensuring a
    dashboard refresh never affects inference callers.
    """
    def __init__(self, store: QuotaStore, probe: QuotaProbe, *, ttl: timedelta = timedelta(minutes=5), min_refresh_interval: timedelta = timedelta(seconds=30)) -> None:
        if ttl.total_seconds() < 0 or min_refresh_interval.total_seconds() < 0: raise QuotaStoreError("durations must not be negative")
        self._store, self._probe, self._ttl, self._minimum = store, probe, ttl, min_refresh_interval
        self._last_attempt: dict[str, datetime] = {}; self._locks: dict[str, asyncio.Lock] = {}
    async def get_or_refresh(self, provider: str, *, force: bool = False) -> list[QuotaSnapshot]:
        provider = _safe(provider, "provider"); now = datetime.now(UTC); cached = self._store.read(provider)
        if not force and cached and all(now - item.updated_at <= self._ttl for item in cached): return cached
        lock = self._locks.setdefault(provider, asyncio.Lock())
        async with lock:
            now = datetime.now(UTC); cached = self._store.read(provider)
            if not force and cached and all(now - item.updated_at <= self._ttl for item in cached): return cached
            last = self._last_attempt.get(provider)
            if last and now - last < self._minimum: return cached
            self._last_attempt[provider] = now
            try:
                result = self._probe.probe(provider)
                snapshots = await result if inspect.isawaitable(result) else result
                items = snapshots if isinstance(snapshots, list) else [snapshots]
                if not items: raise QuotaStoreError("probe returned no snapshots")
                if any(_validate(item).provider != provider for item in items): raise QuotaStoreError("probe returned another provider")
                return self._store.write_many(items)
            except Exception:
                unknown = QuotaSnapshot(provider, "unknown", QuotaStatus.UNKNOWN, "probe_failure", now, error_category="probe_failed")
                return self._store.write_many([unknown])

def _validate(item: QuotaSnapshot) -> QuotaSnapshot:
    if not isinstance(item, QuotaSnapshot): raise QuotaStoreError("snapshot must be a QuotaSnapshot")
    status = QuotaStatus(item.status); provider, bucket_id, source = _safe(item.provider, "provider"), _text(item.bucket_id, "bucket ID", 256), _safe(item.source, "source")
    remaining, used = _ratio(item.remaining_ratio, "remaining ratio"), _ratio(item.used_ratio, "used ratio")
    if status is QuotaStatus.UNKNOWN and (remaining is not None or used is not None or item.reset_at is not None):
        raise QuotaStoreError("unknown quota cannot include ratios or reset time")
    if status is QuotaStatus.AVAILABLE and remaining is None and used is None: raise QuotaStoreError("available quota requires a ratio")
    if remaining is not None and used is not None and abs((remaining + used) - 1.0) > 1e-6:
        raise QuotaStoreError("remaining and used ratios must sum to one")
    return QuotaSnapshot(provider, bucket_id, status, source, _utc(item.updated_at), _optional(item.group_display, "group display", 256), _optional(item.bucket_display, "bucket display", 256), _optional(item.window, "window", 128), remaining, used, _utc(item.reset_at) if item.reset_at else None, _safe(item.error_category, "error category") if item.error_category else None)
def _values(v: QuotaSnapshot) -> dict[str, object]: return {field: getattr(v, field) for field in v.__dataclass_fields__} | {"status": v.status.value}
def _snapshot(row: RowMapping) -> QuotaSnapshot: return QuotaSnapshot(row["provider"], row["bucket_id"], QuotaStatus(row["status"]), row["source"], _utc(row["updated_at"]), row["group_display"], row["bucket_display"], row["window"], row["remaining_ratio"], row["used_ratio"], _utc(row["reset_at"]) if row["reset_at"] else None, row["error_category"])
def _ratio(value: float | None, label: str) -> float | None:
    if value is None: return None
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 <= value <= 1: raise QuotaStoreError(f"{label} must be between 0 and 1")
    return float(value)
def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime): raise QuotaStoreError("time must be a datetime")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
def _text(value: str, label: str, max_len: int = 128) -> str:
    if not isinstance(value, str) or not (out := value.strip()) or len(out) > max_len or any(ord(c) < 32 or ord(c) == 127 for c in out): raise QuotaStoreError(f"{label} is invalid")
    return out
def _optional(value: str | None, label: str, max_len: int) -> str | None: return _text(value, label, max_len) if value is not None else None
def _safe(value: str, label: str) -> str:
    out = _text(value, label).lower()
    if not _SAFE.fullmatch(out): raise QuotaStoreError(f"{label} is invalid")
    return out
