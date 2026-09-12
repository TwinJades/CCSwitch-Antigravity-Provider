"""Add the Phase 2 dynamic model registry.

Only model-catalogue metadata is persisted here.  Credentials, usage, quota,
client keys, sessions, and request payloads remain outside this migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa


revision: str = "0002_phase2_model_registry"
down_revision: str | None = "0001_phase1_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op = __import__("alembic").op
    op.create_table(
        "model_registry",
        sa.Column("canonical_id", sa.String(length=512), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("upstream_id", sa.String(length=512), nullable=False),
        sa.Column("display_name", sa.String(length=512), nullable=True),
        sa.Column("alias", sa.String(length=512), nullable=True),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("canonical_id"),
        sa.UniqueConstraint("alias"),
        sa.UniqueConstraint("provider", "upstream_id", name="uq_model_registry_provider_upstream"),
    )
    op.create_index("ix_model_registry_available", "model_registry", ["available"], unique=False)
    op.create_index("ix_model_registry_provider", "model_registry", ["provider"], unique=False)
    op.create_table(
        "provider_discovery_state",
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("provider"),
    )


def downgrade() -> None:
    op = __import__("alembic").op
    op.drop_table("provider_discovery_state")
    op.drop_index("ix_model_registry_provider", table_name="model_registry")
    op.drop_index("ix_model_registry_available", table_name="model_registry")
    op.drop_table("model_registry")
