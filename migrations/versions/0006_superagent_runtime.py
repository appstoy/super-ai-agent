"""Add Router transcript, durable tool runs, and BYOM devices."""

import sqlalchemy as sa
from alembic import op


def _tables() -> set[str]:
    """Existing table names, for idempotent recovery of partial upgrades."""

    return set(sa.inspect(op.get_bind()).get_table_names())


def _subscription_columns() -> set[str]:
    """Existing subscriptions columns, for idempotent partial upgrades."""

    bind = op.get_bind()
    if "subscriptions" not in _tables():
        return set()
    return {col["name"] for col in sa.inspect(bind).get_columns("subscriptions")}

revision = "0006_superagent_runtime"
down_revision = "0005_add_run_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "seq" not in _subscription_columns():
        op.add_column(
            "subscriptions",
            sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        )
    existing = _tables()
    if "router_messages" not in existing:
        op.create_table(
            "router_messages",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("tenant_id", sa.String(length=255), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(length=32), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "tool_runs" not in existing:
        op.create_table(
            "tool_runs",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("task_id", sa.Uuid(), nullable=False),
            sa.Column("worker_id", sa.Uuid(), nullable=False),
            sa.Column("origin_run_id", sa.Uuid(), nullable=True),
            sa.Column("tool_name", sa.String(length=255), nullable=False),
            sa.Column("arguments_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("result_text", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("cancel_requested", sa.Integer(), nullable=False),
            sa.Column("timeout_seconds", sa.String(length=32), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "devices" not in existing:
        op.create_table(
            "devices",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("capabilities", sa.Text(), nullable=False),
            sa.Column("secret_hash", sa.String(length=128), nullable=False),
            sa.Column("revoked", sa.Integer(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    existing = _tables()
    if "devices" in existing:
        op.drop_table("devices")
    if "tool_runs" in existing:
        op.drop_table("tool_runs")
    if "router_messages" in existing:
        op.drop_table("router_messages")
    if "seq" in _subscription_columns():
        op.drop_column("subscriptions", "seq")
