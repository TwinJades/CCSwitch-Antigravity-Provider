"""Persist the singleton administrator password verifier for Phase 6.

The table intentionally excludes passwords, recovery challenges, sessions and
credentials. Recovery challenges remain short-lived process memory only.
"""

from collections.abc import Sequence

import sqlalchemy as sa


revision: str = "0005_phase6_admin_security"
down_revision: str | None = "0004_phase4_statistics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op = __import__("alembic").op
    op.create_table(
        "admin_security_state",
        sa.Column("singleton_id", sa.Integer(), nullable=False),
        sa.Column("password_verifier", sa.String(length=1024), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("singleton_id"),
        sa.CheckConstraint("singleton_id = 1", name="ck_admin_security_state_singleton"),
    )


def downgrade() -> None:
    op = __import__("alembic").op
    op.drop_table("admin_security_state")
