"""add attempt recovery phases

Revision ID: c60e39c19d47
Revises: 71b14de0faea
Create Date: 2026-07-14 14:02:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c60e39c19d47"
down_revision: str | Sequence[str] | None = "71b14de0faea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing running attempts predate phase tracking and must recover as
    # unknown/dispatched rather than being silently replayed.
    op.add_column(
        "attempts",
        sa.Column(
            "phase",
            sa.String(length=40),
            nullable=False,
            server_default="legacy_unknown",
        ),
    )
    op.add_column("attempts", sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "attempts",
        sa.Column("response_landed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("attempts", sa.Column("response_artifact_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    op.drop_column("attempts", "response_artifact_id")
    op.drop_column("attempts", "response_landed_at")
    op.drop_column("attempts", "dispatched_at")
    op.drop_column("attempts", "phase")
