"""allow openfda directory source

Revision ID: 0c8a3b45ba4f
Revises: 1e64812a63db
Create Date: 2026-08-14 09:02:14.878018
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0c8a3b45ba4f'
down_revision: Union[str, None] = '1e64812a63db'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite cannot ALTER a CHECK constraint, so batch_alter_table rebuilds the
    # table. Adding 'openfda' to the allowed directory sources.
    with op.batch_alter_table("directory_entries", schema=None) as batch_op:
        batch_op.drop_constraint("ck_directory_source", type_="check")
        batch_op.create_check_constraint(
            "ck_directory_source",
            "source IN ('eudamed','mhra','openfda','manual')",
        )


def downgrade() -> None:
    with op.batch_alter_table("directory_entries", schema=None) as batch_op:
        batch_op.drop_constraint("ck_directory_source", type_="check")
        batch_op.create_check_constraint(
            "ck_directory_source",
            "source IN ('eudamed','mhra','manual')",
        )
