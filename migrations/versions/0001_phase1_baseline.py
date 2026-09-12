"""Establish the Phase 1 migration baseline without business tables."""

from collections.abc import Sequence


revision: str = "0001_phase1_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Business schema is intentionally deferred to its authorized phase."""


def downgrade() -> None:
    """The schema-neutral baseline has no objects to remove."""
