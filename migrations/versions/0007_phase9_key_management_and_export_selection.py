"""Add Gateway Key usage metadata and shared Harness export preferences."""

from collections.abc import Sequence

import sqlalchemy as sa


revision: str = "0007_phase9_key_management_and_export_selection"
down_revision: str | None = "0006_phase9_openai_compatible_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op = __import__("alembic").op
    with op.batch_alter_table("gateway_api_keys") as batch:
        batch.add_column(
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.create_table(
        "harness_export_provider_preferences",
        sa.Column("provider_id", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider_id"),
    )


def downgrade() -> None:
    op = __import__("alembic").op
    op.drop_table("harness_export_provider_preferences")
    with op.batch_alter_table("gateway_api_keys") as batch:
        batch.drop_column("last_used_at")
