"""Add Phase 3 Gateway API-key verifier records.

Only public key metadata and irreversible verifiers are stored. Full API keys,
request payloads, usage, quota and credentials are outside this migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa


revision: str = "0003_phase3_gateway_keys"
down_revision: str | None = "0002_phase2_model_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op = __import__("alembic").op
    op.create_table(
        "gateway_api_keys",
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=64), nullable=False),
        sa.Column("verifier", sa.String(length=512), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key_id"),
        sa.UniqueConstraint("prefix"),
    )


def downgrade() -> None:
    op = __import__("alembic").op
    op.drop_table("gateway_api_keys")
