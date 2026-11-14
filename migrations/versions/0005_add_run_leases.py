"""Add heartbeats and leases to durable runs."""

import sqlalchemy as sa
from alembic import op

revision = "0005_add_run_leases"
down_revision = "0004_create_runtime_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("lease_owner", sa.String(length=255), nullable=True))
    op.add_column("runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "lease_expires_at")
    op.drop_column("runs", "lease_owner")
    op.drop_column("runs", "finished_at")
    op.drop_column("runs", "heartbeat_at")
    op.drop_column("runs", "started_at")
