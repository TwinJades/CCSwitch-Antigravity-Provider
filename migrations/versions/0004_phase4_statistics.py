"""Add independent Phase 4 usage, quota, and provider-health storage."""

from collections.abc import Sequence

import sqlalchemy as sa


revision: str = "0004_phase4_statistics"
down_revision: str | None = "0003_phase3_gateway_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op = __import__("alembic").op
    op.create_table(
        "usage_records",
        sa.Column("request_id", sa.String(length=128), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_key_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("canonical_model", sa.String(length=512), nullable=False),
        sa.Column("stream", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("token_source", sa.String(length=32), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_usage_records_timestamp", "usage_records", ["timestamp"])
    op.create_index("ix_usage_records_provider_timestamp", "usage_records", ["provider", "timestamp"])
    op.create_index("ix_usage_records_model_timestamp", "usage_records", ["canonical_model", "timestamp"])
    op.create_index("ix_usage_records_key_timestamp", "usage_records", ["client_key_id", "timestamp"])
    op.create_table(
        "quota_snapshots",
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("bucket_id", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("group_display", sa.String(length=256), nullable=True),
        sa.Column("bucket_display", sa.String(length=256), nullable=True),
        sa.Column("window", sa.String(length=128), nullable=True),
        sa.Column("remaining_ratio", sa.Float(), nullable=True),
        sa.Column("used_ratio", sa.Float(), nullable=True),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("provider", "bucket_id"),
    )
    op.create_index("ix_quota_snapshots_updated_at", "quota_snapshots", ["updated_at"])
    op.create_table(
        "provider_health_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_provider_health_provider_checked", "provider_health_records", ["provider", "checked_at"])


def downgrade() -> None:
    op = __import__("alembic").op
    op.drop_table("provider_health_records")
    op.drop_table("quota_snapshots")
    op.drop_table("usage_records")
