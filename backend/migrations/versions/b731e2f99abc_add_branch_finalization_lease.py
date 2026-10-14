"""add branch finalization lease

Revision ID: b731e2f99abc
Revises: c60e39c19d47
Create Date: 2026-07-14 14:12:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b731e2f99abc"
down_revision: str | Sequence[str] | None = "c60e39c19d47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "branch_heads",
        sa.Column("finalization_owner_run_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "branch_heads",
        sa.Column("finalization_lock_token", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "branch_heads",
        sa.Column("finalization_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("branch_heads", "finalization_lease_expires_at")
    op.drop_column("branch_heads", "finalization_lock_token")
    op.drop_column("branch_heads", "finalization_owner_run_id")
