"""SQLAlchemy Core persistence for local request statistics only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import re

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, MetaData, String, Table, case, delete, func, insert, select
from sqlalchemy.engine import Engine, RowMapping


class UsageStoreError(ValueError):
    pass


class UsageStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


class TokenSource(StrEnum):
    UPSTREAM_REPORTED = "upstream_reported"
    GATEWAY_ESTIMATED = "gateway_estimated"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class UsageRecord:
    request_id: str
    timestamp: datetime
    client_key_id: str
    provider: str
    canonical_model: str
    stream: bool
    status: UsageStatus
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    token_source: TokenSource = TokenSource.UNKNOWN
    error_category: str | None = None


@dataclass(frozen=True, slots=True)
class UsageFilter:
    start: datetime | None = None
    end: datetime | None = None
    provider: str | None = None
    model: str | None = None
    client_key_id: str | None = None


@dataclass(frozen=True, slots=True)
class UsageAggregate:
    request_count: int
    succeeded_count: int
    failed_count: int
    aborted_count: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    average_latency_ms: float | None
    unknown_token_count: int


_metadata = MetaData()
usage_records = Table("usage_records", _metadata,
    Column("request_id", String(128), primary_key=True), Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("client_key_id", String(64), nullable=False), Column("provider", String(128), nullable=False),
    Column("canonical_model", String(512), nullable=False), Column("stream", Boolean, nullable=False),
    Column("status", String(16), nullable=False), Column("latency_ms", Integer, nullable=False),
    Column("input_tokens", Integer), Column("output_tokens", Integer), Column("total_tokens", Integer),
    Column("token_source", String(32), nullable=False), Column("error_category", String(64)),
)
Index("ix_usage_records_timestamp", usage_records.c.timestamp)
Index("ix_usage_records_provider_timestamp", usage_records.c.provider, usage_records.c.timestamp)
Index("ix_usage_records_model_timestamp", usage_records.c.canonical_model, usage_records.c.timestamp)
Index("ix_usage_records_key_timestamp", usage_records.c.client_key_id, usage_records.c.timestamp)
_SAFE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class UsageStore:
    def __init__(self, engine: Engine) -> None: self._engine = engine

    def record(self, record: UsageRecord) -> UsageRecord:
        value = _validate_record(record)
        with self._engine.begin() as connection:
            connection.execute(insert(usage_records).values(**_values(value)))
        return value

    def query(self, filters: UsageFilter | None = None) -> list[UsageRecord]:
        statement = select(usage_records).where(*_conditions(filters)).order_by(usage_records.c.timestamp.desc())
        with self._engine.connect() as connection:
            return [_record(row) for row in connection.execute(statement).mappings()]

    def aggregate(self, filters: UsageFilter | None = None) -> UsageAggregate:
        statement = select(func.count().label("count"), func.sum(usage_records.c.input_tokens).label("input"), func.sum(usage_records.c.output_tokens).label("output"), func.sum(usage_records.c.total_tokens).label("total"), func.avg(usage_records.c.latency_ms).label("latency"), func.sum(case((usage_records.c.status == UsageStatus.SUCCEEDED.value, 1), else_=0)).label("succeeded"), func.sum(case((usage_records.c.status == UsageStatus.FAILED.value, 1), else_=0)).label("failed"), func.sum(case((usage_records.c.status == UsageStatus.ABORTED.value, 1), else_=0)).label("aborted"), func.sum(case((usage_records.c.token_source == TokenSource.UNKNOWN.value, 1), else_=0)).label("unknown_tokens")).where(*_conditions(filters))
        with self._engine.connect() as connection: row = connection.execute(statement).mappings().one()
        return UsageAggregate(int(row["count"] or 0), int(row["succeeded"] or 0), int(row["failed"] or 0), int(row["aborted"] or 0), row["input"], row["output"], row["total"], float(row["latency"]) if row["latency"] is not None else None, int(row["unknown_tokens"] or 0))

    def purge(self, *, older_than: datetime | None = None) -> int:
        cutoff = _utc(older_than) if older_than else datetime.now(UTC) - timedelta(days=30)
        with self._engine.begin() as connection:
            return int(connection.execute(delete(usage_records).where(usage_records.c.timestamp < cutoff)).rowcount or 0)


def _conditions(filters: UsageFilter | None) -> list[object]:
    if filters is None: return []
    start, end = (_utc(filters.start) if filters.start else None), (_utc(filters.end) if filters.end else None)
    if start and end and start > end: raise UsageStoreError("start must not be after end")
    result: list[object] = []
    if start: result.append(usage_records.c.timestamp >= start)
    if end: result.append(usage_records.c.timestamp <= end)
    if filters.provider is not None: result.append(usage_records.c.provider == _safe(filters.provider, "provider"))
    if filters.model is not None: result.append(usage_records.c.canonical_model == _text(filters.model, "model", 512))
    if filters.client_key_id is not None: result.append(usage_records.c.client_key_id == _text(filters.client_key_id, "client key ID", 64))
    return result


def _validate_record(value: UsageRecord) -> UsageRecord:
    if not isinstance(value, UsageRecord): raise UsageStoreError("record must be a UsageRecord")
    tokens = tuple(_nonnegative(item, "token count") for item in (value.input_tokens, value.output_tokens, value.total_tokens))
    token_source = TokenSource(value.token_source)
    if token_source is TokenSource.UNKNOWN and any(item is not None for item in tokens):
        raise UsageStoreError("unknown token source cannot include token counts")
    if token_source is not TokenSource.UNKNOWN and all(item is None for item in tokens):
        raise UsageStoreError("reported or estimated token source requires a token count")
    return UsageRecord(_text(value.request_id, "request ID", 128), _utc(value.timestamp), _text(value.client_key_id, "client key ID", 64), _safe(value.provider, "provider"), _text(value.canonical_model, "canonical model", 512), bool(value.stream), UsageStatus(value.status), _nonnegative(value.latency_ms, "latency"), *tokens, token_source, _safe(value.error_category, "error category") if value.error_category else None)

def _values(v: UsageRecord) -> dict[str, object]: return {field: getattr(v, field) for field in v.__dataclass_fields__} | {"status": v.status.value, "token_source": v.token_source.value}
def _record(row: RowMapping) -> UsageRecord: return UsageRecord(row["request_id"], _utc(row["timestamp"]), row["client_key_id"], row["provider"], row["canonical_model"], bool(row["stream"]), UsageStatus(row["status"]), row["latency_ms"], row["input_tokens"], row["output_tokens"], row["total_tokens"], TokenSource(row["token_source"]), row["error_category"])
def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime): raise UsageStoreError("timestamp must be a datetime")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
def _text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not (result := value.strip()) or len(result) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in result): raise UsageStoreError(f"{label} is invalid")
    return result
def _safe(value: str, label: str) -> str:
    result = _text(value, label, 128).lower()
    if not _SAFE.fullmatch(result): raise UsageStoreError(f"{label} is invalid")
    return result
def _nonnegative(value: int | None, label: str) -> int | None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0): raise UsageStoreError(f"{label} must be non-negative")
    return value


UsageRecorder = UsageStore
UsageQuery = UsageFilter
UsageSummary = UsageAggregate
