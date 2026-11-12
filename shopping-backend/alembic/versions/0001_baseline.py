"""baseline: create every table from the ORM metadata

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-11

Why the baseline is `metadata.create_all()` instead of hand-written DDL: the models *are* the schema
definition for this project, and a hand-copied 0001 is a second source of truth that drifts on the
first column rename. `tests/test_alembic.py` proves this migration produces a schema that matches
the models on SQLite; from here on, `alembic revision --autogenerate` diffs against that same
metadata, so drift is detected by the tool rather than by an outage.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from shopping_backend.db.base import metadata as target_metadata

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("goods", "stock", "coupon", "user_preference", "agent_request_log")


def upgrade() -> None:
    bind = op.get_bind()
    tables = [target_metadata.tables[name] for name in TABLES]
    # checkfirst=True keeps this idempotent: dev environments may already have run create_all.
    target_metadata.create_all(bind=bind, tables=tables, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    tables = [target_metadata.tables[name] for name in reversed(TABLES)]
    target_metadata.drop_all(bind=bind, tables=tables, checkfirst=True)
