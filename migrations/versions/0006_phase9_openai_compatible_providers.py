"""Persist DPAPI-protected OpenAI-compatible provider configuration."""

from collections.abc import Sequence

import sqlalchemy as sa


revision: str = "0006_phase9_openai_compatible_providers"
down_revision: str | None = "0005_phase6_admin_security"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op = __import__("alembic").op
    op.create_table(
        "openai_compatible_providers",
        sa.Column("provider_id", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=512), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("api_key_protected", sa.LargeBinary(), nullable=True),
        sa.Column("headers_protected", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider_id"),
    )


def downgrade() -> None:
    op = __import__("alembic").op
    op.drop_table("openai_compatible_providers")
